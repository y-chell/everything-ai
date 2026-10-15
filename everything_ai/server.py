# -*- coding: utf-8 -*-
"""everything-ai MCP server:AI agent 原生的全盘文件搜索(Everything 机制重实现)。

无需手动挂后台:索引由本 server 进程按需构建(MFT 全量 + USN Journal 增量),
持久化到 SQLite,重启后秒级恢复。需要管理员权限读取卷设备。
"""
from __future__ import annotations

import threading

from mcp.server.fastmcp import FastMCP

from . import ntfs, search as search_mod
from .index import FileIndex
from .query import QueryError, compile_plan
from .search import list_folder as _list_folder
from .search import file_info as _file_info
from .search import recent_files as _recent_files
from .search import run_search

MAX_SEARCH_LIMIT = 200
MAX_FOLDER_LIMIT = 500

INSTRUCTIONS = (
    "全盘文件名搜索:NTFS MFT 索引 + USN Journal 实时增量(Everything 同款机制),"
    "覆盖本机所有固定磁盘,索引里的文件包括这台电脑上其他用户拥有的。\n"
    "语法:空格 = AND(顺序不限),`|` = OR,`!词` = 排除,`\"整段\"` = 短语,"
    "`path:子串` = 路径匹配,词里含 `\\` 也自动按路径匹配,"
    "`ext:py;md` = 扩展名,`size:>1mb` = 大小,`dm:today|>2026-01-01` = 修改时间,"
    "`regex:...` = 正则,`case:...` = 区分大小写,`*` `?` = 通配符。\n"
    "排序:默认 relevance —— 名字完全相等 > 名字开头命中 > 词首命中 > 普通子串,"
    "你自己的文件靠前,node_modules/.git/系统目录里的靠后,最近改过的靠前。\n"
    "建议:结果太多就加一个更具体的词或 `ext:`;搜不到就换同义词、换更短的词干、"
    "中英文各试一遍;不确定某个路径是什么就用 file_info 确认;"
    "`limit` 保持 20-50 即可,不要一次要几百条。\n"
    "空 query 会返回最近修改的文件;要找「最近改了什么」直接用 recent_files,"
    "要看某个目录里有什么用 list_folder,这两个都比 search 更直接。"
)

mcp = FastMCP("everything-ai", instructions=INSTRUCTIONS)

index = FileIndex()
_build_thread: threading.Thread | None = None


def _ensure_build_started() -> None:
    global _build_thread
    if _build_thread is None or not _build_thread.is_alive():
        if index.status in ("empty", "error"):
            _build_thread = threading.Thread(target=index.build_all, daemon=True)
            _build_thread.start()


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(int(value), hi))


def _envelope(outcome: search_mod.Outcome, extra: dict | None = None) -> dict:
    payload = {
        "items": [h.as_dict() for h in outcome.hits],
        "returned": len(outcome.hits),
        "total_matched": outcome.total,
        "truncated": outcome.truncated,
        "elapsed_ms": round(outcome.elapsed_ms, 1),
    }
    if extra:
        payload.update(extra)
    return payload


def _not_ready() -> dict:
    return {
        "error": f"索引未就绪(status={index.status})",
        "detail": index.status_detail or "首次构建约 20-60 秒/卷,请稍后重试 index_status 查看进度",
    }


