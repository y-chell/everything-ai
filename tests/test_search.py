# -*- coding: utf-8 -*-
"""search.py 测试:分阶段求值必须与逐条 matcher 的暴力实现结果一致。

这是本次优化的核心护栏——分阶段过滤把条件拆开、改变了求值顺序,
任何一级拆错(尤其是通配符的字面片段预筛、``!词`` 的排除路径、
路径条件)都会在这里暴露。
"""
from __future__ import annotations

import time

import pytest

from everything_ai.index import Entry, VolumeIndex
from everything_ai.query import compile_plan, compile_query
from everything_ai.rank import FLAG_DIR
from everything_ai.search import run_search

from everything_ai.query import _row_path

NOW_FT = int((time.time() + 11644473600) * 10_000_000)
OLD_FT = 1_300_000_000 * 10_000_000  # 2011-03-13,早于 FIRST_KNOWN_FT?


class FakeIndex:
    """run_search 只用到 vol_index,测试里不需要真的 SQLite。"""

    def __init__(self, *vols: VolumeIndex):
        self.vol_index = {v.drive: v for v in vols}


def build_volume() -> VolumeIndex:
    vol = VolumeIndex("C")
    vol.entries = {
        10: Entry(5, "proj", True, 0, NOW_FT),
        11: Entry(10, "main.py", False, 100, NOW_FT),
        12: Entry(10, "node_modules", True, 0, NOW_FT),
        13: Entry(12, "index.js", False, 200, OLD_FT),
        14: Entry(5, "README.md", False, 50, NOW_FT),
        15: Entry(10, ".git", True, 0, NOW_FT),
        16: Entry(15, "config.json", False, 10, OLD_FT),
        17: Entry(10, "Main.py", False, 300, NOW_FT, hidden=True),
        18: Entry(10, "notes", True, 0, NOW_FT),
        19: Entry(5, "project", True, 0, NOW_FT),   # 用来验证 path_scope 的前缀边界
        20: Entry(19, "other.py", False, 20, NOW_FT),
    }
    # 硬链接:main.py 同一 FRN 在根目录下还有一个名字
    vol.aliases = {11: ((5, "main-link.py"),)}
    vol.build_scan()
    return vol


@pytest.fixture()
def index() -> FakeIndex:
    return FakeIndex(build_volume())


def brute_force(idx, query: str, kind: str = "all") -> set[str]:
    """参照实现:逐行调用完整 matcher,完全不做分阶段优化。"""
    matcher = compile_query(query)
    out: set[str] = set()
    for vol in idx.vol_index.values():
        scan = vol.scan
        for i in range(len(scan)):
            is_dir = bool(scan.flags[i] & FLAG_DIR)
            if kind == "files" and is_dir:
                continue
            if kind == "folders" and not is_dir:
                continue
            path = _row_path(vol, scan, i)
            if path is None:
                continue
            if matcher(scan.names[i], path, scan.sizes[i], scan.mtimes[i], is_dir):
                out.add(path)
    return out


def engine(idx, query: str, kind: str = "all") -> set[str]:
    plan = compile_plan(query)
    return {h.path for h in run_search(idx, plan, kind=kind, limit=1000).hits}


ALL_QUERIES = [
    "",
    "main",
    "MAIN",
    "main.py",
    "*.py",
    "*.PY",
    "?.py",
    "ext:py",
    "ext:py;json",
    "ext:git",
    "!git",
    "!node_modules",
    "ext:py !git",
    "!*.js",
    "path:node_modules",
    "proj\\main",
    "size:>100",
    "size:<=100",
    "size:=50b",
    "case:Main",
    "regex:^m.*\\.py$",
    "regex:README",
    "main|readme",
    "config|index",
    "main py",
    "[abc]*.py",
    "*main*",
    "*",
    "dm:thisyear",
    "dm:>2020-01-01 ext:py",
    "proj",
]


class TestStagedEqualsBruteForce:
    @pytest.mark.parametrize("query", ALL_QUERIES)
    @pytest.mark.parametrize("kind", ["all", "files", "folders"])
    def test_same_result_set(self, index, query, kind):
        expected = brute_force(index, query, kind)
        got = engine(index, query, kind)
        assert got == expected, f"query={query!r} kind={kind}: 差集 +{got - expected} -{expected - got}"

    @pytest.mark.parametrize("query", [q for q in ALL_QUERIES if q])
    def test_total_matches(self, index, query):
        expected = len(brute_force(index, query))
        plan = compile_plan(query)
        assert run_search(index, plan, limit=1000).total == expected


