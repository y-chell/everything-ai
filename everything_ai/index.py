# -*- coding: utf-8 -*-
"""文件索引:FRN 树 + 扁平扫描表 + SQLite 持久化 + USN Journal 增量回放。

设计参照 Everything / WinShun:
- 索引存 FRN 树(frn → parent/name/flags/size/mtime),**路径延迟解析**:
  只有目录的路径被记忆化(WinShun 同款 deferred resolution),文件路径由
  ``父目录路径 + 文件名`` 现算,不必为每条记录常驻一个完整路径字符串。
- 查询走**扁平扫描表**(平行数组),最热的子串条件退化成 ``needle in name``
  的单次 C 调用;代价高的条件只施加在候选集上。
- 增量靠 USN Journal,分**基表 + 增量表**两张:
  * 基表(full build 时定稿)按 FRN 失效——被改过的记录,它的旧行整体作废;
  * 增量表按 FRN 覆盖,永不失效。
  这样「删除后重建同一 FRN」和「新建文件」都不会被自己刚写的新行滤掉
  (单表按 FRN 打死亡标记时这两类必然误杀)。碎片超阈值再整表重建。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from array import array
from dataclasses import dataclass

from . import ntfs
from .rank import FLAG_DIR, FLAG_HIDDEN, FLAG_LOW_PRIORITY, low_priority_dir

ROOT_FRN = 5  # NTFS 根目录固定记录号(低 48 位;完整 FRN 高 16 位是序号,不固定)
ROOT_RECNUM_MASK = 0x0000FFFFFFFFFFFF
MAX_PATH_DEPTH = 256
SCHEMA_VERSION = 5  # v5:USN 期新建的文件补回真实 size(此前落盘的行 size 恒为 0)
FILETIME_EPOCH_OFFSET = 11644473600  # 1601-01-01 → 1970-01-01 的秒数

# 扫描表碎片回收阈值:失效 FRN 或增量行超过这些值就整表重建
SCAN_DEAD_RATIO = 4
SCAN_DEAD_MAX = 65536
SCAN_DELTA_MAX = 16384
# 占比规则的行数下限:小表上"几行失效"就触发重建会退化成反复全量重建
SCAN_COMPACT_FLOOR = 4096


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


def _stat_size_mtime(path: str) -> tuple[int, int] | None:
    """磁盘上的 (size, mtime_ft);拿不到返回 None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, int((st.st_mtime + FILETIME_EPOCH_OFFSET) * 10_000_000)


@dataclass
class Entry:
    parent_frn: int
    name: str
    is_dir: bool
    size: int
    mtime_ft: int
    hidden: bool = False


class ScanTable:
    """一卷的扁平扫描表:一行 = 一个目录项(主名或硬链接别名)。

    平行数组而不是对象数组——查询最热的循环只碰 ``lower``,``needle in x``
    是一次 C 调用;其余字段按需索引。
    """

    __slots__ = ("names", "lower", "parents", "sizes", "mtimes", "frns", "flags", "depths")

    def __init__(self) -> None:
        self.names: list[str] = []
        self.lower: list[str] = []
        self.parents: array = array("q")
        self.sizes: array = array("q")
        self.mtimes: array = array("q")
        self.frns: array = array("q")
        self.flags: bytearray = bytearray()
        self.depths: bytearray = bytearray()

    def __len__(self) -> int:
        return len(self.names)

    def add(self, name, parent, size, mtime, frn, flags, depth) -> int:
        self.names.append(name)
        self.lower.append(name.lower())
        self.parents.append(parent)
        self.sizes.append(size)
        self.mtimes.append(mtime)
        self.frns.append(frn)
        self.flags.append(flags)
        self.depths.append(depth if depth < 255 else 255)
        return len(self.names) - 1

    def set_at(self, idx: int, name, parent, size, mtime, frn, flags, depth) -> None:
        self.names[idx] = name
        self.lower[idx] = name.lower()
        self.parents[idx] = parent
        self.sizes[idx] = size
        self.mtimes[idx] = mtime
        self.frns[idx] = frn
        self.flags[idx] = flags
        self.depths[idx] = depth if depth < 255 else 255


