# -*- coding: utf-8 -*-
"""查询引擎:分阶段求值 + 相关性排序 + 有界 Top-K。

与旧实现的区别:
- 不再对每条记录调用 Python 闭包 matcher;便宜的条件(名字子串 / 扩展名 /
  大小 / 时间)直接在扁平扫描表的平行数组上做列表推导,昂贵的条件
  (路径 / 正则 / OR / NOT 子树)只施加在前一级剩下的候选上。
- 排序键由生成器逐条产出,配 ``heapq.nsmallest`` 做有界堆:不再把所有匹配
  先收进列表再排序,内存与耗时都只和候选数成正比(与 keep 无关的常数)。
"""
from __future__ import annotations

import heapq
import os
import stat as stat_mod
import time
from dataclasses import dataclass

from . import rank
from .index import EXCLUDED_PATH_PREFIXES, FILETIME_EPOCH_OFFSET, FileIndex, ScanTable, VolumeIndex
from .query import Plan, _row_path, run_stages

MAX_DEPTH_PENALTY = 12


@dataclass(slots=True)
class Hit:
    path: str
    name: str
    is_dir: bool
    size: int
    mtime_ft: int
    score: int = 0
    exact: bool = False

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "type": "folder" if self.is_dir else "file",
            "size": None if self.is_dir else self.size,
            "modified": ft_to_iso(self.mtime_ft),
            "exact": self.exact,
        }


@dataclass(slots=True)
class Outcome:
    hits: list[Hit]
    total: int
    elapsed_ms: float
    offset: int = 0

    @property
    def truncated(self) -> bool:
        return self.offset + len(self.hits) < self.total


def ft_to_iso(ft: int) -> str | None:
    """FILETIME → 本地时区 ISO 8601;未知时间(0 / 早于 1980)返回 None。"""
    if ft < rank.FIRST_KNOWN_FT:
        return None
    secs = ft // 10_000_000 - FILETIME_EPOCH_OFFSET
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(secs))
    except (OverflowError, OSError, ValueError):
        return None


def _excluded(path: str) -> bool:
    if not EXCLUDED_PATH_PREFIXES:
        return False
    low = path.lower()
    return any(low.startswith(p) for p in EXCLUDED_PATH_PREFIXES)


def _hit_of(vol: VolumeIndex, scan: ScanTable, i: int, path: str) -> Hit:
    return Hit(
        path,
        scan.names[i],
        bool(scan.flags[i] & rank.FLAG_DIR),
        scan.sizes[i],
        scan.mtimes[i],
    )


def _kind_filter(scan: ScanTable, kind: str):
    flags = scan.flags
    if kind == "files":
        return lambda i: not (flags[i] & rank.FLAG_DIR)
    if kind == "folders":
        return lambda i: bool(flags[i] & rank.FLAG_DIR)
    return None


def _iter_keyed(
    index: FileIndex,
    plan: Plan,
    kind: str,
    sort: str,
    path_scope: str | None,
    include_system: bool,
    counter: list,
):
    """逐条产出 (排序键, 序号, vol, scan, 行号, 分数, 是否整名命中)。

    刻意不在循环里解析路径、也不构造 Hit:这两件事实测各占 ~440ns / ~470ns
    每候选,而最终只会用到 30-200 条。只有需要按路径过滤或按路径排序时才解析。
    """
    scope_l = (path_scope.rstrip("\\/").lower() + "\\") if path_scope else None
    need_path = bool(EXCLUDED_PATH_PREFIXES) or scope_l is not None or sort == "name"
    scoring = bool(plan.score_terms) and sort == "relevance"
    terms = plan.score_terms
    single = plan.single_term
    seq = 0
    for vol in index.vol_index.values():
        for scan, dead in vol.scan_tables():
            if not len(scan):
                continue
            flags, depths, mtimes, sizes, names = scan.flags, scan.depths, scan.mtimes, scan.sizes, scan.names
            lower, parents, frns = scan.lower, scan.parents, scan.frns
            dir_path = vol.dir_path

            for i in run_stages(plan, vol, scan, _kind_filter(scan, kind)):
                frn = frns[i]
                if dead and frn in dead:
                    continue
                fl = flags[i]
                if not include_system and (fl & (rank.FLAG_LOW_PRIORITY | rank.FLAG_HIDDEN)):
                    continue
                is_dir = bool(fl & rank.FLAG_DIR)
                name = names[i]
                if need_path:
                    d = dir_path(parents[i])
                    if d is None:
                        continue
                    keypath = d + name + "\\" if is_dir else d + name
                    if _excluded(keypath):
                        continue
                    if scope_l is not None and not keypath.lower().startswith(scope_l):
                        continue
                else:
                    keypath = name
                counter[0] += 1

                score = 0
                exact = False
                if scoring:
                    lname = lower[i]
                    score = rank.score_terms(lname, terms)
                    if single is not None:
                        bonus = rank.exact_bonus(lname, single)
                        if bonus:
                            score += bonus
                            exact = True
                    elif len(terms) == 1 and lname == terms[0]:
                        exact = True
                if is_dir:
                    score += 2
                if fl & rank.FLAG_LOW_PRIORITY:
                    score -= 35
                if fl & rank.FLAG_HIDDEN:
                    score -= 15
                dd = depths[i]
                score -= dd if dd < MAX_DEPTH_PENALTY else MAX_DEPTH_PENALTY

                if sort == "modified":
                    key = (-mtimes[i], -score, keypath)
                elif sort == "oldest":
                    key = (mtimes[i], -score, keypath)
                elif sort == "size":
                    key = (-sizes[i], keypath)
                elif sort == "name":
                    key = (keypath.lower(),)
                else:
                    key = (-score, 1 if is_dir else 0, -mtimes[i], keypath)
                seq += 1
                yield key, seq, vol, scan, i, score, exact


