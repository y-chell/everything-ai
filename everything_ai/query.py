# -*- coding: utf-8 -*-
"""Everything 搜索语法解析器(兼容子集)+ 分阶段查询计划。

语法:空格=AND、``|``=OR、``!``=NOT、``"..."``=短语、
``path:``=全路径、``ext:``=扩展名(多值用 ``;``)、``case:``=区分大小写、
``regex:``=正则、``size:``=大小(>/>=/</<=/= n b|kb|mb|gb)、
``dm:``=修改时间(today/yesterday/thisweek/thismonth/thisyear/>=<=日期)、
``*``/``?``=通配符。词含 ``\\`` 时自动按路径匹配(Everything 同款行为)。

除原有的 ``compile_query``(返回单个 matcher)外,本模块提供 ``compile_plan``:
把查询拆成**按代价排序的过滤阶段**,便宜的条件(名字子串、扩展名)先在整表上
跑,昂贵的条件(路径、正则、OR/NOT 子树)只跑前面筛剩的候选。当所有条件都能
拆成廉价阶段时 ``Plan.quick`` 为真,此时无需再调用完整 matcher——这就是
WinShun ``NameMatcher::m_quickDecides`` 的等价物。
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .rank import FLAG_DIR

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
# term 分析:把修饰符前缀解析成结构化 TermSpec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TermSpec:
    text: str
    on_path: bool = False
    case: bool = False
    kind: str = "plain"          # plain | wildcard | regex | ext | size | dm
    exts: tuple[str, ...] = ()
    size: tuple[str, int] | None = None
    mtime: tuple[int, int] | None = None


def _parse_size(text: str) -> tuple[str, int]:
    m = re.fullmatch(r"(>=|<=|>|<|=)?\s*(\d+(?:\.\d+)?)\s*([kmg]?b)?", text.strip(), re.IGNORECASE)
    if not m:
        raise QueryError(f"size: 语法无效: {text!r}(示例 size:>1mb)")
    op = m.group(1) or "="
    value = float(m.group(2))
    unit = (m.group(3) or "b").lower()
    return op, int(value * _SIZE_UNITS[unit])


def _parse_dm(text: str) -> tuple[int, int]:
    """→ [start_ft, end_ft) 的 FILETIME 区间;无界边界用哨兵。"""
    text = text.strip().lower()
    now = datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        start, end = today, today + timedelta(days=1)
    elif text == "yesterday":
        start, end = today - timedelta(days=1), today
    elif text == "thisweek":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    elif text == "thismonth":
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    elif text == "thisyear":
        start = today.replace(month=1, day=1)
        end = start.replace(year=start.year + 1)
    else:
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
            return _dt_to_ft(end), _FT_MAX
        if op == "<":
            return 0, _dt_to_ft(start)
        if op == ">=":
            return _dt_to_ft(start), _FT_MAX
        if op == "<=":
            return 0, _dt_to_ft(end)
    return _dt_to_ft(start), _dt_to_ft(end)


_FT_MAX = 0x7FFFFFFFFFFFFFFF


def _dt_to_ft(dt: datetime) -> int:
    return int((dt - FILETIME_EPOCH).total_seconds() * 10_000_000)


def _analyze_term(term: str) -> TermSpec:
    """剥掉修饰符前缀 → TermSpec。ext/regex/size/dm 立即定型(与旧行为一致)。"""
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
            exts = tuple(e.lower().lstrip(".") for e in term[4:].replace("|", ";").split(";") if e)
            if not exts:
                raise QueryError("ext: 缺少扩展名")
            return TermSpec(term, on_path, case_sensitive, "ext", exts=exts)
        elif low.startswith("regex:"):
            pattern = term[6:]
            try:
                re.compile(pattern)
            except re.error as exc:
                raise QueryError(f"正则无效: {exc}") from exc
            return TermSpec(pattern, on_path, case_sensitive, "regex")
        elif low.startswith("size:"):
            return TermSpec(term, on_path, case_sensitive, "size", size=_parse_size(term[5:]))
        elif low.startswith("dm:"):
            return TermSpec(term, on_path, case_sensitive, "dm", mtime=_parse_dm(term[3:]))
        else:
            break
    if not term:
        raise QueryError("空条件")
    if any(c in term for c in "*?"):
        return TermSpec(term, on_path, case_sensitive, "wildcard")
    if "\\" in term or "/" in term:
        # Everything 行为:词含路径分隔符 → 按路径匹配
        return TermSpec(term.replace("/", "\\"), True, case_sensitive, "plain")
    return TermSpec(term, on_path, case_sensitive, "plain")


# ---------------------------------------------------------------------------
# TermSpec → matcher
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


def _ext_matcher(exts: tuple[str, ...]) -> Matcher:
    ext_set = frozenset(exts)

    def match(name: str, path: str, size: int, mtime: int, is_dir: bool) -> bool:
        if is_dir:
            return False
        dot = name.rfind(".")
        return dot != -1 and name[dot + 1:].lower() in ext_set

    return match


def _size_matcher(op: str, n: int) -> Matcher:
    if op == ">":
        return lambda name, path, size, mtime, is_dir: size > n
    if op == ">=":
        return lambda name, path, size, mtime, is_dir: size >= n
    if op == "<":
        return lambda name, path, size, mtime, is_dir: size < n
    if op == "<=":
        return lambda name, path, size, mtime, is_dir: size <= n
    return lambda name, path, size, mtime, is_dir: size == n


def _dm_matcher(lo: int, hi: int) -> Matcher:
    return lambda name, path, size, mtime, is_dir: lo <= mtime < hi


def _build_matcher(spec: TermSpec) -> Matcher:
    if spec.kind == "ext":
        return _ext_matcher(spec.exts)
    if spec.kind == "size":
        return _size_matcher(*spec.size)
    if spec.kind == "dm":
        return _dm_matcher(*spec.mtime)
    if spec.kind == "regex":
        try:
            rx = re.compile(spec.text, 0 if spec.case else re.IGNORECASE)
        except re.error as exc:
            raise QueryError(f"正则无效: {exc}") from exc
        if spec.on_path:
            return lambda name, path, size, mtime, is_dir: rx.search(path) is not None
        return lambda name, path, size, mtime, is_dir: rx.search(name) is not None
    if spec.kind == "wildcard":
        return _wildcard_matcher(spec.text, spec.case, spec.on_path)
    return _substring_matcher(spec.text, spec.case, spec.on_path)


def _compile_term(term: str) -> Matcher:
    """编译单个词;支持前缀组合(case:/regex: 可叠加)。"""
    return _build_matcher(_analyze_term(term))


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


# ---------------------------------------------------------------------------
# 分阶段查询计划
# ---------------------------------------------------------------------------

def _needs_path(node) -> bool:
    kind = node[0]
    if kind == "term":
        return _analyze_term(node[1]).on_path
    if kind in ("and", "or"):
        return any(_needs_path(n) for n in node[1])
    if kind == "not":
        return _needs_path(node[1])
    return True


def _literal_runs(pattern: str) -> list[str]:
    """通配型里连续的非通配片段(每个都是必要条件)。

    含 ``[`` 的模式交给 fnmatch 当字符类处理,这时任何片段都不再是必要条件,
    直接放弃预筛(返回空),宁可慢也不能漏。
    """
    if "[" in pattern:
        return []
    return [p for p in re.split(r"[*?]+", pattern) if p]


def _positive_literals(node, out: list[str]) -> None:
    """收集正项中落在名字上的字面量(统一折叠),用于相关性加分。"""
    kind = node[0]
    if kind == "term":
        spec = _analyze_term(node[1])
        if spec.on_path:
            return
        if spec.kind == "plain":
            out.append(spec.text.lower())
        elif spec.kind == "wildcard":
            runs = _literal_runs(spec.text)
            if runs:
                out.append(max(runs, key=len).lower())
        return
    if kind in ("and", "or"):
        for n in node[1]:
            _positive_literals(n, out)
    # not 子树不计入


class Stage:
    """一级过滤。scan() 在整表上跑,filter() 只跑上一级剩下的候选。

    ``vol`` 是 VolumeIndex(提供延迟解析的目录路径),``scan`` 是它的扁平扫描表。
    """

    cost = 0

    def scan(self, vol, scan) -> list[int]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def filter(self, vol, scan, cands: list[int]) -> list[int]:  # pragma: no cover - 抽象
        raise NotImplementedError


class NameContains(Stage):
    """名字必须含某子串(折叠比较时对预折叠的名字表做 in,单次 C 调用)。"""

    cost = 0

    def __init__(self, needle: str, case: bool):
        self.case = case
        self.needle = needle if case else needle.lower()

    def scan(self, vol, scan) -> list[int]:
        src = scan.names if self.case else scan.lower
        nd = self.needle
        return [i for i, x in enumerate(src) if nd in x]

    def filter(self, vol, scan, cands: list[int]) -> list[int]:
        src = scan.names if self.case else scan.lower
        nd = self.needle
        return [i for i in cands if nd in src[i]]


class ExtIn(Stage):
    """扩展名命中。目录一律不命中(与 ``_ext_matcher`` 语义一致)。"""

    cost = 1

    def __init__(self, exts: tuple[str, ...]):
        self.suffixes = tuple("." + e for e in exts)

    def scan(self, vol, scan) -> list[int]:
        # endswith 放前面:命中少,后面那次 flags 索引只对候选取值(实测 95→68 ns/行)
        lower, flags = scan.lower, scan.flags
        suf = self.suffixes
        return [i for i, x in enumerate(lower) if x.endswith(suf) and not (flags[i] & FLAG_DIR)]

    def filter(self, vol, scan, cands: list[int]) -> list[int]:
        lower, flags = scan.lower, scan.flags
        suf = self.suffixes
        return [i for i in cands if lower[i].endswith(suf) and not (flags[i] & FLAG_DIR)]


class SizeCmp(Stage):
    cost = 2

    def __init__(self, op: str, n: int):
        self.n = n
        self.cmp = {">": int.__gt__, ">=": int.__ge__, "<": int.__lt__, "<=": int.__le__}.get(op, int.__eq__)

    def scan(self, vol, scan) -> list[int]:
        cmp_, n, sizes = self.cmp, self.n, scan.sizes
        return [i for i, s in enumerate(sizes) if cmp_(s, n)]

    def filter(self, vol, scan, cands: list[int]) -> list[int]:
        cmp_, n, sizes = self.cmp, self.n, scan.sizes
        return [i for i in cands if cmp_(sizes[i], n)]


class MtimeRange(Stage):
    cost = 3

    def __init__(self, lo: int, hi: int):
        self.lo = lo
        self.hi = hi

    def scan(self, vol, scan) -> list[int]:
        lo, hi, mt = self.lo, self.hi, scan.mtimes
        return [i for i, t in enumerate(mt) if lo <= t < hi]

    def filter(self, vol, scan, cands: list[int]) -> list[int]:
        lo, hi, mt = self.lo, self.hi, scan.mtimes
        return [i for i in cands if lo <= mt[i] < hi]


class NameExcludes(Stage):
    """名字必须不含某子串(``!词`` 的专用快速路径)。

    排除条件不会缩小候选集,所以代价排在所有收窄阶段之后、完整 matcher 之前。
    """

    cost = 4

    def __init__(self, needle: str, case: bool):
        self.case = case
        self.needle = needle if case else needle.lower()

    def scan(self, vol, scan) -> list[int]:
        src = scan.names if self.case else scan.lower
        nd = self.needle
        return [i for i, x in enumerate(src) if nd not in x]

    def filter(self, vol, scan, cands: list[int]) -> list[int]:
        src = scan.names if self.case else scan.lower
        nd = self.needle
        return [i for i in cands if nd not in src[i]]


class Full(Stage):
    """完整 matcher:正则、OR/NOT 子树、路径条件等(代价最高,排最后)。"""

    cost = 5

    def __init__(self, matcher: Matcher, needs_path: bool):
        self.matcher = matcher
        self.needs_path = needs_path

    def _test(self, vol, scan, i: int) -> bool:
        path = ""
        if self.needs_path:
            path = _row_path(vol, scan, i)
            if path is None:
                return False
        return self.matcher(scan.names[i], path, scan.sizes[i], scan.mtimes[i], bool(scan.flags[i] & 1))

    def scan(self, vol, scan) -> list[int]:
        return [i for i in range(len(scan.names)) if self._test(vol, scan, i)]

    def filter(self, vol, scan, cands: list[int]) -> list[int]:
        return [i for i in cands if self._test(vol, scan, i)]


def _row_path(vol, scan, i: int) -> str | None:
    """行下标 → 完整路径(目录带尾部反斜杠)。目录路径延迟解析。"""
    d = vol.dir_path(scan.parents[i])
    if d is None:
        return None
    name = scan.names[i]
    return d + name + "\\" if scan.flags[i] & FLAG_DIR else d + name


@dataclass(frozen=True)
class Plan:
    """一次查询的可执行计划(不再保留一个可能用不到的完整 matcher)。"""

    stages: tuple[Stage, ...] = ()
    quick: bool = False           # stages 已等价于整个查询,无需再跑完整 matcher
    empty: bool = False           # 空查询:匹配全部
    score_terms: tuple[str, ...] = ()   # 参与相关性打分的正项字面量(长→短)
    single_term: str | None = None      # 唯一正项(非通配、名字侧、折叠)→ 启用整名相等加分


def _stages_of(node) -> tuple[list[Stage], bool]:
    """→ (阶段列表, quick)。quick=False 表示有条件无法用阶段表达(会退化为 Full)。"""
    kind = node[0]
    if kind == "term":
        spec = _analyze_term(node[1])
        if spec.kind == "ext":
            return [ExtIn(spec.exts)], True
        if spec.kind == "size":
            return [SizeCmp(*spec.size)], True
        if spec.kind == "dm":
            return [MtimeRange(*spec.mtime)], True
        if spec.kind == "regex":
            return [Full(_build_matcher(spec), spec.on_path)], False
        if spec.kind == "wildcard":
            full = Full(_build_matcher(spec), spec.on_path)
            runs = _literal_runs(spec.text)
            if runs:
                # 最长的字面片段是必要条件,先用它筛掉绝大多数名字
                return [NameContains(max(runs, key=len), spec.case), full], False
            return [full], False
        if spec.on_path:
            return [Full(_build_matcher(spec), True)], False
        return [NameContains(spec.text, spec.case)], True
    if kind == "and":
        stages: list[Stage] = []
        quick = True
        for n in node[1]:
            sub, sub_quick = _stages_of(n)
            stages.extend(sub)
            quick = quick and sub_quick
        return stages, quick
    if kind == "not":
        child = node[1]
        if child[0] == "term":
            spec = _analyze_term(child[1])
            if spec.kind == "plain" and not spec.on_path:
                return [NameExcludes(spec.text, spec.case)], True
        return [Full(compile_node(node), _needs_path(node))], False
    # or:整体交给完整 matcher
    return [Full(compile_node(node), _needs_path(node))], False


def compile_plan(query: str) -> Plan:
    """编译成可执行的阶段计划。空查询匹配全部(由调用方决定排序)。"""
    if not query or not query.strip():
        return Plan(empty=True)
    tokens = tokenize(query)
    ast, pos = parse_expr(tokens, 0)
    if pos != len(tokens):
        raise QueryError(f"无法解析的 token: {tokens[pos:]!r}")

    stages, quick = _stages_of(ast)
    # 同代价内最长的子串先测:它拒绝的名字最多(WinShun NameMatcher 同款排序)
    stages.sort(key=lambda s: (s.cost, -len(getattr(s, "needle", ""))))

    lits: list[str] = []
    _positive_literals(ast, lits)
    seen = set()
    uniq: list[str] = []
    for t in lits:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    # 最长的正项先测:它拒绝的名字最多(WinShun NameMatcher 同款排序)
    uniq.sort(key=len, reverse=True)

    single: str | None = None
    if len(lits) == 1 and len(stages) == 1 and isinstance(stages[0], NameContains) and not stages[0].case:
        single = uniq[0]

    return Plan(
        stages=tuple(stages),
        quick=quick,
        score_terms=tuple(uniq),
        single_term=single,
    )


def run_stages(plan: Plan, vol, scan, kind_filter: Callable[[int], bool] | None = None) -> list[int]:
    """在单卷扫描表上按计划求值 → 候选行下标。"""
    if plan.stages:
        cands = plan.stages[0].scan(vol, scan)
        for st in plan.stages[1:]:
            if not cands:
                break
            cands = st.filter(vol, scan, cands)
    else:
        cands = list(range(len(scan.names)))
    if kind_filter is not None and cands:
        cands = [i for i in cands if kind_filter(i)]
    return cands
