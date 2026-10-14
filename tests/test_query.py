# -*- coding: utf-8 -*-
"""query.py 测试:Everything 语法解析正例 + 反例。"""
import pytest

from everything_ai.query import QueryError, compile_query

# (name, path, size, mtime_ft, is_dir)
FT = 134_000_000_000_000_000  # 任意 FILETIME


def m(name="report.txt", path="C:\\docs\\report.txt", size=100, mtime=FT, is_dir=False):
    return (name, path, size, mtime, is_dir)


class TestBasic:
    def test_empty_matches_all(self):
        matcher = compile_query("")
        assert matcher(*m()) is True

    def test_single_substring(self):
        matcher = compile_query("report")
        assert matcher(*m()) is True
        assert matcher(*m(name="other.doc")) is False

    def test_case_insensitive_default(self):
        matcher = compile_query("REPORT")
        assert matcher(*m()) is True

    def test_and(self):
        matcher = compile_query("report txt")
        assert matcher(*m()) is True
        assert matcher(*m(name="report.doc")) is False

    def test_or(self):
        matcher = compile_query("doc|txt")
        assert matcher(*m()) is True
        assert matcher(*m(name="a.py")) is False

    def test_and_or_precedence(self):
        # a b|c == a AND (b OR c)
        matcher = compile_query("report doc|txt")
        assert matcher(*m()) is True  # report AND txt
        assert matcher(*m(name="report.doc")) is True  # report AND doc
        assert matcher(*m(name="notes.txt")) is False  # no report

    def test_not(self):
        matcher = compile_query("!doc")
        assert matcher(*m()) is True  # report.txt 不含 doc → 保留
        assert matcher(*m(name="a.doc")) is False

    def test_not_with_and(self):
        matcher = compile_query("txt !doc")
        assert matcher(*m()) is True
        assert matcher(*m(name="report.doc")) is False

    def test_quoted_phrase(self):
        matcher = compile_query('"report 2026"')
        assert matcher(*m(name="report 2026 final.txt")) is True
        assert matcher(*m(name="report.txt")) is False


class TestModifiers:
    def test_path(self):
        matcher = compile_query("path:docs")
        assert matcher(*m()) is True
        assert matcher(*m(path="C:\\web\\a.txt")) is False

    def test_path_separator_implies_path_match(self):
        matcher = compile_query("docs\\report")
        assert matcher(*m()) is True
        assert matcher(*m(name="report.txt", path="C:\\web\\report.txt")) is False

    def test_ext_single(self):
        matcher = compile_query("ext:txt")
        assert matcher(*m()) is True
        assert matcher(*m(name="a.py")) is False
        assert matcher(*m(name="noext", is_dir=False)) is False

    def test_ext_multi(self):
        matcher = compile_query("ext:py;md")
        assert matcher(*m(name="a.py")) is True
        assert matcher(*m(name="b.md")) is True
        assert matcher(*m(name="c.txt")) is False

    def test_ext_ignores_dirs(self):
        matcher = compile_query("ext:git")
        assert matcher(*m(name=".git", is_dir=True)) is False

    def test_case_sensitive(self):
        matcher = compile_query("case:Report")
        assert matcher(*m()) is False
        assert matcher(*m(name="Report.txt")) is True

    def test_regex(self):
        matcher = compile_query("regex:^report.*\\.txt$")
        assert matcher(*m()) is True
        assert matcher(*m(name="xreport.txt")) is False

    def test_regex_invalid(self):
        with pytest.raises(QueryError):
            compile_query("regex:([unclosed")

    def test_size_gt(self):
        matcher = compile_query("size:>1mb")
        assert matcher(*m(size=2 * 1024 * 1024)) is True
        assert matcher(*m(size=1024)) is False

    def test_size_lt_eq(self):
        assert compile_query("size:<=100kb")(*m(size=100 * 1024)) is True
        assert compile_query("size:<=100kb")(*m(size=100 * 1024 + 1)) is False
        assert compile_query("size:=5kb")(*m(size=5120)) is True

    def test_size_invalid(self):
        with pytest.raises(QueryError):
            compile_query("size:abc")

    def test_dm_date_range(self):
        # 2026-10-14 当天
        dt = compile_query("dm:2026-10-14")
        import calendar
        from datetime import datetime, timezone
        # 构造 2026-10-14 12:00 本地时间的 FILETIME
        local_noon = datetime(2026, 10, 14, 12, 0, 0).astimezone()
        ft = int((local_noon - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)
        assert dt(*m(mtime=ft)) is True
        other = datetime(2026, 10, 15, 12, 0, 0).astimezone()
        ft2 = int((other - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)
        assert dt(*m(mtime=ft2)) is False

    def test_dm_gt(self):
        matcher = compile_query("dm:>2026-01-01")
        from datetime import datetime, timezone
        ft = int((datetime(2026, 6, 1, tzinfo=timezone.utc) - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)
        assert matcher(*m(mtime=ft)) is True
        ft_old = int((datetime(2025, 6, 1, tzinfo=timezone.utc) - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)
        assert matcher(*m(mtime=ft_old)) is False

    def test_dm_invalid(self):
        with pytest.raises(QueryError):
            compile_query("dm:notadate")


class TestWildcard:
    def test_star(self):
        matcher = compile_query("*.py")
        assert matcher(*m(name="a.py")) is True
        assert matcher(*m(name="a.txt")) is False

    def test_question(self):
        matcher = compile_query("a?.txt")
        assert matcher(*m(name="ab.txt")) is True
        assert matcher(*m(name="abc.txt")) is False

    def test_wildcard_path(self):
        matcher = compile_query("path:C:\\docs\\*")
        assert matcher(*m()) is True
        assert matcher(*m(path="C:\\web\\a.txt")) is False


class TestErrors:
    def test_unclosed_quote(self):
        with pytest.raises(QueryError):
            compile_query('"unclosed')

    def test_dangling_pipe(self):
        with pytest.raises(QueryError):
            compile_query("a |")

    def test_dangling_not(self):
        with pytest.raises(QueryError):
            compile_query("a !")

    def test_unknown_prefix_treated_as_literal(self):
        # Everything 行为:未知前缀按普通词处理
        matcher = compile_query("foo:bar")
        assert matcher(*m(name="foo:bar.txt")) is True
        assert matcher(*m(name="other.txt")) is False
