# -*- coding: utf-8 -*-
"""Everything 搜索语法解析器(兼容子集)。

语法:空格=AND、``|``=OR、``!``=NOT、``"..."``=短语、
``path:``=全路径、``ext:``=扩展名(多值用 ``;``)、``case:``=区分大小写、
``regex:``=正则、``size:``=大小(>/>=/</<=/= n b|kb|mb|gb)、
``dm:``=修改时间(today/yesterday/thisweek/thismonth/thisyear/>=<=日期)、
``*``/``?``=通配符。词含 ``\\`` 时自动按路径匹配(Everything 同款行为)。
"""
from __future__ import annotations

import fnmatch
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

# matcher(name, path, size, mtime_ft, is_dir) -> bool
Matcher = Callable[[str, str, int, int, bool], bool]

FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}


class QueryError(ValueError):
    pass


def tokenize(query: str) -> list[str]:
    """空格分词;双引号短语;``|`` 独立成词(引号内除外)。"""
    tokens: list[str] = []
    buf: list[str] = []
    in_quote = False
    i = 0
    while i < len(query):
        ch = query[i]
        if ch == '"':
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote and ch == "|":
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append("|")
            i += 1
            continue
        if not in_quote and ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if in_quote:
        raise QueryError("引号未闭合")
    if buf:
        tokens.append("".join(buf))
    return tokens


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

def parse_expr(tokens: list[str], pos: int):
    """expr := and_expr(顶层入口)"""
    return parse_and(tokens, pos)


def parse_and(tokens: list[str], pos: int):
    """and_expr := or_expr+ → ('and', [nodes]) | node

    Everything 语义:OR 绑定比 AND 更紧,`a b|c` = a AND (b OR c)。
    """
    nodes = []
    while pos < len(tokens):
        node, pos = parse_or(tokens, pos)
        nodes.append(node)
    if not nodes:
        raise QueryError("空表达式")
    if len(nodes) == 1:
        return nodes[0], pos
    return ("and", nodes), pos


def parse_or(tokens: list[str], pos: int):
    """or_expr := not_expr ('|' not_expr)* → ('or', [nodes]) | node"""
    nodes = []
    node, pos = parse_not(tokens, pos)
    nodes.append(node)
    while pos < len(tokens) and tokens[pos] == "|":
        if pos + 1 >= len(tokens):
            raise QueryError("'|' 后缺少条件")
        node, pos = parse_not(tokens, pos + 1)
        nodes.append(node)
    if len(nodes) == 1:
        return nodes[0], pos
    return ("or", nodes), pos


def parse_not(tokens: list[str], pos: int):
    tok = tokens[pos]
    if tok == "!":
        if pos + 1 >= len(tokens):
            raise QueryError("'!' 后缺少条件")
        node, pos = parse_not(tokens, pos + 1)
        return ("not", node), pos
    if tok.startswith("!") and len(tok) > 1:
        # "!doc" / "!path:xxx" 紧贴形式
        return ("not", ("term", tok[1:])), pos + 1
    return parse_term(tokens, pos)


def parse_term(tokens: list[str], pos: int):
    return ("term", tokens[pos]), pos + 1


# ---------------------------------------------------------------------------
# term 编译
# ---------------------------------------------------------------------------

def _substring_matcher(needle: str, case_sensitive: bool, on_path: bool) -> Matcher:
    if case_sensitive:
        if on_path:
            return lambda name, path, size, mtime, is_dir: needle in path
        return lambda name, path, size, mtime, is_dir: needle in name
    needle_l = needle.lower()
    if on_path:
        return lambda name, path, size, mtime, is_dir: needle_l in path.lower()
    return lambda name, path, size, mtime, is_dir: needle_l in name.lower()


def _wildcard_matcher(pattern: str, case_sensitive: bool, on_path: bool) -> Matcher:
    # Everything 通配符语义:匹配整个文件名/路径(fnmatch 全串)
    rx = re.compile(fnmatch.translate(pattern), re.IGNORECASE if not case_sensitive else 0)
    if on_path:
        return lambda name, path, size, mtime, is_dir: rx.match(path) is not None
    return lambda name, path, size, mtime, is_dir: rx.match(name) is not None


def _regex_matcher(pattern: str, case_sensitive: bool, on_path: bool) -> Matcher:
    try:
        rx = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise QueryError(f"正则无效: {exc}") from exc
    if on_path:
        return lambda name, path, size, mtime, is_dir: rx.search(path) is not None
    return lambda name, path, size, mtime, is_dir: rx.search(name) is not None


def _ext_matcher(exts: list[str]) -> Matcher:
    exts_l = [e.lower().lstrip(".") for e in exts if e]
    if not exts_l:
        raise QueryError("ext: 缺少扩展名")
    ext_set = frozenset(exts_l)
    return lambda name, path, size, mtime, is_dir: (
        not is_dir and name.rfind(".") != -1 and name[name.rfind(".") + 1:].lower() in ext_set
    )