class VolumeIndex:
    """单卷索引:FRN 树 + 目录路径缓存 + 扁平扫描表(基表 / 增量表)。"""

    def __init__(self, drive: str):
        self.drive = drive.upper()
        self._root_prefix = self.drive + ":\\"
        self.entries: dict[int, Entry] = {}
        self.aliases: dict[int, tuple[tuple[int, str], ...]] = {}  # frn → 硬链接别名
        self.dir_paths: dict[int, str] = {}   # 目录记录号 → 带尾部反斜杠的路径
        self.dir_depths: dict[int, int] = {}
        self.scan: ScanTable | None = None    # 基表
        self.delta: ScanTable = ScanTable()   # USN 增量表
        self.journal_id = 0
        self.next_usn = 0
        self.built_at = 0.0
        self.record_count = 0
        self._scan_dead: set[int] = set()     # 基表里作废的 FRN
        self._delta_row: dict[int, int] = {}  # FRN → 增量表行号(重复变更就地覆盖)
        self._lp_cache: dict[int, bool] = {}
        # 自上次落盘以来变过的 FRN:用于增量写 SQLite,避免每次启动整表重写
        self.pending_changes: set[int] = set()
        # 回读文件大小的方式,测试可替换
        self._stat = _stat_size_mtime

    # -- 路径(延迟解析) ------------------------------------------------------

    def dir_path(self, frn: int) -> str | None:
        """目录 → 带尾部反斜杠的完整路径。自底向上解析整条链并全部记忆化。"""
        rn = _recnum(frn)
        if rn == ROOT_FRN:
            return self._root_prefix
        cached = self.dir_paths.get(rn)
        if cached is not None:
            return cached
        chain: list[int] = []
        base: str | None = None
        base_depth = 0
        cur = rn
        for _ in range(MAX_PATH_DEPTH):
            c = self.dir_paths.get(cur)
            if c is not None:
                base = c
                base_depth = self.dir_depths[cur]
                break
            if cur == ROOT_FRN:
                base = self._root_prefix
                base_depth = 1
                break
            e = self.entries.get(cur)
            if e is None:
                return None
            chain.append(cur)
            cur = _recnum(e.parent_frn)
        if base is None:
            return None
        for c in reversed(chain):
            base = base + self.entries[c].name + "\\"
            base_depth += 1
            self.dir_paths[c] = base
            self.dir_depths[c] = base_depth
        return self.dir_paths.get(rn)

    def file_path(self, frn: int) -> str | None:
        """任意记录 → 完整路径(目录带尾部反斜杠)。"""
        rn = _recnum(frn)
        e = self.entries.get(rn)
        if e is None:
            return None
        if e.is_dir:
            return self.dir_path(rn)
        d = self.dir_path(e.parent_frn)
        if d is None:
            return None
        return d + e.name

    # 兼容旧调用名
    resolve_path = file_path

    def invalidate_paths(self) -> None:
        """目录改名后整棵子树的缓存路径失效;下次访问再惰性重建。"""
        self.dir_paths.clear()
        self.dir_depths.clear()

    def _dir_low_priority(self, frn: int) -> bool:
        """目录是否低优先级(自身命中规则或从祖先继承)。"""
        rn = _recnum(frn)
        v = self._lp_cache.get(rn)
        if v is not None:
            return v
        e = self.entries.get(rn)
        own = bool(e is not None and e.is_dir and low_priority_dir(e.name.lower()))
        if not own and e is not None and _recnum(e.parent_frn) != ROOT_FRN:
            own = self._dir_low_priority(e.parent_frn)
        self._lp_cache[rn] = own
        return own

    # -- 扫描表 ---------------------------------------------------------------

    def _row_fields(self, frn: int, parent: int, name: str, path: str, e: Entry):
        flags = FLAG_DIR if e.is_dir else 0
        lp = self._dir_low_priority(frn) if e.is_dir else self._dir_low_priority(parent)
        if lp:
            flags |= FLAG_LOW_PRIORITY
        if e.hidden:
            flags |= FLAG_HIDDEN
        return name, parent, e.size, e.mtime_ft, frn, flags, path.count("\\")

    def _resolve_row_path(self, frn: int, parent: int, name: str, e: Entry) -> str | None:
        """行的完整路径。目录主名走 dir_path 缓存;别名行由父目录拼。"""
        if e.is_dir and name == e.name:
            return self.dir_path(frn)
        d = self.dir_path(parent)
        if d is None:
            return None
        return d + name + "\\" if e.is_dir else d + name

    def build_scan(self) -> None:
        """整表重建(全量索引完成后、或碎片超阈值时)。"""
        scan = ScanTable()
        entries = self.entries
        for frn, e in entries.items():
            path = self._resolve_row_path(frn, e.parent_frn, e.name, e)
            if path is None:
                continue
            scan.add(*self._row_fields(frn, _recnum(e.parent_frn), e.name, path, e))
        # 硬链接别名:同一 FRN 的其余路径,作为独立行参与匹配
        for frn, alist in self.aliases.items():
            e = entries.get(frn)
            if e is None:
                continue
            for parent, name in alist:
                path = self._resolve_row_path(frn, parent, name, e)
                if path is None:
                    continue
                scan.add(*self._row_fields(frn, _recnum(parent), name, path, e))
        self.scan = scan
        self.delta = ScanTable()
        self._scan_dead = set()
        self._delta_row = {}

    def _apply_scan_delta(self, touched: set[int]) -> None:
        """USN 变更落到扫描表:基表里这些 FRN 的旧行整体作废,新状态进增量表。"""
        # 待落盘集合与扫描表无关(内存索引可能还没建表),先记下来
        self.pending_changes.update(touched)
        if self.scan is None:
            return
        self._scan_dead.update(touched)
        delta = self.delta
        for frn in touched:
            e = self.entries.get(frn)
            if e is None:
                continue
            path = self._resolve_row_path(frn, e.parent_frn, e.name, e)
            if path is None:
                continue
            fields = self._row_fields(frn, _recnum(e.parent_frn), e.name, path, e)
            idx = self._delta_row.get(frn)
            if idx is None:
                self._delta_row[frn] = delta.add(*fields)
            else:
                delta.set_at(idx, *fields)
        base_rows = len(self.scan.names)
        if (
            len(self._delta_row) > SCAN_DELTA_MAX
            or len(self._scan_dead) > SCAN_DEAD_MAX
            or (base_rows > SCAN_COMPACT_FLOOR and len(self._scan_dead) * SCAN_DEAD_RATIO > base_rows)
        ):
            self.build_scan()

    def scan_tables(self):
        """→ [(扫描表, 需要按 FRN 失效的集合)]。增量表永不作废。"""
        if self.scan is None:
            return []
        if len(self.delta):
            return [(self.scan, self._scan_dead), (self.delta, None)]
        return [(self.scan, self._scan_dead)]

    # -- 构建 / 增量 ----------------------------------------------------------

    def build_from_mft(self, progress=None) -> None:
        """全量枚举 $MFT 建索引,并查询 USN journal 状态作为增量起点。"""
        entries: dict[int, Entry] = {}
        aliases: dict[int, tuple[tuple[int, str], ...]] = {}
        for e in ntfs.enumerate_mft_records(self.drive, progress):
            if e.name is None:
                continue
            entries[e.frn] = Entry(e.parent_frn, e.name, e.is_dir, e.size, e.mtime_ft, e.hidden)
            if e.aliases:
                aliases[e.frn] = e.aliases
        self.entries = entries
        self.aliases = aliases
        self.record_count = len(entries)
        self.invalidate_paths()
        self._lp_cache.clear()
        state = ntfs.query_usn_journal(self.drive)
        self.journal_id = state.journal_id
        self.next_usn = state.next_usn
        self.built_at = time.time()
        self.build_scan()

    def _refresh_from_disk(self, frns: set[int]) -> None:
        """USN 记录不含文件大小:对新建/改过的文件回读磁盘补上。

        不做这一步,所有 USN 期间新建的文件 size 永远是 0——能搜到、但 `size:`
        查询打不中,agent 写文件后拿到的元数据也是错的。

        这里**不能**加上限:USN 检查点在一次刷新里就把积压全部消费掉,被上限
        漏掉的文件之后不会再出现在 USN 里,size 会永久停在 0。代价是一次
        `os.stat`/变更文件,且只在变更发生的那一次刷新里付。
        """
        for frn in frns:
            e = self.entries.get(frn)
            if e is None or e.is_dir:
                continue
            path = self.file_path(frn)
            if path is None:
                continue
            got = self._stat(path)
            if got is None:
                continue
            size, mtime = got
            e.size = size
            if mtime > e.mtime_ft:
                e.mtime_ft = mtime

    def apply_usn_records(self, records: list[ntfs.UsnRecord], next_usn: int) -> int:
        """回放 USN 变更 → 返回受影响条数。目录改名靠 FRN 树天然保持子孙路径正确。"""
        changed = 0
        touched: set[int] = set()
        need_size: set[int] = set()
        for r in records:
            reason = r.reason
            if reason & ntfs.USN_REASON_FILE_DELETE:
                if self.entries.pop(r.frn, None) is not None:
                    changed += 1
                self.aliases.pop(r.frn, None)
                touched.add(r.frn)
            elif reason & ntfs.USN_REASON_RENAME_NEW_NAME:
                old = self.entries.get(r.frn)
                is_dir = bool(r.attributes & ntfs.FILE_ATTRIBUTE_DIRECTORY) if old is None else old.is_dir
                if is_dir:
                    # 目录改名:整棵子树的缓存路径作废(树 key 仍然是记录号)
                    self.aliases.pop(r.frn, None)
                    self.invalidate_paths()
                    self._lp_cache.clear()
                self.entries[r.frn] = Entry(
                    r.parent_frn, r.name, is_dir, old.size if old else 0, r.timestamp_ft,
                    old.hidden if old else bool(r.attributes & ntfs.FILE_ATTRIBUTE_HIDDEN),
                )
                touched.add(r.frn)
                changed += 1
            elif reason & ntfs.USN_REASON_FILE_CREATE:
                is_dir = bool(r.attributes & ntfs.FILE_ATTRIBUTE_DIRECTORY)
                self.entries[r.frn] = Entry(
                    r.parent_frn, r.name, is_dir, 0, r.timestamp_ft,
                    bool(r.attributes & ntfs.FILE_ATTRIBUTE_HIDDEN),
                )
                touched.add(r.frn)
                if not is_dir:
                    need_size.add(r.frn)
                changed += 1
            elif reason & ntfs.USN_REASON_DATA_CHANGE:
                old = self.entries.get(r.frn)
                if old is not None:
                    old.mtime_ft = r.timestamp_ft
                    touched.add(r.frn)
                    if not old.is_dir:
                        need_size.add(r.frn)
                    changed += 1
            # RENAME_OLD_NAME / CLOSE / 其他 reason 不影响索引
        self.next_usn = next_usn
        if need_size:
            self._refresh_from_disk(need_size)
        if touched:
            self._apply_scan_delta(touched)
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
        self.db_path = db_path or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "everything-ai", "index.sqlite"
        )
        self.volumes = [v.upper() for v in (volumes or _detect_volumes())]
        self.vol_index: dict[str, VolumeIndex] = {v: VolumeIndex(v) for v in self.volumes}
        self.lock = threading.RLock()
        self.status = "empty"  # empty | building | ready | error
        self.status_detail = ""
        self._last_refresh = 0.0
        self.refresh_interval = float(os.environ.get("EVERYTHING_AI_REFRESH_INTERVAL", "1.0"))
        self._ensure_db()

    # -- 持久化 ---------------------------------------------------------------

    def _ensure_db(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        con = sqlite3.connect(self.db_path)
        try:
            ver = con.execute("PRAGMA user_version").fetchone()[0]
            if ver != SCHEMA_VERSION:
                con.executescript(
                    "DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS aliases;"
                )
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            con.execute(
                "CREATE TABLE IF NOT EXISTS files ("
                " volume TEXT NOT NULL, frn INTEGER NOT NULL, parent INTEGER NOT NULL,"
                " name TEXT NOT NULL, is_dir INTEGER NOT NULL, size INTEGER NOT NULL,"
                " mtime INTEGER NOT NULL, hidden INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (volume, frn)) WITHOUT ROWID"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                " volume TEXT PRIMARY KEY, journal_id INTEGER, next_usn INTEGER,"
                " built_at REAL, record_count INTEGER)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS aliases ("
                " volume TEXT NOT NULL, frn INTEGER NOT NULL, parent INTEGER NOT NULL,"
                " name TEXT NOT NULL, PRIMARY KEY (volume, frn, parent, name))"
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
                "INSERT INTO files VALUES (?,?,?,?,?,?,?,?)",
                (
                    (vol.drive, frn, e.parent_frn, e.name, 1 if e.is_dir else 0, e.size, e.mtime_ft, 1 if e.hidden else 0)
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
                ((vol.drive, frn, parent, name) for frn, alist in vol.aliases.items() for parent, name in alist),
            )
            con.commit()
            vol.pending_changes.clear()
        finally:
            con.close()

    def _save_delta(self, vol: VolumeIndex) -> None:
        """只写自上次落盘以来变过的 FRN。

        旧实现是"有变更就 DELETE + 全量重插",三卷 220 万条每次启动要重写整个库
        (实测占掉启动时间的一半以上)。增量写只碰变过的几十~几万行。
        """
        frns = vol.pending_changes
        if not frns:
            return
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("BEGIN")
            rows = []
            gone = []
            for frn in frns:
                e = vol.entries.get(frn)
                if e is None:
                    gone.append((vol.drive, frn))
                else:
                    rows.append(
                        (vol.drive, frn, e.parent_frn, e.name, 1 if e.is_dir else 0, e.size, e.mtime_ft,
                         1 if e.hidden else 0)
                    )
            if gone:
                con.executemany("DELETE FROM files WHERE volume=? AND frn=?", gone)
            if rows:
                con.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)", rows)
            # 别名按 FRN 整体重放(改名会换掉别名集)
            con.executemany("DELETE FROM aliases WHERE volume=? AND frn=?", ((vol.drive, f) for f in frns))
            con.executemany(
                "INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)",
                ((vol.drive, frn, parent, name) for frn in frns for parent, name in vol.aliases.get(frn, ())),
            )
            con.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?,?,?,?)",
                (vol.drive, vol.journal_id, vol.next_usn, vol.built_at, vol.record_count),
            )
            con.commit()
            vol.pending_changes.clear()
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
            for frn, parent, name, is_dir, size, mtime, hidden in con.execute(
                "SELECT frn, parent, name, is_dir, size, mtime, hidden FROM files WHERE volume=?",
                (vol.drive,),
            ):
                entries[frn] = Entry(parent, name, bool(is_dir), size, mtime, bool(hidden))
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
            vol.invalidate_paths()
            vol._lp_cache.clear()
            vol.build_scan()  # 扫描表不持久化,加载后必须重建
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
                        self._save_delta(vol)
                if not loaded:
                    vol.build_from_mft()
                    self._save_volume(vol)
            with self.lock:
                self.status = "ready"
                self.status_detail = ""
                self._last_refresh = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - 状态上报给 MCP 调用方
            with self.lock:
                self.status = "error"
                self.status_detail = f"{type(exc).__name__}: {exc}"

    def refresh_if_stale(self) -> int:
        """查询前按需拉取 USN 增量(节流)。返回总变更数;journal 断档则全量重建。"""
        if self.status != "ready":
            return 0
        now = time.monotonic()
        if self.refresh_interval > 0 and now - self._last_refresh < self.refresh_interval:
            return 0
        with self.lock:
            self._last_refresh = time.monotonic()
            total = 0
            for vol in self.vol_index.values():
                try:
                    changed, need_rebuild = vol.refresh_usn()
                except Exception:  # noqa: BLE001 - 单卷失败不影响其他卷
                    continue
                total += changed
                if need_rebuild:
                    vol.build_from_mft()
                    self._save_volume(vol)
                elif changed:
                    self._save_delta(vol)
            return total

    def rebuild(self, drive: str | None = None) -> None:
        targets = [drive.upper()] if drive else self.volumes
        for d in targets:
            vol = self.vol_index.get(d)
            if vol is None:
                raise ValueError(f"未知卷 {d}: 可用卷 {self.volumes}")
            vol.build_from_mft()
            self._save_volume(vol)

    # -- 状态 -----------------------------------------------------------------

    def index_status(self) -> dict:
        with self.lock:
            vols = []
            for v in self.volumes:
                vi = self.vol_index[v]
                vols.append(
                    {
                        "volume": v + ":",
                        "records": vi.record_count,
                        "rows": len(vi.scan) if vi.scan is not None else 0,
                        "stale_rows": len(vi._scan_dead),
                        "delta_rows": len(vi.delta),
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
