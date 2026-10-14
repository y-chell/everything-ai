# -*- coding: utf-8 -*-
"""文件索引:FRN 树内存索引 + SQLite 持久化 + USN Journal 增量回放。

设计参照 Everything:索引存 FRN 树(frn → parent/name/flags/size/mtime),
路径按需/预计算解析;增量靠 USN Journal,断档即全量重建。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import ntfs

ROOT_FRN = 5  # NTFS 根目录固定记录号(低 48 位;完整 FRN 高 16 位是序号,不固定)
ROOT_RECNUM_MASK = 0x0000FFFFFFFFFFFF
MAX_PATH_DEPTH = 256


def _recnum(frn: int) -> int:
    """FRN → 记录号(低 48 位)。根判断/树遍历只看记录号。"""
    return frn & ROOT_RECNUM_MASK

def _parse_excluded_prefixes(raw: str) -> tuple[str, ...]:
    """EVERYTHING_AI_EXCLUDE 解析:分号分隔的路径前缀,归一小写,忽略空段。"""
    return tuple(p.strip().lower() for p in raw.split(";") if p.strip())


# 可选配置:设环境变量 EVERYTHING_AI_EXCLUDE(分号分隔、大小写不敏感)后,
# 匹配前缀的路径会从结果中剔除——用于把"agent 拿到路径后递归读取会有副作用"的
# 目录(如被手机同步软件托管的目录,读取会触发云端下载)挡在结果之外。
EXCLUDED_PATH_PREFIXES = _parse_excluded_prefixes(os.environ.get("EVERYTHING_AI_EXCLUDE", ""))


@dataclass
class Entry:
    parent_frn: int
    name: str
    is_dir: bool
    size: int
    mtime_ft: int


class VolumeIndex:
    """单卷索引:FRN 树 + 预计算路径表。"""

    def __init__(self, drive: str):
        self.drive = drive.upper()
        self.entries: dict[int, Entry] = {}
        self.paths: dict[int, str] = {}
        self.aliases: dict[int, tuple[tuple[int, str], ...]] = {}  # frn → 硬链接别名
        self.alias_paths: dict[tuple[int, int, str], str] = {}  # (frn, parent, name) → 路径
        self.journal_id = 0
        self.next_usn = 0
        self.built_at = 0.0
        self.record_count = 0

    def build_from_mft(self, progress=None) -> None:
        """全量枚举 $MFT 建索引,并查询 USN journal 状态作为增量起点。"""
        entries: dict[int, Entry] = {}
        aliases: dict[int, tuple[tuple[int, str], ...]] = {}
        for e in ntfs.enumerate_mft_records(self.drive, progress):
            if e.name is None:
                continue
            entries[e.frn] = Entry(e.parent_frn, e.name, e.is_dir, e.size, e.mtime_ft)
            if e.aliases:
                aliases[e.frn] = e.aliases
        self.entries = entries
        self.aliases = aliases
        self.record_count = len(entries)
        state = ntfs.query_usn_journal(self.drive)
        self.journal_id = state.journal_id
        self.next_usn = state.next_usn
        self.built_at = time.time()
        self._rebuild_paths()

    def _rebuild_paths(self) -> None:
        """BFS 从根向下预计算全部路径。根的直接子项 = parent 记录号为 5 的条目。"""
        paths: dict[int, str] = {}
        children: dict[int, list[int]] = {}
        for frn, e in self.entries.items():
            children.setdefault(e.parent_frn, []).append(frn)
        root_prefix = self.drive + ":\\"
        queue: list[tuple[int, str]] = []
        for frn in children.get(ROOT_FRN, ()):  # parent_frn 恰为裸 5(理论情况)
            e = self.entries[frn]
            p = root_prefix + e.name + ("\\" if e.is_dir else "")
            paths[frn] = p
            if e.is_dir:
                queue.append((frn, p))
        # 实际情况:根子项的 parent_frn 带根目录序号(如 0x0005000000000005)
        for frn, e in self.entries.items():
            if _recnum(e.parent_frn) == ROOT_FRN and frn not in paths:
                p = root_prefix + e.name + ("\\" if e.is_dir else "")
                paths[frn] = p
                if e.is_dir:
                    queue.append((frn, p))
        while queue:
            parent_frn, parent_path = queue.pop()
            for frn in children.get(parent_frn, ()):
                e = self.entries[frn]
                p = parent_path + e.name + ("\\" if e.is_dir else "")
                paths[frn] = p
                if e.is_dir:
                    queue.append((frn, p))  # 目录路径已含尾部 \,不再追加
        self.paths = paths
        # 硬链接别名路径:父目录路径此时已可解析
        alias_paths: dict[tuple[int, int, str], str] = {}
        for frn, alist in self.aliases.items():
            for parent, name in alist:
                pp = paths.get(parent)
                if pp is None:
                    pp = self.resolve_path(parent)
                if pp is None:
                    continue
                base = pp if pp.endswith("\\") else pp + "\\"
                alias_paths[(frn, parent, name)] = base + name
        self.alias_paths = alias_paths

    def resolve_path(self, frn: int) -> str | None:
        """向上爬 FRN 树解析路径(路径表未命中时兜底)。根目录(FRN 5)不在 entries。"""
        p = self.paths.get(frn)
        if p is not None:
            return p
        parts: list[str] = []
        cur = frn
        for _ in range(MAX_PATH_DEPTH):
            if _recnum(cur) == ROOT_FRN:
                break
            e = self.entries.get(cur)
            if e is None:
                return None
            parts.append(e.name)
            cur = e.parent_frn
        else:
            return None
        path = self.drive + ":\\" + "\\".join(reversed(parts))
        self.paths[frn] = path
        return path

    def apply_usn_records(self, records: list[ntfs.UsnRecord], next_usn: int) -> int:
        """回放 USN 变更 → 返回受影响条数。目录改名靠 FRN 树天然保持子孙路径正确。"""
        changed = 0
        for r in records:
            reason = r.reason
            if reason & ntfs.USN_REASON_FILE_DELETE:
                self.entries.pop(r.frn, None)
                self.paths.pop(r.frn, None)
                self.aliases.pop(r.frn, None)
                changed += 1
            elif reason & ntfs.USN_REASON_RENAME_NEW_NAME:
                old = self.entries.get(r.frn)
                is_dir = bool(r.attributes & ntfs.FILE_ATTRIBUTE_DIRECTORY) if old is None else old.is_dir
                # 目录改名:子孙路径缓存全部失效(目录路径带尾部 \,前缀匹配正好圈定子孙)
                old_path = self.paths.pop(r.frn, None)
                if is_dir and old_path:
                    for f in [f for f, p in self.paths.items() if p.startswith(old_path)]:
                        del self.paths[f]
                self.entries[r.frn] = Entry(r.parent_frn, r.name, is_dir, old.size if old else 0, r.timestamp_ft)
                changed += 1
            elif reason & ntfs.USN_REASON_FILE_CREATE:
                is_dir = bool(r.attributes & ntfs.FILE_ATTRIBUTE_DIRECTORY)
                self.entries[r.frn] = Entry(r.parent_frn, r.name, is_dir, 0, r.timestamp_ft)
                changed += 1
            elif reason & ntfs.USN_REASON_DATA_CHANGE:
                old = self.entries.get(r.frn)
                if old is not None:
                    old.mtime_ft = r.timestamp_ft
                    changed += 1
            # RENAME_OLD_NAME / CLOSE / 其他 reason 不影响索引
        self.next_usn = next_usn
        return changed

    def refresh_usn(self) -> tuple[int, bool]:
        """拉取 USN 增量。返回 (变更数, 是否需要全量重建)。"""
        if not self.journal_id:
            return 0, True
        try:
            next_usn, records = ntfs.read_usn_changes(self.drive, self.journal_id, self.next_usn)
        except ntfs.NtfsError:
            return 0, True
        if not records:
            self.next_usn = next_usn
            return 0, False
        return self.apply_usn_records(records, next_usn), False


class FileIndex:
    """多卷索引管理 + SQLite 持久化。线程安全(后台建索引 + 查询)。"""

    def __init__(self, db_path: str | None = None, volumes: list[str] | None = None):
        self.db_path = db_path or str(
            Path(os.environ.get("LOCALAPPDATA", Path.home())) / "everything-ai" / "index.sqlite"
        )
        self.volumes = [v.upper() for v in (volumes or _detect_volumes())]
        self.vol_index: dict[str, VolumeIndex] = {v: VolumeIndex(v) for v in self.volumes}
        self.lock = threading.RLock()
        self.status = "empty"  # empty | building | ready | error
        self.status_detail = ""
        self._ensure_db()

    # -- 持久化 ---------------------------------------------------------------

    def _ensure_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS files ("
                " volume TEXT NOT NULL, frn INTEGER NOT NULL, parent INTEGER NOT NULL,"
                " name TEXT NOT NULL, is_dir INTEGER NOT NULL, size INTEGER NOT NULL,"
                " mtime INTEGER NOT NULL, PRIMARY KEY (volume, frn)) WITHOUT ROWID"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                " volume TEXT PRIMARY KEY, journal_id INTEGER, next_usn INTEGER,"
                " built_at REAL, record_count INTEGER)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS aliases ("
                " volume TEXT NOT NULL, frn INTEGER NOT NULL, parent INTEGER NOT NULL,"
                " name TEXT NOT NULL, PRIMARY KEY (volume, frn, name))"
            )
            con.commit()
        finally:
            con.close()

    def _save_volume(self, vol: VolumeIndex) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("BEGIN")
            con.execute("DELETE FROM files WHERE volume=?", (vol.drive,))
            con.executemany(
                "INSERT INTO files VALUES (?,?,?,?,?,?,?)",
                (
                    (vol.drive, frn, e.parent_frn, e.name, 1 if e.is_dir else 0, e.size, e.mtime_ft)
                    for frn, e in vol.entries.items()
                ),
            )
            con.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?,?,?,?)",
                (vol.drive, vol.journal_id, vol.next_usn, vol.built_at, vol.record_count),
            )
            con.execute("DELETE FROM aliases WHERE volume=?", (vol.drive,))
            con.executemany(
                "INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)",
                (
                    (vol.drive, frn, parent, name)
                    for frn, alist in vol.aliases.items()
                    for parent, name in alist
                ),
            )
            con.commit()
        finally:
            con.close()

    def _load_volume(self, vol: VolumeIndex) -> bool:
        """从 SQLite 加载;返回是否成功。"""
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT journal_id, next_usn, built_at, record_count FROM meta WHERE volume=?",
                (vol.drive,),
            ).fetchone()
            if row is None:
                return False
            entries: dict[int, Entry] = {}
            for frn, parent, name, is_dir, size, mtime in con.execute(
                "SELECT frn, parent, name, is_dir, size, mtime FROM files WHERE volume=?",
                (vol.drive,),
            ):
                entries[frn] = Entry(parent, name, bool(is_dir), size, mtime)
            if not entries:
                return False
            vol.entries = entries
            vol.record_count = len(entries)
            vol.journal_id, vol.next_usn, vol.built_at, _ = row
            vol.aliases = {
                frn: tuple((parent, name) for _v, _f, parent, name in rows)
                for frn, rows in _group_aliases(
                    con.execute("SELECT volume, frn, parent, name FROM aliases WHERE volume=?", (vol.drive,))
                ).items()
            }
            vol._rebuild_paths()  # 路径表不持久化,加载后必须重建
            return True
        finally:
            con.close()

    # -- 构建 / 刷新 ----------------------------------------------------------

    def build_all(self, force: bool = False) -> None:
        """启动入口:优先加载缓存 + USN 回放,缓存缺失/过期则全量建索引。"""
        with self.lock:
            if self.status == "building":
                return
            self.status = "building"
        try:
            for vol in self.vol_index.values():
                loaded = False
                if not force:
                    try:
                        loaded = self._load_volume(vol)
                    except sqlite3.DatabaseError as exc:
                        self.status_detail = f"缓存损坏,重建: {exc}"
                        loaded = False
                if loaded:
                    changed, need_rebuild = vol.refresh_usn()
                    if need_rebuild:
                        loaded = False
                    elif changed:
                        self._save_volume(vol)
                if not loaded:
                    vol.build_from_mft()
                    self._save_volume(vol)
            with self.lock:
                self.status = "ready"
                self.status_detail = ""
        except Exception as exc:  # noqa: BLE001 - 状态上报给 MCP 调用方
            with self.lock:
                self.status = "error"
                self.status_detail = f"{type(exc).__name__}: {exc}"

    def refresh(self) -> int:
        """查询前刷新所有卷的 USN 增量。返回总变更数;需要重建时自动重建。"""
        total = 0
        for vol in self.vol_index.values():
            changed, need_rebuild = vol.refresh_usn()
            total += changed
            if need_rebuild:
                vol.build_from_mft()
                self._save_volume(vol)
            elif changed:
                self._save_volume(vol)
        return total

    def rebuild(self, drive: str | None = None) -> None:
        targets = [drive.upper()] if drive else self.volumes
        for d in targets:
            vol = self.vol_index.get(d)
            if vol is None:
                raise ValueError(f"未知卷 {d}: 可用卷 {self.volumes}")
            vol.build_from_mft()
            self._save_volume(vol)

    # -- 查询辅助 -------------------------------------------------------------

    def iter_items(self):
        """遍历 (drive, frn, entry, path)。含硬链接别名;路径缺失(孤儿)跳过。"""
        for drive, vol in self.vol_index.items():
            entries, paths = vol.entries, vol.paths
            for frn, e in entries.items():
                p = paths.get(frn)
                if p is None:
                    p = vol.resolve_path(frn)
                    if p is None:
                        continue
                yield drive, frn, e, p
            # 别名条目(硬链接的额外路径)
            for frn, alist in vol.aliases.items():
                e = entries.get(frn)
                if e is None:
                    continue
                for parent, name in alist:
                    ap = vol.alias_paths.get((frn, parent, name))
                    if ap is None:
                        continue
                    yield drive, frn, Entry(parent, name, e.is_dir, e.size, e.mtime_ft), ap

    def index_status(self) -> dict:
        with self.lock:
            vols = []
            for v in self.volumes:
                vi = self.vol_index[v]
                vols.append(
                    {
                        "volume": v + ":",
                        "records": vi.record_count,
                        "built_at": vi.built_at,
                        "next_usn": vi.next_usn,
                        "journal_id": vi.journal_id,
                    }
                )
            return {"status": self.status, "detail": self.status_detail, "volumes": vols}


def _group_aliases(rows):
    """把 (volume, frn, parent, name) 行分组为 {frn: [(None, frn, parent, name), ...]}。"""
    grouped: dict[int, list[tuple]] = {}
    for row in rows:
        grouped.setdefault(row[1], []).append(row)
    return grouped


def _detect_volumes() -> list[str]:
    """枚举本机固定磁盘卷(排除可移动设备)。"""
    drives = []
    for bit in range(65, 91):  # 'A'..'Z'
        letter = chr(bit)
        if os.path.exists(f"{letter}:\\"):
            dtype = ctypes_get_drive_type(letter)
            if dtype == 3:  # DRIVE_FIXED
                drives.append(letter)
    return drives or ["C"]


def ctypes_get_drive_type(letter: str) -> int:
    import ctypes as _ctypes

    return _ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
