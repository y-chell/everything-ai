# -*- coding: utf-8 -*-
"""ntfs.py 测试:USA fixup / mapping pairs / USN_RECORD 解析(合成 fixture,正例+反例)。"""
import struct

import pytest

from everything_ai import ntfs


def make_file_record(seq=1, flags=1, base_frn=0, attributes=b"", record_size=1024):
    """构造一条合成 MFT 记录(含合法 USA fixup)。

    attributes: 已编码的属性字节流(从 0x14 属性偏移开始)。
    """
    usa_off = 0x30
    usa_count = record_size // 512 + 1  # 每扇区一个 fixup + 校验值
    attr_off = 0x30 + usa_count * 2
    header_len = attr_off
    body = bytearray(record_size)
    body[0:4] = b"FILE"
    struct.pack_into("<HH", body, 4, usa_off, usa_count)
    struct.pack_into("<Q", body, 8, 12345)  # LSN
    struct.pack_into("<H", body, 0x10, seq)
    struct.pack_into("<H", body, 0x12, 1)  # links
    struct.pack_into("<H", body, 0x14, attr_off)
    struct.pack_into("<H", body, 0x16, flags)
    used = header_len + len(attributes) + 8
    struct.pack_into("<I", body, 0x18, used)
    struct.pack_into("<I", body, 0x1C, record_size)
    struct.pack_into("<Q", body, 0x20, base_frn)
    body[attr_off: attr_off + len(attributes)] = attributes
    body[attr_off + len(attributes): attr_off + len(attributes) + 4] = b"\xff\xff\xff\xff"
    # USA:校验值 + 每扇区末 2 字节的原始数据(这里原始数据 = 0)
    struct.pack_into("<H", body, usa_off, 1)  # check value
    # 扇区末 2 字节当前是 1(fixup 占位),USA 中保存"原始值" 0
    for i in range(1, usa_count):
        pos = i * 512 - 2
        struct.pack_into("<H", body, pos, 1)  # fixup 占位
        struct.pack_into("<H", body, usa_off + i * 2, 0)  # 原始值
    return bytes(body)


def _resident_header(attr_type, content: bytes) -> bytes:
    """resident 属性头(Windows ATTRIBUTE_RECORD_HEADER,0x18 字节):
    0x10=ValueLength, 0x14=ValueOffset=0x18。"""
    total = 0x18 + len(content)
    head = struct.pack("<IIBBHHHIH", attr_type, total, 0, 0, 0, 0, 0, len(content), 0x18)
    return head + b"\x00\x00" + content  # 0x16-0x17 reserved


def make_si_attribute(mtime_ft):
    """$STANDARD_INFORMATION(resident):ModificationTime @ content+0x08。"""
    content = struct.pack("<QQQQI", 0, mtime_ft, 0, 0, 0x20)
    return _resident_header(0x10, content)


def make_fn_attribute(parent_frn, name):
    """$FILE_NAME(resident):parent @0, Flags @0x38, EaSize @0x3C, NameLength @0x40, Name @0x42。"""
    name_bytes = name.encode("utf-16-le")
    content = struct.pack("<Q", parent_frn)  # 0x00 parent
    content += b"\x00" * 0x30  # 0x08-0x37 四个时间戳
    content += struct.pack("<I", 0)  # 0x38 flags
    content += struct.pack("<I", 0)  # 0x3C EaSize
    content += struct.pack("<BB", len(name), 1)  # 0x40 NameLength, 0x41 Namespace
    content += name_bytes  # 0x42 name
    return _resident_header(0x30, content)


def make_data_attribute_nonresident(real_size):
    """$DATA(非驻留,0x40 字节头,空 runs):RealSize @ attr+0x30。"""
    attr = struct.pack("<IIBBHHH", 0x80, 0x41, 1, 0, 0, 0, 0)  # non-resident 头 16 字节
    attr += struct.pack("<QQ", 0, 0)  # 0x10 start VCN, 0x18 end VCN
    attr += struct.pack("<HHI", 0x40, 0, 0)  # 0x20 runs offset, 0x22 compression unit, 0x24 padding
    attr += struct.pack("<QQQ", real_size, real_size, real_size)  # 0x28 alloc, 0x30 real, 0x38 init
    attr += b"\x00"  # 0x40 空 runs 终止符
    return attr


class TestUsaFixup:
    def test_valid_record_passes(self):
        rec = bytearray(make_file_record())
        ntfs.usa_fixup(rec, len(rec))  # 不抛异常
        # fixup 后扇区末 2 字节应恢复为 USA 中的原始值 0
        assert struct.unpack_from("<H", rec, 510)[0] == 0
        assert struct.unpack_from("<H", rec, 1022)[0] == 0

    def test_mismatch_raises(self):
        rec = bytearray(make_file_record())
        rec[510:512] = b"\x99\x99"  # 破坏 fixup 占位
        with pytest.raises(ValueError, match="USA fixup"):
            ntfs.usa_fixup(rec, len(rec))

    def test_usa_out_of_bounds_raises(self):
        rec = bytearray(make_file_record())
        struct.pack_into("<HH", rec, 4, 0xFF00, 100)  # USA 越界
        with pytest.raises(ValueError, match="越界"):
            ntfs.usa_fixup(rec, len(rec))


