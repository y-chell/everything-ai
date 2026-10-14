# -*- coding: utf-8 -*-
"""index.py 测试:USN 回放语义、路径解析、FRN 树一致性(合成数据,不碰真实卷)。"""
import pytest

from everything_ai import ntfs
from everything_ai.index import ROOT_FRN, Entry, VolumeIndex, _parse_excluded_prefixes


def make_usn_record(frn, parent, name, reason, ts=1000, attrs=0x20):
    return ntfs.UsnRecord(frn, parent, name, reason, ts, attrs)


def seeded_volume():
    vol = VolumeIndex("C")
    # 结构: root(5) → docs(10, dir) → a.txt(11) / sub(12, dir) → b.txt(13)
    vol.entries = {
        10: Entry(5, "docs", True, 0, 100),
        11: Entry(10, "a.txt", False, 5, 101),
        12: Entry(10, "sub", True, 0, 102),
        13: Entry(12, "b.txt", False, 7, 103),
    }
    vol._rebuild_paths()
    return vol


class TestPaths:
    def test_rebuild_paths(self):
        vol = seeded_volume()
        assert vol.paths[11] == "C:\\docs\\a.txt"
        assert vol.paths[13] == "C:\\docs\\sub\\b.txt"
        assert vol.paths[12] == "C:\\docs\\sub\\"

    def test_resolve_lazy(self):
        vol = seeded_volume()
        vol.paths.pop(13)
        assert vol.resolve_path(13) == "C:\\docs\\sub\\b.txt"
        # 缓存回填
        assert vol.paths[13] == "C:\\docs\\sub\\b.txt"

    def test_orphan_returns_none(self):
        vol = seeded_volume()
        vol.entries[99] = Entry(12345, "orphan.txt", False, 0, 1)
        assert vol.resolve_path(99) is None


class TestUsnReplay:
    def test_file_create(self):
        vol = seeded_volume()
        changed = vol.apply_usn_records(
            [make_usn_record(14, 10, "c.txt", ntfs.USN_REASON_FILE_CREATE, ts=200)], 2000
        )
        assert changed == 1
        assert vol.entries[14].name == "c.txt"
        assert vol.entries[14].parent_frn == 10
        assert vol.next_usn == 2000

    def test_file_delete(self):
        vol = seeded_volume()
        vol.apply_usn_records([make_usn_record(11, 10, "a.txt", ntfs.USN_REASON_FILE_DELETE)], 2000)
        assert 11 not in vol.entries

    def test_rename_updates_name_and_parent(self):
        vol = seeded_volume()
        vol.apply_usn_records(
            [make_usn_record(11, 5, "moved.txt", ntfs.USN_REASON_RENAME_NEW_NAME, ts=300)], 2000
        )
        e = vol.entries[11]
        assert e.name == "moved.txt"
        assert e.parent_frn == 5
        assert e.size == 5  # 旧 size 保留
        assert e.mtime_ft == 300

    def test_directory_rename_keeps_children_frn_tree(self):
        """FRN 树设计核心:目录改名后子孙条目不变,路径解析自动跟随。"""
        vol = seeded_volume()
        vol.apply_usn_records(
            [make_usn_record(10, 5, "documents", ntfs.USN_REASON_RENAME_NEW_NAME, ts=400)], 2000
        )
        assert vol.entries[13].parent_frn == 12  # 子孙 FRN 链不变
        # 路径表已失效,惰性解析得到新路径
        assert vol.resolve_path(13) == "C:\\documents\\sub\\b.txt"

    def test_data_change_updates_mtime_only(self):
        vol = seeded_volume()
        vol.apply_usn_records(
            [make_usn_record(11, 10, "a.txt", ntfs.USN_REASON_DATA_CHANGE, ts=500)], 2000
        )
        e = vol.entries[11]
        assert e.mtime_ft == 500
        assert e.size == 5  # USN 不带新 size,保持旧值

    def test_close_reason_ignored(self):
        vol = seeded_volume()
        changed = vol.apply_usn_records(
            [make_usn_record(11, 10, "a.txt", ntfs.USN_REASON_CLOSE)], 2000
        )
        assert changed == 0

    def test_delete_then_create_same_frn(self):
        vol = seeded_volume()
        vol.apply_usn_records([make_usn_record(11, 10, "a.txt", ntfs.USN_REASON_FILE_DELETE)], 1500)
        vol.apply_usn_records(
            [make_usn_record(11, 12, "reborn.txt", ntfs.USN_REASON_FILE_CREATE, ts=1600)], 1600
        )
        assert vol.entries[11].name == "reborn.txt"
        assert vol.entries[11].parent_frn == 12


class TestExcludedPrefixes:
    def test_parse_prenormalized(self):
        # 正例:分号分隔,大小写归一,去空白(含尾部反斜杠前缀)
        assert _parse_excluded_prefixes("C:\\Foo\\Bar; D:\\Baz\\") == ("c:\\foo\\bar", "d:\\baz\\")

    def test_parse_empty_and_blank_segments(self):
        # 反例:空串与仅空段的输入 → 无排除
        assert _parse_excluded_prefixes("") == ()
        assert _parse_excluded_prefixes(";;   ;;") == ()