def _parse_size(text: str) -> tuple[str, int]:
    m = re.fullmatch(r"(>=|<=|>|<|=)?\s*(\d+(?:\.\d+)?)\s*([kmg]?b)?", text.strip(), re.IGNORECASE)
    if not m:
        raise QueryError(f"size: 语法无效: {text!r}(示例 size:>1mb)")
    op = m.group(1) or "="
    value = float(m.group(2))
    unit = (m.group(3) or "b").lower()
    return op, int(value * _SIZE_UNITS[unit])


def _size_matcher(text: str) -> Matcher:
    op, n = _parse_size(text)
    if op == ">":
        return lambda name, path, size, mtime, is_dir: size > n
    if op == ">=":
        return lambda name, path, size, mtime, is_dir: size >= n
    if op == "<":
        return lambda name, path, size, mtime, is_dir: size < n
    if op == "<=":
        return lambda name, path, size, mtime, is_dir: size <= n
    return lambda name, path, size, mtime, is_dir: size == n


def _parse_dm(text: str) -> tuple[datetime | None, datetime | None]:
    """→ [start, end) 本地时间区间;None = 无界。"""
    text = text.strip().lower()
    now = datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        return today, today + timedelta(days=1)
    if text == "yesterday":
        return today - timedelta(days=1), today
    if text == "thisweek":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if text == "thismonth":
        start = today.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt
    if text == "thisyear":
        start = today.replace(month=1, day=1)
        return start, start.replace(year=start.year + 1)
    m = re.fullmatch(r"(>=|<=|>|<|=)?(\d{4}-\d{1,2}(?:-\d{1,2})?)", text)
    if not m:
        raise QueryError(f"dm: 语法无效: {text!r}(示例 dm:today / dm:>2026-01-01)")
    op, date_text = m.group(1) or "=", m.group(2)
    parts = [int(p) for p in date_text.split("-")]
    try:
        if len(parts) == 2:
            start = datetime(parts[0], parts[1], 1, tzinfo=None).astimezone()
            end = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
        else:
            start = datetime(*parts, tzinfo=None).astimezone()
            end = start + timedelta(days=1)
    except ValueError as exc:
        raise QueryError(f"dm: 日期无效: {date_text}") from exc
    if op == ">":
        return end, None
    if op == "<":
        return None, start
    if op == ">=":
        return start, None
    if op == "<=":
        return None, end
    return start, end  # =


def _dt_to_ft(dt: datetime) -> int:
    return int((dt - FILETIME_EPOCH).total_seconds() * 10_000_000)


_FT_MAX = 0x7FFFFFFFFFFFFFFF


def _dm_matcher(text: str) -> Matcher:
    start, end = _parse_dm(text)
    ft_start = _dt_to_ft(start) if start else 0
    ft_end = _dt_to_ft(end) if end else _FT_MAX
    return lambda name, path, size, mtime, is_dir: ft_start <= mtime < ft_end


def _compile_term(term: str) -> Matcher:
    """编译单个词;支持前缀组合(case:/regex: 可叠加)。"""
    on_path = False
    case_sensitive = False
    while True:
        low = term.lower()
        if low.startswith("path:"):
            on_path = True
            term = term[5:]
        elif low.startswith("case:"):
            case_sensitive = True
            term = term[5:]
        elif low.startswith("ext:"):
            return _ext_matcher(term[4:].replace("|", ";").split(";"))
        elif low.startswith("regex:"):
            return _regex_matcher(term[6:], case_sensitive, on_path)
        elif low.startswith("size:"):
            return _size_matcher(term[5:])
        elif low.startswith("dm:"):
            return _dm_matcher(term[3:])
        else:
            break
    if not term:
        raise QueryError("空条件")
    if any(c in term for c in "*?"):
        return _wildcard_matcher(term, case_sensitive, on_path)
    if "\\" in term or "/" in term:
        # Everything 行为:词含路径分隔符 → 按路径匹配
        norm = term.replace("/", "\\")
        return _substring_matcher(norm, case_sensitive, True)
    return _substring_matcher(term, case_sensitive, on_path)


def compile_node(node) -> Matcher:
    kind = node[0]
    if kind == "term":
        return _compile_term(node[1])
    if kind == "and":
        children = [compile_node(n) for n in node[1]]
        return lambda name, path, size, mtime, is_dir: all(c(name, path, size, mtime, is_dir) for c in children)
    if kind == "or":
        children = [compile_node(n) for n in node[1]]
        return lambda name, path, size, mtime, is_dir: any(c(name, path, size, mtime, is_dir) for c in children)
    if kind == "not":
        child = compile_node(node[1])
        return lambda name, path, size, mtime, is_dir: not child(name, path, size, mtime, is_dir)
    raise QueryError(f"未知节点 {kind}")


def compile_query(query: str) -> Matcher:
    """编译 Everything 语法查询 → matcher。空查询匹配全部。"""
    if not query or not query.strip():
        return lambda name, path, size, mtime, is_dir: True
    tokens = tokenize(query)
    if not tokens:
        return lambda name, path, size, mtime, is_dir: True
    ast, pos = parse_expr(tokens, 0)
    if pos != len(tokens):
        raise QueryError(f"无法解析的 token: {tokens[pos:]!r}")
    return compile_node(ast)