def _materialize(top, offset: int) -> list[Hit]:
    """把选出的一页变成 Hit(此时才解析路径)。"""
    hits: list[Hit] = []
    for _key, _seq, vol, scan, i, score, exact in top[offset:]:
        path = _row_path(vol, scan, i)
        if path is None:
            continue
        hits.append(
            Hit(path, scan.names[i], bool(scan.flags[i] & rank.FLAG_DIR), scan.sizes[i], scan.mtimes[i], score, exact)
        )
    return hits


def run_search(
    index: FileIndex,
    plan: Plan,
    *,
    kind: str = "all",
    sort: str = "relevance",
    limit: int = 30,
    offset: int = 0,
    path_scope: str | None = None,
    include_system: bool = True,
) -> Outcome:
    """执行一次查询。空查询按"最近修改"倒序返回(等价于 recent_files)。"""
    t0 = time.perf_counter()
    keep = offset + limit
    # 空查询 + 时间序:走 recent_files 的 mtime 快速通道(先 C 层筛再解析路径)。
    # 其他排序(按大小/名字)仍需走通用路径,不能吞掉调用方要的排序。
    if plan.empty and sort in ("relevance", "modified"):
        out = recent_files(
            index,
            folder=path_scope,
            kind=kind,
            limit=keep,
            include_system=include_system,
            extensions=(),
            days=0,
        )
        out.hits = out.hits[offset:]
        out.offset = offset
        out.elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return out

    counter = [0]
    top = heapq.nsmallest(keep, _iter_keyed(index, plan, kind, sort, path_scope, include_system, counter))
    return Outcome(_materialize(top, offset), counter[0], (time.perf_counter() - t0) * 1000.0, offset)


# ---------------------------------------------------------------------------
# 目录级工具:直接走文件系统(单目录 O(子项),永远是最新状态)
# ---------------------------------------------------------------------------

def _entry_hit(entry: os.DirEntry) -> Hit | None:
    """os.DirEntry → Hit;目录不算 size,stat 失败视为不可读并跳过。"""
    try:
        st = entry.stat()
    except OSError:
        return None
    is_dir = stat_mod.S_ISDIR(st.st_mode)
    return Hit(
        entry.path,
        entry.name,
        is_dir,
        0 if is_dir else st.st_size,
        int((st.st_mtime + FILETIME_EPOCH_OFFSET) * 10_000_000),
    )


def is_hidden(st) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & 0x2)


def list_folder(
    path: str,
    *,
    extensions: tuple[str, ...] = (),
    days: int = 0,
    kind: str = "files",
    include_hidden: bool = False,
    limit: int = 100,
) -> tuple[list[Hit], int, str | None]:
    """列出目录的直接子项。返回 (命中, 总数, 错误)。"""
    norm = os.path.abspath(path)
    if _excluded(norm):
        return [], 0, "该目录对 AI 不可见(EVERYTHING_AI_EXCLUDE)"
    if not os.path.isdir(norm):
        return [], 0, f"不是目录或不存在: {path}"
    since = time.time() - days * 86400 if days > 0 else 0.0
    exts = tuple("." + e.lower().lstrip(".") for e in extensions if e)
    entries: list[tuple[float, Hit]] = []
    total = 0
    try:
        with os.scandir(norm) as it:
            for entry in it:
                try:
                    st = entry.stat()
                except OSError:
                    continue
                is_dir = stat_mod.S_ISDIR(st.st_mode)
                if kind == "files" and is_dir:
                    continue
                if kind == "folders" and not is_dir:
                    continue
                if not include_hidden and is_hidden(st):
                    continue
                if exts and not is_dir and not entry.name.lower().endswith(exts):
                    continue
                if since and st.st_mtime < since:
                    continue
                total += 1
                hit = Hit(
                    entry.path,
                    entry.name,
                    is_dir,
                    0 if is_dir else st.st_size,
                    int((st.st_mtime + FILETIME_EPOCH_OFFSET) * 10_000_000),
                )
                entries.append((st.st_mtime, hit))
    except PermissionError:
        return [], 0, f"没有读取权限: {path}"
    except OSError as exc:
        return [], 0, f"读取目录失败: {exc}"
    entries.sort(key=lambda t: -t[0])
    return [h for _t, h in entries[:limit]], total, None