class TestMappingPairs:
    def test_single_run(self):
        # header 0x11: len=1字节 off=1字节;len=0x01,off=0x08 → LCN 8, 1 cluster
        runs = ntfs.parse_mapping_pairs(memoryview(bytes([0x11, 0x01, 0x08, 0x00])), 4096)
        assert runs == [(8 * 4096, 1 * 4096)]

    def test_multi_run_relative_offsets(self):
        # header 低 4 位=len 字节数、高 4 位=off 字节数(Everything 反编译:v44=*v40&0xF, v43=*v40>>4)
        # (LCN 100, 2簇) → (LCN 101, 1簇) → (LCN 99, 1簇);偏移为相对前段的有符号值
        data = bytes([0x11, 0x02, 0x64, 0x11, 0x01, 0x01, 0x11, 0x01, 0xFE, 0x00])
        runs = ntfs.parse_mapping_pairs(memoryview(data), 4096)
        assert runs == [(100 * 4096, 2 * 4096), (101 * 4096, 1 * 4096), (99 * 4096, 1 * 4096)]

    def test_sparse_run_skipped(self):
        # header 0x01: len=1字节 off=0字节 → sparse run,无物理段
        data = bytes([0x01, 0x05, 0x00])
        runs = ntfs.parse_mapping_pairs(memoryview(data), 4096)
        assert runs == []

    def test_bad_header_raises(self):
        with pytest.raises(ValueError, match="header"):
            ntfs.parse_mapping_pairs(memoryview(bytes([0x0F, 0x00])), 4096)  # len_size=0

    def test_truncated_raises(self):
        with pytest.raises(ValueError, match="越界"):
            ntfs.parse_mapping_pairs(memoryview(bytes([0x21, 0x02])), 4096)  # 声明 2 字节但只有 1


class TestFileRecordParsing:
    def test_full_record(self):
        attrs = make_si_attribute(134_500_000_000_000_000) + make_fn_attribute(0x5000000000005, "hello.txt") + make_data_attribute_nonresident(4096)
        rec = make_file_record(seq=7, flags=1, attributes=attrs)
        entry = ntfs._parse_file_record((100 << 48) | 7, rec)
        assert entry is not None
        assert entry.name == "hello.txt"
        assert entry.parent_frn == 5  # parent 只保留记录号(低 48 位)
        assert entry.size == 4096
        assert entry.mtime_ft == 134_500_000_000_000_000
        assert entry.is_dir is False
        assert entry.base_frn == 0

    def test_directory_flag(self):
        attrs = make_fn_attribute(5, "Docs")
        rec = make_file_record(flags=0x03, attributes=attrs)  # in-use + directory
        entry = ntfs._parse_file_record((50 << 48) | 1, rec)
        assert entry.is_dir is True

    def test_bad_magic_returns_none(self):
        rec = bytearray(make_file_record())
        rec[0:4] = b"BAAD"
        assert ntfs._parse_file_record(1, bytes(rec)) is None

    def test_broken_fixup_returns_none(self):
        rec = bytearray(make_file_record())
        rec[510:512] = b"\x99\x99"
        assert ntfs._parse_file_record(1, bytes(rec)) is None

    def test_extended_record_base_frn(self):
        attrs = make_fn_attribute(0x5000000000005, "ext.txt")
        rec = make_file_record(flags=1, base_frn=0x1234, attributes=attrs)
        entry = ntfs._parse_file_record((200 << 48) | 2, rec)
        assert entry.base_frn == 0x1234


class TestUsnRecordParsing:
    def make_usn_buffer(self, records):
        body = b""
        for frn, parent, name, reason, ts, attrs in records:
            name_bytes = name.encode("utf-16-le")
            rec_len = 60 + len(name_bytes)
            body += struct.pack(
                "<IHHQQqQIIIIHH", rec_len, 2, 0, frn, parent, 0, ts, reason, 0, 0, attrs,
                len(name_bytes), 60,
            )
            body += name_bytes
        return struct.pack("<Q", 999) + body  # NextUsn 头

    def test_parse_single(self):
        buf = self.make_usn_buffer([(0x1A2B, 0x5000000000005, "new.txt", 0x80002100, 134_000_000_000, 0x20)])
        recs = ntfs.parse_usn_records(buf, len(buf))
        assert len(recs) == 1
        r = recs[0]
        assert r.frn == 0x1A2B
        assert r.parent_frn == 5  # 只保留记录号(低 48 位)
        assert r.name == "new.txt"
        assert r.reason == 0x80002100
        assert r.attributes == 0x20

    def test_parse_multiple_and_truncated_tail(self):
        buf = self.make_usn_buffer(
            [
                (1, 5, "a.txt", 0x100, 1, 0),
                (2, 5, "b.txt", 0x200, 2, 0),
            ]
        )
        # 截断最后 10 字节 → 第二条不完整,应只解析出第一条
        recs = ntfs.parse_usn_records(buf, len(buf) - 10)
        assert len(recs) == 1
        assert recs[0].name == "a.txt"

    def test_empty(self):
        buf = struct.pack("<Q", 123)
        assert ntfs.parse_usn_records(buf, len(buf)) == []
