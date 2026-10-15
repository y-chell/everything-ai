# -*- coding: utf-8 -*-
"""相关性打分:把 WinShun(src/core/Query.cpp NameMatcher::score、NameSearch.cpp staticBonus)
的打分模型移植到本项目的扁平扫描表上。

打分构成(整数,越大越靠前):
  - 每个命中项:出现在名字开头 +30;出现在词首 +18;否则 +4
  - 覆盖率:+25 * 命中字数 / 名字长度
  - 单正项时整名相等 +60、去扩展名相等 +45
  - 静态:目录 +2、低优先级目录 -35、隐藏 -15
  - 深度惩罚:-min(depth, 12)

排序键(越小越靠前):(-score, 文件在前, -mtime, 路径)
"""
from __future__ import annotations

# WinShun FileIndex.h 的 knownTime 阈值:1980-01-02 UTC 之前的时间一律当"未知",
# 排序时与 0 一样排最后(索引里仍存原值)。
FIRST_KNOWN_FT = 315_619_200 * 10_000_000

# flags 位(扁平扫描表的 bytearray)
FLAG_DIR = 1
FLAG_HIDDEN = 2
FLAG_LOW_PRIORITY = 4

# 低优先级目录名(折叠后)。参照 WinShun 的 dirPriorityFlags:以 "." 开头的目录
# 一律低优先级,再加上构建产物 / 依赖 / 缓存类目录;这些目录里的文件在
# 相关度排序里降权,但不会从结果里消失。
LOW_PRIORITY_NAMES = frozenset(
    {
        "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".cache", ".gradle", ".idea", ".vs",
        ".vscode", ".next", ".nuxt", ".terraform", "site-packages", "dist-info",
        "cmakefiles", "cmake-build-debug", "cmake-build-release", "coverage",
        "vendor", "third_party", "thirdparty", "bower_components",
    }
)
# 系统 / 程序目录名:同样降权(参照 WinShun path 排序里的 aside 判定)
SYSTEM_DIR_NAMES = frozenset(
    {
        "windows", "winsxs", "system32", "syswow64", "sysnative", "program files",
        "program files (x86)", "programdata", "$recycle.bin", "$windows.~bt",
        "$windows.~ws", "recovery", "msocache", "driverstore", "assembly",
        "servicing", "installer", "softwaredistribution", "winsxs\\manifests",
    }
)
_WORD_SEP = " _-.()[]{}+@!#$%&,;="


def low_priority_dir(name_lower: str) -> bool:
    """目录名是否属于低优先级(点目录 / 构建产物 / 依赖 / 缓存 / 系统目录)。"""
    if name_lower.startswith("."):
        return True
    return name_lower in LOW_PRIORITY_NAMES or name_lower in SYSTEM_DIR_NAMES


def _word_bonus(name_lower: str, term: str, pos: int) -> int:
    """命中不在名字开头:向右最多再找 4 次,任一次落在词首记 +18,否则 +4。"""
    for _ in range(4):
        if pos > 0 and name_lower[pos - 1] in _WORD_SEP:
            return 18
        pos = name_lower.find(term, pos + 1)
        if pos < 0:
            return 4
    return 4


def score_terms(name_lower: str, terms: tuple[str, ...]) -> int:
    """正项全部命中时的基础分(terms 已按长度降序,调用方保证都命中)。"""
    score = 0
    covered = 0
    for t in terms:
        pos = name_lower.find(t)
        if pos < 0:
            continue
        score += 30 if pos == 0 else _word_bonus(name_lower, t, pos)
        covered += len(t)
    n = len(name_lower)
    if n:
        score += 25 * min(covered, n) // n
    return score


def exact_bonus(name_lower: str, term: str) -> int:
    """单正项整名相等 +60;去扩展名相等 +45;否则 0。"""
    if name_lower == term:
        return 60
    dot = name_lower.rfind(".")
    if dot > 0 and name_lower[:dot] == term:
        return 45
    return 0


def static_bonus(is_dir: bool, low_priority: bool, hidden: bool, depth: int) -> int:
    """与查询无关的静态加减分(在扫描表构建时即可确定的部分)。"""
    s = 2 if is_dir else 0
    if low_priority:
        s -= 35
    if hidden:
        s -= 15
    s -= min(depth, 12)
    return s