class TestPlanShape:
    def test_plain_term_is_single_name_stage(self):
        plan = compile_plan("main")
        assert plan.quick is True
        assert len(plan.stages) == 1
        assert type(plan.stages[0]).__name__ == "NameContains"
        assert plan.single_term == "main"

    def test_longest_term_goes_first(self):
        plan = compile_plan("a longer")
        assert [s.needle for s in plan.stages] == ["longer", "a"]

    def test_multi_term_is_quick(self):
        plan = compile_plan("main py")
        assert plan.quick is True
        assert all(type(s).__name__ == "NameContains" for s in plan.stages)

    def test_extension_and_name_are_both_quick(self):
        plan = compile_plan("ext:py config")
        assert plan.quick is True

    def test_negative_term_uses_exclude_stage(self):
        plan = compile_plan("!git")
        assert plan.quick is True
        assert type(plan.stages[0]).__name__ == "NameExcludes"

    def test_negative_runs_after_narrowing(self):
        plan = compile_plan("ext:py !git")
        names = [type(s).__name__ for s in plan.stages]
        assert names == ["ExtIn", "NameExcludes"]

    def test_or_falls_back_to_full(self):
        plan = compile_plan("a|b")
        assert plan.quick is False
        assert type(plan.stages[0]).__name__ == "Full"

    def test_path_term_falls_back_to_full(self):
        plan = compile_plan("path:proj")
        assert plan.quick is False

    def test_wildcard_gets_literal_prefilter(self):
        plan = compile_plan("*.py")
        names = [type(s).__name__ for s in plan.stages]
        assert names == ["NameContains", "Full"]
        assert plan.stages[0].needle == ".py"

    def test_bracket_pattern_gets_no_prefilter(self):
        # fnmatch 把 [abc] 当字符类,任何片段都不是必要条件 → 不能预筛
        plan = compile_plan("[abc]*.py")
        assert [type(s).__name__ for s in plan.stages] == ["Full"]

    def test_empty_query_marked_empty(self):
        plan = compile_plan("   ")
        assert plan.empty is True
        assert plan.stages == ()


class TestRanking:
    def test_exact_name_comes_first(self, index):
        hits = run_search(index, compile_plan("main.py"), limit=10).hits
        assert hits[0].name == "main.py"
        assert hits[0].exact is True

    def test_low_priority_directory_is_demoted(self, index):
        # index.js 在 node_modules 下,config.json 在 .git 下
        hits = run_search(index, compile_plan("js"), limit=10).hits
        assert hits and hits[0].name == "index.js"
        low = run_search(index, compile_plan("*.js"), limit=10).hits
        # 目录 node_modules 自身也命中 "*.js"?不命中;命中的只有 index.js
        assert len(low) >= 1

    def test_stem_match_beats_substring(self, index):
        hits = run_search(index, compile_plan("main"), limit=10).hits
        names = [h.name for h in hits]
        # main.py / Main.py 整名去扩展名相等,应排在 main-link.py 之前
        assert names[0] in ("main.py", "Main.py")

    def test_sort_modified(self, index):
        hits = run_search(index, compile_plan("ext:py"), sort="modified", limit=10).hits
        mtimes = [h.mtime_ft for h in hits]
        assert mtimes == sorted(mtimes, reverse=True)

    def test_sort_size(self, index):
        hits = run_search(index, compile_plan(""), sort="size", limit=10).hits
        sizes = [h.size for h in hits]
        assert sizes == sorted(sizes, reverse=True)

    def test_path_scope(self, index):
        hits = run_search(index, compile_plan("ext:py"), path_scope="C:\\proj", limit=10).hits
        assert hits
        assert all(h.path.startswith("C:\\proj\\") for h in hits)

    def test_path_scope_does_not_leak_into_sibling_prefix(self, index):
        # C:\project 不是 C:\proj 的子目录,不能被当成命中
        hits = run_search(index, compile_plan("ext:py"), path_scope="C:\\proj", limit=50).hits
        assert hits
        assert not any(h.path.startswith("C:\\project\\") for h in hits)
        wide = run_search(index, compile_plan("ext:py"), path_scope="C:\\project", limit=50).hits
        assert [h.name for h in wide] == ["other.py"]


class TestHardlinkAliasRows:
    def test_alias_is_a_separate_row(self, index):
        paths = engine(index, "*")
        assert "C:\\main-link.py" in paths
        assert "C:\\proj\\main.py" in paths

    def test_alias_not_dropped_by_name_filter(self, index):
        paths = engine(index, "main-link")
        assert paths == {"C:\\main-link.py"}