@mcp.tool()
def search(
    query: str,
    kind: str = "all",
    sort: str = "relevance",
    limit: int = 30,
    offset: int = 0,
    path_scope: str = "",
    include_system: bool = True,
) -> dict:
    """全盘文件名搜索(Everything 语法)。95% 的情况用这一个工具就够。

    Args:
        query: 搜索表达式。空格=AND,|=OR,!=NOT,"..."=短语,path:xxx=按路径,
            ext:py;md=扩展名,size:>1mb=大小,dm:today|>2026-01-01=修改时间,
            regex:xxx=正则,case:xxx=区分大小写,*?=通配符。含 \\ 的词按路径匹配。
            留空则返回最近修改的文件。
        kind: all | files | folders,默认 all。
        sort: relevance(默认) | modified | oldest | size | name。
        limit: 返回条数(1-200,默认 30)。
        offset: 跳过前 N 条(分页)。
        path_scope: 只在这个目录及其子目录里找(完整路径,如 D:\\proj)。留空=全盘。
        include_system: 是否包含 node_modules/.git/系统目录和隐藏文件,默认包含
            (它们只是排序靠后);置 false 直接排除。
    """
    _ensure_build_started()
    if index.status != "ready":
        return _not_ready()
    try:
        plan = compile_plan(query)
    except QueryError as exc:
        return {"error": f"查询语法错误: {exc}"}

    kind = kind if kind in ("all", "files", "folders") else "all"
    sort = sort if sort in ("relevance", "modified", "oldest", "size", "name") else "relevance"
    index.refresh_if_stale()
    # 持读锁跑完整个查询:MCP client 可以并发调工具,而 USN 刷新可能正在重建索引
    # (重建会替换 entries/scan),不加锁会读到撕裂状态、算出对不上的路径。
    with index.lock:
        outcome = run_search(
            index,
            plan,
            kind=kind,
            sort=sort,
            limit=_clamp(limit, 1, MAX_SEARCH_LIMIT),
            offset=max(0, int(offset)),
            path_scope=path_scope or None,
            include_system=bool(include_system),
        )
    return _envelope(outcome)


@mcp.tool()
def recent_files(
    folder: str = "",
    extensions: list[str] | None = None,
    days: int = 0,
    kind: str = "files",
    limit: int = 30,
    include_system: bool = False,
) -> dict:
    """最近修改的文件(倒序)。找"刚写的东西""今天改了什么"用这个,不要用 search。

    Args:
        folder: 只在这个目录及其子目录里找(完整路径)。留空=所有磁盘。
        extensions: 只看这些扩展名,如 ["py", "md"]。
        days: 只看最近 N 天修改过的;0 = 不限。
        kind: files(默认) | folders | all。
        limit: 返回条数(1-200,默认 30)。
        include_system: 是否包含 node_modules/.git/系统目录和隐藏文件,默认排除。
    """
    _ensure_build_started()
    if index.status != "ready":
        return _not_ready()
    kind = kind if kind in ("all", "files", "folders") else "files"
    index.refresh_if_stale()
    with index.lock:
        outcome = _recent_files(
            index,
            folder=folder or None,
            extensions=tuple(extensions or ()),
            days=max(0, int(days)),
            kind=kind,
            limit=_clamp(limit, 1, MAX_SEARCH_LIMIT),
            include_system=bool(include_system),
        )
    return _envelope(outcome)


@mcp.tool()
def list_folder(
    path: str,
    extensions: list[str] | None = None,
    days: int = 0,
    kind: str = "files",
    include_hidden: bool = False,
    limit: int = 100,
) -> dict:
    """列出某个目录的直接子项(不递归,实读磁盘,永远是最新状态)。

    Args:
        path: 目录完整路径,如 D:\\Projects。
        extensions: 只看这些扩展名,如 ["py"]。
        days: 只看最近 N 天修改过的;0 = 不限。
        kind: files(默认) | folders | all。
        include_hidden: 是否包含隐藏项,默认不包含。
        limit: 返回条数(1-500,默认 100)。
    """
    kind = kind if kind in ("all", "files", "folders") else "files"
    hits, total, error = _list_folder(
        path,
        extensions=tuple(extensions or ()),
        days=max(0, int(days)),
        kind=kind,
        include_hidden=bool(include_hidden),
        limit=_clamp(limit, 1, MAX_FOLDER_LIMIT),
    )
    if error:
        return {"error": error, "path": path}
    return {
        "path": path,
        "items": [h.as_dict() for h in hits],
        "returned": len(hits),
        "total_matched": total,
        "truncated": total > len(hits),
    }


@mcp.tool()
def file_info(path: str) -> dict:
    """单个路径的元数据:是否存在、类型、大小、三个时间、扩展名、隐藏/只读。

    Args:
        path: 文件或目录的完整路径。
    """
    return _file_info(path)


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
