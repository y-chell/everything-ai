# -*- coding: utf-8 -*-
"""everything-ai MCP server:AI agent 原生的全盘文件搜索(Everything 机制重实现)。

无需手动挂后台:索引由本 server 进程按需构建(MFT 全量 + USN Journal 增量),
持久化到 SQLite,重启后秒级恢复。需要管理员权限读取卷设备。
"""
from __future__ import annotations

import heapq
import threading
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from . import ntfs
from .index import EXCLUDED_PATH_PREFIXES, FileIndex
from .query import QueryError, compile_query

FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

mcp = FastMCP(
    "everything-ai",
    instructions=(
        "全盘文件名搜索(NTFS MFT 索引 + USN Journal 实时增量,Everything 同款机制)。"
        "查询语法与 Everything 兼容:空格=AND、|=OR、!=NOT、path:/ext:/size:/dm:/regex:/case:、*? 通配符。"
    ),
)

index = FileIndex()
_build_thread: threading.Thread | None = None


def _ensure_build_started() -> None:
    global _build_thread
    if _build_thread is None or not _build_thread.is_alive():
        if index.status in ("empty", "error"):
            _build_thread = threading.Thread(target=index.build_all, daemon=True)
            _build_thread.start()


def _ft_to_iso(ft: int) -> str | None:
    if not ft:
        return None
    try:
        dt = FILETIME_EPOCH + timedelta(microseconds=ft // 10)
        return dt.astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError):
        return None


def _path_excluded(path: str) -> bool:
    low = path.lower()
    return any(low.startswith(p) for p in EXCLUDED_PATH_PREFIXES)


@mcp.tool()
def search(query: str, max_results: int = 100, offset: int = 0, sort: str = "name") -> dict:
    """全盘文件搜索(Everything 语法)。

    Args:
        query: 搜索表达式。空格=AND,|=OR,!=NOT,"..."=短语,path:xxx=按路径,
            ext:py;md=扩展名,size:>1mb=大小,dm:today|>2026-01-01=修改时间,
            regex:xxx=正则,case:xxx=区分大小写,*?=通配符。含 \\ 的词按路径匹配。
        max_results: 返回条数上限(1-1000)。
        offset: 跳过前 N 条(分页)。
        sort: name | date_modified | size。
    """
    _ensure_build_started()
    with index.lock:
        status = index.status
        detail = index.status_detail
    if status != "ready":
        return {
            "error": f"索引未就绪(status={status})",
            "detail": detail or "首次构建约 20-60 秒/卷,请稍后重试 index_status 查看进度",
        }
    try:
        matcher = compile_query(query)
    except QueryError as exc:
        return {"error": f"查询语法错误: {exc}"}

    max_results = max(1, min(int(max_results), 1000))
    offset = max(0, int(offset))

    matched = 0
    collected: list[tuple] = []
    for drive, frn, e, path in index.iter_items():
        if _path_excluded(path):
            continue
        if not matcher(e.name, path, e.size, e.mtime_ft, e.is_dir):
            continue
        matched += 1
        if sort == "date_modified":
            key = e.mtime_ft
        elif sort == "size":
            key = e.size
        else:
            key = path.lower()
        collected.append((key, drive, frn))

    k = offset + max_results
    if sort in ("date_modified", "size"):
        top = heapq.nlargest(k, collected, key=lambda t: t[0])
    else:
        top = heapq.nsmallest(k, collected, key=lambda t: t[0])
    page = top[offset:]

    results = []
    for _key, drive, frn in page:
        vol = index.vol_index[drive]
        e = vol.entries[frn]
        path = vol.paths.get(frn) or vol.resolve_path(frn)
        if path is None or _path_excluded(path):
            continue
        results.append(
            {
                "path": path,
                "is_dir": e.is_dir,
                "size": e.size,
                "modified": _ft_to_iso(e.mtime_ft),
            }
        )
    return {"total_matched": matched, "returned": len(results), "offset": offset, "results": results}


@mcp.tool()
def index_status() -> dict:
    """查看索引状态:各卷记录数、构建时间、USN 位置、是否就绪。"""
    _ensure_build_started()
    return index.index_status()


@mcp.tool()
def rebuild_index(volume: str = "") -> dict:
    """强制重建索引(全量重读 $MFT)。volume 留空重建全部卷。

    Args:
        volume: 盘符字母,如 "C"。留空 = 全部卷。
    """
    try:
        index.rebuild(volume or None)
        return {"ok": True, "status": index.index_status()}
    except (ValueError, ntfs.NtfsError) as exc:
        return {"error": str(exc)}


def main() -> None:
    _ensure_build_started()
    mcp.run()


if __name__ == "__main__":
    main()