def _usn(frn, parent, name, reason, ts=NOW_FT, attrs=0x20):
    from everything_ai.ntfs import UsnRecord

    return UsnRecord(frn, parent, name, reason, ts, attrs)


class TestLiveUpdates:
    """USN 变更必须立刻反映到查询结果(索引不再只在启动时刷新)。"""

    def test_created_file_becomes_searchable(self, index):
        vol = index.vol_index["C"]
        vol.apply_usn_records([_usn(20, 10, "brand-new.txt", 0x100)], 999)
        assert "C:\\proj\\brand-new.txt" in engine(index, "brand-new")

    def test_deleted_file_disappears_with_its_alias(self, index):
        vol = index.vol_index["C"]
        vol.apply_usn_records([_usn(11, 10, "main.py", 0x200)], 999)
        paths = engine(index, "*")
        assert "C:\\proj\\main.py" not in paths
        assert "C:\\main-link.py" not in paths

    def test_renamed_file_uses_new_name(self, index):
        vol = index.vol_index["C"]
        vol.apply_usn_records([_usn(11, 10, "renamed.py", 0x2000)], 999)
        paths = engine(index, "*.py")
        assert "C:\\proj\\renamed.py" in paths
        assert "C:\\proj\\main.py" not in paths

    def test_delete_then_recreate_same_frn(self, index):
        """单表按 FRN 打死亡标记会误杀这里的新行;基表/增量表分离正是为此。"""
        vol = index.vol_index["C"]
        vol.apply_usn_records([_usn(11, 10, "main.py", 0x200)], 1)
        vol.apply_usn_records([_usn(11, 10, "reborn.txt", 0x100)], 2)
        paths = engine(index, "*")
        assert "C:\\proj\\reborn.txt" in paths
        assert "C:\\proj\\main.py" not in paths

    def test_data_change_updates_mtime_once(self, index):
        vol = index.vol_index["C"]
        vol.apply_usn_records([_usn(14, 5, "README.md", 0x2, ts=NOW_FT + 10)], 1)
        vol.apply_usn_records([_usn(14, 5, "README.md", 0x2, ts=NOW_FT + 20)], 2)
        assert len(vol.delta) == 1  # 同一 FRN 就地覆盖,不堆重复行
        hits = run_search(index, compile_plan("readme"), limit=5).hits
        assert hits and hits[0].mtime_ft == NOW_FT + 20

    def test_created_file_size_is_read_back(self, index):
        """USN 记录不带大小:新建的文件必须回读磁盘,否则 size 永远是 0。"""
        vol = index.vol_index["C"]
        vol._stat = lambda path: (1234, NOW_FT + 5)
        vol.apply_usn_records([_usn(30, 10, "new.txt", 0x100)], 1)
        hits = run_search(index, compile_plan("new.txt"), limit=5).hits
        assert hits and hits[0].size == 1234
        assert hits[0].mtime_ft == NOW_FT + 5
        # 旧 mtime 不能被回读的小值回退
        assert hits[0].mtime_ft >= NOW_FT

    def test_directory_create_is_not_stat_ed(self, index):
        vol = index.vol_index["C"]
        calls: list[str] = []
        vol._stat = lambda path: (calls.append(path), (1, 2))[1]
        vol.apply_usn_records([_usn(31, 10, "newdir", 0x100, attrs=0x10)], 1)
        assert calls == []

    def test_data_change_refreshes_size(self, index):
        vol = index.vol_index["C"]
        vol._stat = lambda path: (999, NOW_FT + 1)
        vol.apply_usn_records([_usn(14, 5, "README.md", 0x2, ts=NOW_FT + 1)], 1)
        hits = run_search(index, compile_plan("readme"), limit=5).hits
        assert hits and hits[0].size == 999

    def test_repeated_churn_triggers_compaction(self, index):
        from everything_ai.index import SCAN_DELTA_MAX

        vol = index.vol_index["C"]
        base_before = len(vol.scan)
        for k in range(SCAN_DELTA_MAX + 5):
            vol.apply_usn_records([_usn(1000 + k, 10, f"f{k}.txt", 0x100)], k + 1)
        # 超过阈值会整表重建:基表吸收了大部分新行,增量表被压回阈值以下
        assert len(vol.scan) > base_before + SCAN_DELTA_MAX
        assert len(vol.delta) < SCAN_DELTA_MAX
        paths = engine(index, "f0.txt")
        assert "C:\\proj\\f0.txt" in paths
        assert f"C:\\proj\\f{SCAN_DELTA_MAX}.txt" in engine(index, f"f{SCAN_DELTA_MAX}.txt")
