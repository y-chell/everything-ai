# -*- coding: utf-8 -*-
"""index.py 测试:USN 回放语义、路径解析、FRN 树一致性(合成数据,不碰真实卷)。"""
import pytest

from everything_ai import ntfs
from everything_ai.index import Entry, FileIndex, VolumeIndex, _parse_excluded_prefixes
from everything_ai.rank import FLAG_DIR


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
    vol.build_scan()
    return vol


class TestPaths:
    def test_dir_paths(self):
        vol = seeded_volume()
        # 目录路径带尾部反斜杠
        assert vol.dir_path(10) == "C:\\docs\\"
        assert vol.dir_path(12) == "C:\\docs\\sub\\"
        assert vol.dir_path(5) == "C:\\"

    def test_file_paths(self):
        vol = seeded_volume()
        assert vol.file_path(11) == "C:\\docs\\a.txt"
        assert vol.file_path(13) == "C:\\docs\\sub\\b.txt"

    def test_resolve_lazy_after_invalidate(self):
        vol = seeded_volume()
        vol.invalidate_paths()
        assert vol.dir_paths == {}
        assert vol.file_path(13) == "C:\\docs\\sub\\b.txt"
        # 解析后链上每一级都进了缓存
        assert vol.dir_paths[10] == "C:\\docs\\"
        assert vol.dir_paths[12] == "C:\\docs\\sub\\"

    def test_orphan_returns_none(self):
        vol = seeded_volume()
        vol.entries[99] = Entry(12345, "orphan.txt", False, 0, 1)
        assert vol.file_path(99) is None

    def test_scan_rows_cover_dirs_and_files(self):
        vol = seeded_volume()
        scan = vol.scan
        assert len(scan) == 4
        assert sorted(scan.names) == ["a.txt", "b.txt", "docs", "sub"]
        by_name = {scan.names[i]: i for i in range(len(scan))}
        assert scan.flags[by_name["docs"]] & FLAG_DIR
        assert not scan.flags[by_name["a.txt"]] & FLAG_DIR


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


class TestPersistence:
    def test_same_name_aliases_survive_round_trip(self, tmp_path):
        """同一 FRN 下同名、不同父目录的硬链接别名不能被落盘时吞掉。

        旧 schema 的 aliases 主键是 (volume, frn, name),同名别名会塌成一条,
        冷建索引 274 万行、重启后只剩 239 万行——查询结果随"是否重启"而变。
        """
        db = str(tmp_path / "idx.sqlite")
        idx = FileIndex(db_path=db, volumes=["C"])
        vol = idx.vol_index["C"]
        vol.entries = {
            10: Entry(5, "d1", True, 0, 1),
            11: Entry(5, "d2", True, 0, 1),
            13: Entry(5, "d3", True, 0, 1),
            12: Entry(13, "same.py", False, 5, 2),
        }
        vol.aliases = {12: ((10, "same.py"), (11, "same.py"))}
        vol.journal_id, vol.next_usn, vol.built_at, vol.record_count = 1, 2, 3.0, 4
        idx._save_volume(vol)

        idx2 = FileIndex(db_path=db, volumes=["C"])
        assert idx2._load_volume(idx2.vol_index["C"]) is True
        loaded = idx2.vol_index["C"]
        assert sorted(loaded.aliases[12]) == [(10, "same.py"), (11, "same.py")]

        paths = {
            loaded.file_path(12),
            loaded.dir_path(10) + "same.py",
            loaded.dir_path(11) + "same.py",
        }
        assert paths == {"C:\\d3\\same.py", "C:\\d1\\same.py", "C:\\d2\\same.py"}

    def test_hidden_flag_survives_round_trip(self, tmp_path):
        db = str(tmp_path / "idx.sqlite")
        idx = FileIndex(db_path=db, volumes=["C"])
        vol = idx.vol_index["C"]
        vol.entries = {10: Entry(5, "d", True, 0, 1), 11: Entry(10, "hidden.txt", False, 3, 2, hidden=True)}
        vol.journal_id, vol.next_usn, vol.built_at, vol.record_count = 1, 2, 3.0, 2
        idx._save_volume(vol)

        idx2 = FileIndex(db_path=db, volumes=["C"])
        assert idx2._load_volume(idx2.vol_index["C"]) is True
        assert idx2.vol_index["C"].entries[11].hidden is True

    def test_delta_save_round_trip(self, tmp_path):
        """USN 变更要增量落盘:重启后新建的能找到、删掉的消失、检查点前进。"""
        db = str(tmp_path / "idx.sqlite")
        idx = FileIndex(db_path=db, volumes=["C"])
        vol = idx.vol_index["C"]
        vol.entries = {10: Entry(5, "d", True, 0, 1), 11: Entry(10, "a.txt", False, 5, 2)}
        vol.journal_id, vol.next_usn, vol.built_at, vol.record_count = 1, 100, 3.0, 2
        vol.build_scan()
        idx._save_volume(vol)

        vol.apply_usn_records([make_usn_record(12, 10, "b.txt", ntfs.USN_REASON_FILE_CREATE, ts=500)], 200)
        vol.apply_usn_records([make_usn_record(11, 10, "a.txt", ntfs.USN_REASON_FILE_DELETE, ts=500)], 300)
        assert vol.pending_changes == {11, 12}
        idx._save_delta(vol)
        assert vol.pending_changes == set()

        idx2 = FileIndex(db_path=db, volumes=["C"])
        assert idx2._load_volume(idx2.vol_index["C"]) is True
        loaded = idx2.vol_index["C"]
        assert 11 not in loaded.entries
        assert loaded.entries[12].name == "b.txt"
        assert loaded.next_usn == 300