def file_info(path: str) -> dict:
    """单个路径的元数据(实读磁盘,不查索引)。"""
    norm = os.path.abspath(path)
    if _excluded(norm):
        return {"exists": False, "path": norm, "error": "该路径对 AI 不可见"}
    try:
        st = os.stat(norm)
    except FileNotFoundError:
        return {"exists": False, "path": norm}
    except OSError as exc:
        return {"exists": False, "path": norm, "error": str(exc)}
    is_dir = stat_mod.S_ISDIR(st.st_mode)
    base = os.path.basename(norm)
    dot = base.rfind(".")
    return {
        "exists": True,
        "path": norm,
        "name": base,
        "type": "folder" if is_dir else "file",
        "size": None if is_dir else st.st_size,
        "modified": ft_to_iso(int((st.st_mtime + FILETIME_EPOCH_OFFSET) * 10_000_000)),
        "created": ft_to_iso(int((st.st_ctime + FILETIME_EPOCH_OFFSET) * 10_000_000)),
        "accessed": ft_to_iso(int((st.st_atime + FILETIME_EPOCH_OFFSET) * 10_000_000)),
        "extension": base[dot + 1:].lower() if dot > 0 else None,
        "hidden": is_hidden(st),
        "read_only": not (st.st_mode & 0o200),
    }


RECENT_KEEP_FACTOR = 4
RECENT_KEEP_MIN = 80


def recent_files(
    index: FileIndex,
    *,
    folder: str | None = None,
    extensions: tuple[str, ...] = (),
    days: int = 0,
    kind: str = "files",
    limit: int = 30,
    include_system: bool = False,
    offset: int = 0,
) -> Outcome:
    """最近修改的文件(倒序)。先用 mtime 数组做 C 层筛选与 Top-K,再解析路径。"""
    t0 = time.perf_counter()
    since_ft = int((time.time() - days * 86400 + FILETIME_EPOCH_OFFSET) * 10_000_000) if days > 0 else 0
    scope_l = (folder.rstrip("\\/").lower() + "\\") if folder else None
    exts = tuple("." + e.lower().lstrip(".") for e in extensions if e)
    want = offset + limit
    keep = max(want * RECENT_KEEP_FACTOR, RECENT_KEEP_MIN)

    picked: list[tuple[int, int, VolumeIndex, ScanTable, set | None]] = []
    for vol in index.vol_index.values():
        for scan, dead in vol.scan_tables():
            if not len(scan):
                continue
            flags = scan.flags
            if since_ft:
                cands = [i for i, t in enumerate(scan.mtimes) if t >= since_ft]
            else:
                cands = list(range(len(scan.names)))
            if kind == "files":
                cands = [i for i in cands if not (flags[i] & rank.FLAG_DIR)]
            elif kind == "folders":
                cands = [i for i in cands if flags[i] & rank.FLAG_DIR]
            if not include_system:
                cands = [i for i in cands if not (flags[i] & (rank.FLAG_LOW_PRIORITY | rank.FLAG_HIDDEN))]
            if exts:
                lower = scan.lower
                cands = [i for i in cands if (flags[i] & rank.FLAG_DIR) or lower[i].endswith(exts)]
            if not cands:
                continue
            for i in heapq.nlargest(keep, cands, key=scan.mtimes.__getitem__):
                picked.append((scan.mtimes[i], i, vol, scan, dead))
    picked.sort(key=lambda t: -t[0])

    hits: list[Hit] = []
    total = 0
    for _mt, i, vol, scan, dead in picked:
        if dead and scan.frns[i] in dead:
            continue
        path = _row_path(vol, scan, i)
        if path is None or _excluded(path):
            continue
        if scope_l is not None and not path.lower().startswith(scope_l):
            continue
        total += 1
        hits.append(_hit_of(vol, scan, i, path))
        if len(hits) >= keep:
            break
    # total 只在候选被 keep 截断时才可能偏小;它只需支撑 truncated 判断
    return Outcome(hits[offset:offset + limit], max(total, len(hits)), (time.perf_counter() - t0) * 1000.0, offset)
