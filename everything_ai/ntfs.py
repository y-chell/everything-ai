r"""NTFS 底层引擎:卷设备读取、$MFT 解析、USN Journal。

机制逆向自 Everything 1.4.1.1032(见 work/20261014-161930-*/everything-mechanism-notes.md):
- 全量索引:读 boot sector 定位 $MFT → 解析 $DATA mapping pairs → 顺序读 $MFT 流 →
  USA fixup → 属性遍历($FILE_NAME / $STANDARD_INFORMATION / $DATA)
- 增量更新:FSCTL_READ_USN_JOURNAL 拉取变更;journal 过期(1177-1179/1181)触发全量重建

需要管理员权限(读 \\.\X: 卷设备)。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
kernel32.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]

GENERIC_READ = 0x80000000
FILE_SHARE_READ_WRITE_DELETE = 1 | 2 | 4
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wt.HANDLE(-1)

FSCTL_READ_USN_JOURNAL = 0x900BB
FSCTL_QUERY_USN_JOURNAL = 0x900F4

ERROR_HANDLE_EOF = 38
ERROR_JOURNAL_NOT_ACTIVE = 1171
ERROR_JOURNAL_DELETE_IN_PROGRESS = 1178
ERROR_JOURNAL_ENTRY_DELETED = 1181
JOURNAL_INVALID_ERRORS = {1177, 1178, 1179, 1181}

# USN reason flags(winioctl.h)
USN_REASON_DATA_CHANGE = 0x00000057  # overwrite|extend|truncation + named variants
USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000
USN_REASON_CLOSE = 0x80000000

FILE_ATTRIBUTE_DIRECTORY = 0x10

FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def filetime_to_datetime(ft: int) -> datetime | None:
    if not ft:
        return None
    try:
        return FILETIME_EPOCH + timedelta(microseconds=ft // 10)
    except OverflowError:
        return None


class NtfsError(OSError):
    """NTFS 引擎错误,带 Windows 错误码上下文。"""


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def open_volume(drive: str) -> int:
    r"""打开卷设备句柄(如 drive='C' → \\.\C:)。需要管理员权限。"""
    h = kernel32.CreateFileW(
        f"\\\\.\\{drive}:", GENERIC_READ, FILE_SHARE_READ_WRITE_DELETE,
        None, OPEN_EXISTING, 0, None,
    )
    if h == INVALID_HANDLE_VALUE or not h:
        raise NtfsError(f"打开卷 \\\\.\\{drive}: 失败 (err={ctypes.get_last_error()});需要管理员权限")
    return h


def _read_at(h: int, offset: int, size: int) -> bytes:
    """卷设备 ReadFile 要求扇区对齐:向上对齐读取后截断。"""
    aligned = (size + 511) // 512 * 512
    buf = ctypes.create_string_buffer(aligned)
    if kernel32.SetFilePointerEx(h, offset, None, 0) == 0xFFFFFFFFFFFFFFFF:
        raise NtfsError(f"SetFilePointer({offset:#x}) 失败 err={ctypes.get_last_error()}")
    br = wt.DWORD(0)
    if not kernel32.ReadFile(h, buf, aligned, ctypes.byref(br), None) or br.value != aligned:
        raise NtfsError(f"读卷 offset={offset:#x} size={aligned} 失败: got {br.value}, err={ctypes.get_last_error()}")
    return buf.raw[:size]


@dataclass(frozen=True)
class VolumeInfo:
    drive: str
    bytes_per_cluster: int
    mft_lcn: int
    bytes_per_record: int
    serial_number: int


def read_volume_info(h: int, drive: str) -> VolumeInfo:
    """解析 NTFS boot sector(逆向参照:everything.exe sub_1400CFA70)。"""
    bs = _read_at(h, 0, 512)
    if bs[3:7] != b"NTFS":
        raise NtfsError(f"{drive}: 不是 NTFS 卷(签名 {bs[3:7]!r})")
    bps = struct.unpack_from("<H", bs, 0x0B)[0]
    spc = struct.unpack_from("<b", bs, 0x0D)[0]
    bytes_per_cluster = bps * spc if spc > 0 else 1 << -spc
    mft_lcn = struct.unpack_from("<Q", bs, 0x30)[0]
    rec_size = struct.unpack_from("<b", bs, 0x40)[0]  # 带符号单字节,负值 n → 2^-n
    bytes_per_record = rec_size if rec_size > 0 else 1 << -rec_size
    serial = struct.unpack_from("<Q", bs, 0x48)[0]
    if not bytes_per_cluster or not bytes_per_record:
        raise NtfsError(f"{drive}: boot sector 字段异常 (bpc={bytes_per_cluster} bpr={bytes_per_record})")
    return VolumeInfo(drive, bytes_per_cluster, mft_lcn, bytes_per_record, serial)


def usa_fixup(record: bytearray, record_size: int) -> None:
    """Update Sequence Array fixup:每个扇区末 2 字节是校验值,替换回 USA 保存的原始数据。

    逆向参照:everything.exe ntfs_enum_mft 内 'bad US %x != %x' 分支。
    """
    usa_off, usa_count = struct.unpack_from("<HH", record, 4)
    if usa_count <= 1:
        return
    if usa_off + usa_count * 2 > len(record):
        raise ValueError(f"USA 越界: off={usa_off} count={usa_count}")
    check = record[usa_off: usa_off + 2]
    for i in range(1, usa_count):
        pos = i * 512 - 2
        if pos + 2 > len(record):
            raise ValueError(f"USA fixup 位置越界: {pos}")
        if record[pos: pos + 2] != check:
            raise ValueError(f"USA fixup 不匹配 @ {pos}: {record[pos:pos+2].hex()} != {check.hex()}")
        record[pos: pos + 2] = record[usa_off + i * 2: usa_off + i * 2 + 2]


def parse_mapping_pairs(data: memoryview, bytes_per_cluster: int) -> list[tuple[int, int]]:
    """解析非驻留属性的 data runs → [(byte_offset, byte_length), ...]。

    每段首字节:低 4 位=长度字节数,高 4 位=偏移字节数;偏移有符号(稀疏)。
    """
    runs: list[tuple[int, int]] = []
    lcn = 0
    i = 0
    while i < len(data) and data[i] != 0:
        header = data[i]
        len_size = header & 0xF
        off_size = header >> 4
        i += 1
        if not 1 <= len_size <= 8 or off_size > 8:
            raise ValueError(f"非法 mapping pair header {header:#x}")
        if i + len_size > len(data):
            raise ValueError("mapping pair 长度字段越界")
        run_len = int.from_bytes(data[i: i + len_size], "little")
        i += len_size
        if off_size:
            if i + off_size > len(data):
                raise ValueError("mapping pair 偏移字段越界")
            run_off = int.from_bytes(data[i: i + off_size], "little", signed=True)
            i += off_size
            lcn += run_off
            runs.append((lcn * bytes_per_cluster, run_len * bytes_per_cluster))
        # off_size == 0 → sparse run,无物理位置,跳过
    return runs


def _iter_attributes(record: bytes) -> Iterator[tuple[int, int, memoryview]]:
    """遍历记录内属性 → (type, total_len, attr_view)。"""
    attr_off = struct.unpack_from("<H", record, 0x14)[0]
    n = len(record)
    while attr_off + 0x10 <= n:
        attr_type = struct.unpack_from("<I", record, attr_off)[0]
        if attr_type == 0xFFFFFFFF:
            return
        attr_len = struct.unpack_from("<I", record, attr_off + 4)[0]
        if attr_len < 0x10 or attr_off + attr_len > n:
            return
        yield attr_type, attr_len, memoryview(record)[attr_off: attr_off + attr_len]
        attr_off += attr_len


@dataclass
class MftEntry:
    frn: int            # MFT 记录号(低 48 位)
    parent_frn: int
    name: str | None    # 主名($FILE_NAME,优先 Win32);None = 无名(元数据文件等)
    is_dir: bool
    size: int
    mtime_ft: int       # $STANDARD_INFORMATION.ModificationTime (FILETIME)
    base_frn: int       # 非 0 = attribute list 扩展记录,指向 base 记录
    aliases: tuple[tuple[int, str], ...] = ()  # 其余 $FN(硬链接),(parent_recnum, name)
    name_ns: int = -1   # 主名 namespace(0=POSIX 1=Win32 2=DOS 3=Win32+DOS)


def _parse_file_record(frn: int, raw: bytes) -> MftEntry | None:
    """解析单条 MFT 记录。坏记录返回 None(不中断全量枚举)。

    $FILE_NAME 可有多条(Win32 长名 + DOS 8.3 短名),按 namespace 优先取
    Win32(1/3),纯 DOS(2)仅在没有更好名字时使用。
    """
    if raw[0:4] != b"FILE":
        return None
    rec = bytearray(raw)
    try:
        usa_fixup(rec, len(rec))
    except ValueError:
        return None
    flags = struct.unpack_from("<H", rec, 0x16)[0]
    base_frn = struct.unpack_from("<Q", rec, 0x20)[0] & 0x0000FFFFFFFFFFFF  # 只取记录号(序号会漂移)
    name: str | None = None
    name_ns = -1
    parent = 0
    size = 0
    mtime = 0
    fn_list: list[tuple[int, int, str]] = []  # (parent_recnum, ns, name)
    for attr_type, _attr_len, attr in _iter_attributes(bytes(rec)):
        non_resident = attr[8] & 1
        if attr_type == 0x30:  # $FILE_NAME(可有多个:硬链接 / Win32+DOS)
            content_off = struct.unpack_from("<H", attr, 0x14)[0]
            content = attr[content_off:]
            name_len = content[0x40]
            ns = content[0x41]
            if not (0 < name_len <= 255 and 0x42 + name_len * 2 <= len(content)):
                continue
            cand = content[0x42: 0x42 + name_len * 2].tobytes().decode("utf-16-le", errors="replace")
            fn_list.append((struct.unpack_from("<Q", content, 0)[0] & 0x0000FFFFFFFFFFFF, ns, cand))
        elif attr_type == 0x10 and not mtime:  # $STANDARD_INFORMATION
            content_off = struct.unpack_from("<H", attr, 0x14)[0]
            mtime = struct.unpack_from("<Q", attr, content_off + 0x08)[0]  # LastModificationTime
        elif attr_type == 0x80 and not size and attr[9] == 0:
            # 只取 unnamed 主数据流(namelen==0);named 流(如 Zone.Identifier)不是文件大小。
            # 驻留:ValueLength@0x10 就是实际字节数;非驻留:RealSize@0x30
            size = struct.unpack_from("<H", attr, 0x10)[0] if not non_resident else struct.unpack_from("<Q", attr, 0x30)[0]
    aliases: list[tuple[int, str]] = []
    for prent, ns, cand in fn_list:
        if name is None or (name_ns == 2 and ns != 2):
            if name is not None and name_ns != 2:
                # 被替换的旧主名进 alias;DOS 短名直接丢弃
                aliases.append((parent, name))
            name = cand
            name_ns = ns
            parent = prent
        elif ns != 2 and (prent, cand) != (parent, name):
            # 硬链接同名不同目录:按 (parent, name) 对去重,不能只比名字
            aliases.append((prent, cand))
    return MftEntry(frn, parent, name, bool(flags & 2), size, mtime, base_frn, tuple(aliases), name_ns)


def _merge_extended(ext: MftEntry, base: MftEntry) -> None:
    """把 attribute list 扩展记录的属性合并进 base(双向,支持任意出现顺序)。"""
    if base.name is None and ext.name is not None:
        base.name = ext.name
        base.name_ns = ext.name_ns
        base.parent_frn = ext.parent_frn
    elif ext.name is not None and (ext.parent_frn, ext.name) != (base.parent_frn, base.name):
        if ext.name_ns != 2 and base.name_ns == 2:
            # base 主名是 DOS 短名,扩展记录有更好的名字 → 直接替换(旧短名丢弃)
            base.name = ext.name
            base.name_ns = ext.name_ns
            base.parent_frn = ext.parent_frn
        elif ext.name_ns != 2:
            base.aliases = base.aliases + ((ext.parent_frn, ext.name),)
    # 扩展记录自身的 aliases(硬链接 $FN 可能分布在 base 与扩展两条记录里)
    existing = {(base.parent_frn, base.name)} | set(base.aliases)
    for p, n in ext.aliases:
        if n and ns_ok(n) and (p, n) not in existing:
            base.aliases = base.aliases + ((p, n),)
            existing.add((p, n))
    if ext.size > base.size:
        base.size = ext.size
    if ext.mtime_ft > base.mtime_ft:
        base.mtime_ft = ext.mtime_ft


def ns_ok(name: str) -> bool:
    """过滤 DOS 8.3 短名形式(含 ~ 的生成名)。"""
    return "~" not in name


def enumerate_mft_records(drive: str, progress: Callable[[int], None] | None = None) -> list[MftEntry]:
    """全量枚举卷上所有 MFT 记录(Everything ntfs_enum_mft 同款路线)。

    FRN = MFT 记录号(低 48 位;高 16 位序号会随记录重用漂移,不参与索引)。
    attribute list 扩展记录(base_frn != 0)的 $FILE_NAME/$DATA 合并进 base 条目。
    硬链接的多个 $FILE_NAME:主名之外进 aliases。
    """
    h = open_volume(drive)
    try:
        vol = read_volume_info(h, drive)
        rec0 = _read_at(h, vol.mft_lcn * vol.bytes_per_cluster, vol.bytes_per_record)
        if rec0[0:4] != b"FILE":
            raise NtfsError(f"{drive}: $MFT record 0 魔数异常")
        rec0ba = bytearray(rec0)
        usa_fixup(rec0ba, len(rec0ba))
        runs = []
        for attr_type, attr_len, attr in _iter_attributes(bytes(rec0ba)):
            if attr_type == 0x80 and attr[8] & 1:
                runs_off = struct.unpack_from("<H", attr, 0x20)[0]
                runs = parse_mapping_pairs(attr[runs_off: attr_len], vol.bytes_per_cluster)
                break
        if not runs:
            raise NtfsError(f"{drive}: $MFT $DATA 无有效 data runs")

        entries: list[MftEntry] = []
        by_frn: dict[int, MftEntry] = {}
        pending_ext: dict[int, list[MftEntry]] = {}  # base_frn → 先于 base 出现的扩展记录
        record_number = 0
        CHUNK = 1 << 22
        carry = b""
        for seg_off, seg_len in runs:
            pos, end = seg_off, seg_off + seg_len
            while pos < end:
                n = min(CHUNK, end - pos)
                data = carry + _read_at(h, pos, n)
                pos += n
                nrec = len(data) // vol.bytes_per_record
                carry = data[nrec * vol.bytes_per_record:]
                for i in range(nrec):
                    base_raw = data[i * vol.bytes_per_record: (i + 1) * vol.bytes_per_record]
                    recnum = record_number  # MFT 记录号 = 流内位置索引,从 0 开始
                    record_number += 1
                    if recnum <= 15:
                        # 记录 0-15 是固定元数据($MFT/$LogFile/$Bitmap/$Boot 等),Everything 同样不索引
                        continue
                    # FRN 只用记录号(低 48 位):目录重建后序号(高 16 位)会漂移,
                    # 子项 $FN 里存的父序号是旧值,拼完整 64 位会导致树断裂
                    entry = _parse_file_record(recnum, base_raw)
                    if entry is None:
                        continue
                    if entry.base_frn:
                        # 扩展记录:base 可能已出现(直接合并)或尚未出现(暂存)
                        base = by_frn.get(entry.base_frn)
                        if base is not None:
                            _merge_extended(entry, base)
                        else:
                            pending_ext.setdefault(entry.base_frn, []).append(entry)
                        continue
                    for ext in pending_ext.pop(recnum, ()):
                        _merge_extended(ext, entry)
                    by_frn[recnum] = entry
                    entries.append(entry)
                    if progress and len(entries) % 65536 == 0:
                        progress(len(entries))
        return entries
    finally:
        kernel32.CloseHandle(h)


# ---------------------------------------------------------------------------
# USN Journal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsnJournalState:
    journal_id: int
    next_usn: int
    first_usn: int


@dataclass(frozen=True)
class UsnRecord:
    frn: int
    parent_frn: int
    name: str
    reason: int
    timestamp_ft: int
    attributes: int


def query_usn_journal(drive: str) -> UsnJournalState:
    h = open_volume(drive)
    try:
        buf = ctypes.create_string_buffer(64)
        br = wt.DWORD(0)
        if not kernel32.DeviceIoControl(h, FSCTL_QUERY_USN_JOURNAL, None, 0, buf, 64, ctypes.byref(br), None):
            raise NtfsError(f"{drive}: FSCTL_QUERY_USN_JOURNAL 失败 err={ctypes.get_last_error()}")
        journal_id, next_usn, first_usn = struct.unpack_from("<QQQ", buf, 0)
        return UsnJournalState(journal_id, next_usn, first_usn)
    finally:
        kernel32.CloseHandle(h)


def parse_usn_records(data: bytes, count: int) -> list[UsnRecord]:
    """解析 FSCTL_READ_USN_JOURNAL 输出:首个 QWORD=NextUsn,后接连续 USN_RECORD_V2。"""
    recs = []
    off = 8
    end = count
    while off + 64 <= end:
        rec_len, major, minor = struct.unpack_from("<IHH", data, off)
        if rec_len < 60 or off + rec_len > end:
            break
        frn, parent_frn, _usn, ts, reason = struct.unpack_from("<QQqQI", data, off + 8)
        attrs, name_len, name_off = struct.unpack_from("<IHH", data, off + 52)
        name = data[off + name_off: off + name_off + name_len].decode("utf-16-le", errors="replace")
        # 只保留记录号(低 48 位),与 MFT 枚举的 FRN 语义一致(序号会漂移)
        recs.append(UsnRecord(frn & 0x0000FFFFFFFFFFFF, parent_frn & 0x0000FFFFFFFFFFFF, name, reason, ts, attrs))
        off += rec_len
    return recs


def read_usn_changes(drive: str, journal_id: int, from_usn: int, timeout_ms: int = 0) -> tuple[int, list[UsnRecord]]:
    """读取 from_usn 起的全部 USN 变更 → (next_usn, records)。

    journal 过期(1177-1179/1181)抛 NtfsError,由上层触发全量重建。
    """
    h = open_volume(drive)
    try:
        # READ_USN_JOURNAL_DATA_V0: StartUsn, ReasonMask, ReturnOnlyOnClose, Timeout, BytesToWaitFor, UsnJournalId
        in_buf = struct.pack("<qIIQQQ", from_usn, 0xFFFFFFFF, 0, timeout_ms * 10000, 0, journal_id)
        records: list[UsnRecord] = []
        usn = from_usn
        buf = ctypes.create_string_buffer(1 << 20)
        while True:
            br = wt.DWORD(0)
            ok = kernel32.DeviceIoControl(h, FSCTL_READ_USN_JOURNAL, in_buf, len(in_buf), buf, len(buf), ctypes.byref(br), None)
            if not ok:
                err = ctypes.get_last_error()
                if err in JOURNAL_INVALID_ERRORS:
                    raise NtfsError(f"{drive}: USN journal 过期 (err={err}),需全量重建")
                raise NtfsError(f"{drive}: FSCTL_READ_USN_JOURNAL 失败 err={err}")
            next_usn = struct.unpack_from("<Q", buf.raw, 0)[0]
            recs = parse_usn_records(buf.raw, br.value)
            records.extend(recs)
            if next_usn == usn or not recs:
                return next_usn, records
            usn = next_usn
            in_buf = struct.pack("<qIIQQQ", usn, 0xFFFFFFFF, 0, timeout_ms * 10000, 0, journal_id)
    finally:
        kernel32.CloseHandle(h)
