# -*- coding: utf-8 -*-
"""查询性能基线 + 真实数据正确性抽查。

用法(需管理员):python scripts/bench_search.py

正确性抽查:每个查询同时用"分阶段求值"(run_search)和"逐条完整 matcher"
(compile_query 遍历扫描表)各跑一遍,比较匹配总数。两者必须一致——分阶段
过滤只允许改变求值顺序,不允许改变结果集合。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from everything_ai.index import FileIndex  # noqa: E402
from everything_ai.query import _row_path, compile_plan, compile_query  # noqa: E402
from everything_ai.rank import FLAG_DIR  # noqa: E402
from everything_ai.search import run_search  # noqa: E402

QUERIES = [
    "",
    "ext:py",
    "ext:py;md",
    "python.exe",
    "path:system32 ext:dll",
    "*.log dm:today",
    "node_modules",
    "!node_modules ext:json",
    "path:windows size:>10mb",
    "config",
    "README.md",
    "*.py !test",
    "main|app|index ext:py",
]


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb() -> tuple[float, float]:
    c = _PMC()
    c.cb = ctypes.sizeof(_PMC)
    fn = getattr(ctypes.windll.kernel32, "K32GetProcessMemoryInfo", None) or ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
    fn.restype = wt.BOOL
    if not fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return 0.0, 0.0
    return c.WorkingSetSize / 1e6, c.PeakWorkingSetSize / 1e6


def brute_total(index, query: str) -> int:
    """参照实现:逐行调用完整 matcher。"""
    matcher = compile_query(query)
    n = 0
    for vol in index.vol_index.values():
        for scan, dead in vol.scan_tables():
            for i in range(len(scan)):
                if dead and scan.frns[i] in dead:
                    continue
                path = _row_path(vol, scan, i)
                if path is None:
                    continue
                if matcher(scan.names[i], path, scan.sizes[i], scan.mtimes[i], bool(scan.flags[i] & FLAG_DIR)):
                    n += 1
    return n


def size_accuracy(index, sample: int = 400) -> tuple[int, int, list[str]]:
    """抽样比对索引里的 size 与磁盘上的 size。"""
    import random

    from everything_ai.search import _row_path

    checked = 0
    bad: list[str] = []
    for vol in index.vol_index.values():
        for scan, _dead in vol.scan_tables():
            idxs = [i for i in range(len(scan)) if not (scan.flags[i] & FLAG_DIR)]
            if not idxs:
                continue
            for i in random.sample(idxs, min(sample, len(idxs))):
                p = _row_path(vol, scan, i)
                if p is None:
                    continue
                try:
                    real = os.stat(p).st_size
                except OSError:
                    continue
                checked += 1
                if real != scan.sizes[i]:
                    bad.append(f"idx={scan.sizes[i]} disk={real} {p}")
    return checked, len(bad), bad


def main() -> None:
    t0 = time.perf_counter()
    idx = FileIndex()
    idx.build_all()
    load_s = time.perf_counter() - t0
    cur, peak = rss_mb()
    total = sum(v.record_count for v in idx.vol_index.values())
    rows = sum(len(v.scan or []) + len(v.delta) for v in idx.vol_index.values())
    print(f"build/load: {load_s:.1f}s  status={idx.status}  records={total}  scan_rows={rows}")
    print(f"rss: {cur:.0f}MB  peak: {peak:.0f}MB")

    checked, bad_n, bad = size_accuracy(idx)
    print(f"\nsize 抽样对拍: checked={checked} mismatched={bad_n}")
    for line in bad[:5]:
        print("   ", line)

    mismatched = 0
    for q in QUERIES:
        plan = compile_plan(q)
        t0 = time.perf_counter()
        out = run_search(idx, plan, limit=30)
        dt = (time.perf_counter() - t0) * 1000
        if plan.empty:
            # 空查询走 recent_files 的 mtime 快速通道,total 是"至少这么多"而非精确总数
            print(f"[n/a] {dt:8.1f} ms  returned={len(out.hits)} (空查询按最近修改返回)  q={q!r}")
            continue
        t0 = time.perf_counter()
        ref = brute_total(idx, q)
        dt_ref = (time.perf_counter() - t0) * 1000
        ok = "OK " if out.total == ref else "MISMATCH"
        mismatched += out.total != ref
        print(
            f"[{ok}] {dt:8.1f} ms (ref {dt_ref:8.1f} ms)  total={out.total:>8}  "
            f"truncated={out.truncated}  q={q!r}"
        )
        if out.total != ref:
            print(f"        staged={out.total} matcher={ref}")
    print(f"\nMISMATCHED: {mismatched}/{len(QUERIES) - 1}")
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
