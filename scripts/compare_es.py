# -*- coding: utf-8 -*-
"""与 es.exe(Everything 官方 CLI)对拍验证。

同一查询分别跑 everything-ai 引擎与 es.exe,比较结果集合。
差异容忍:建索引与查询之间文件系统可能漂移,报告差异数量与样例。
"""
import json
import os
import subprocess
import sys
import time

from everything_ai.index import EXCLUDED_PATH_PREFIXES, FileIndex
from everything_ai.query import compile_query

# es.exe 路径:默认从 PATH 找,或用环境变量 ES_EXE 指定完整路径
ES_EXE = os.environ.get("ES_EXE", "es.exe")

QUERIES = [
    "everything.exe",
    "ext:py path:everything-ai",
    "*.dll path:system32",
    "size:>500mb",
    "dm:today ext:log",
]


def run_es(query: str, limit: int = 200000) -> set[str]:
    """es.exe 把每个 argv 当独立搜索词(AND),argv 内空格不拆分 → 按词拆开传。

    -json 输出为 UTF-8,避开控制台 GBK 乱码。
    """
    out = subprocess.run(
        [ES_EXE, "-n", str(limit), "-json", *query.split()],
        capture_output=True, timeout=120,
    ).stdout.decode("utf-8", errors="replace")
    paths = set()
    for line in out.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            p = json.loads(line)["filename"]
        except (json.JSONDecodeError, KeyError):
            continue
        low = p.lower()
        if not low.startswith("c:\\"):
            continue
        if any(low.startswith(x) for x in EXCLUDED_PATH_PREFIXES):
            continue
        paths.add(p)
    return paths


def run_ours(idx: FileIndex, query: str) -> set[str]:
    matcher = compile_query(query)
    paths = set()
    for _drive, _frn, e, path in idx.iter_items():
        low = path.lower()
        if any(low.startswith(x) for x in EXCLUDED_PATH_PREFIXES):
            continue
        if matcher(e.name, path, e.size, e.mtime_ft, e.is_dir):
            paths.add(path)
    return paths


def main():
    t0 = time.perf_counter()
    idx = FileIndex(volumes=["C"])
    idx.build_all()
    print(f"index built/loaded in {time.perf_counter() - t0:.1f}s, records={idx.vol_index['C'].record_count}")

    total_diff = 0
    for q in QUERIES:
        t = time.perf_counter()
        ours = run_ours(idx, q)
        t_ours = time.perf_counter() - t
        t = time.perf_counter()
        es = run_es(q)
        t_es = time.perf_counter() - t
        only_ours = ours - es
        only_es = es - ours
        status = "OK " if not only_ours and not only_es else "DIFF"
        print(f"[{status}] {q!r}: ours={len(ours)} ({t_ours:.2f}s) es={len(es)} ({t_es:.2f}s) only_ours={len(only_ours)} only_es={len(only_es)}")
        for p in list(only_ours)[:3]:
            print(f"    + {p}")
        for p in list(only_es)[:3]:
            print(f"    - {p}")
        total_diff += len(only_ours) + len(only_es)
    print(f"\nTOTAL DIFF: {total_diff}")
    return 0 if total_diff == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
