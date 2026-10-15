# -*- coding: utf-8 -*-
"""MCP server 端到端验证:stdio JSON-RPC 握手 + tools/list + 全部工具各调一次。"""
import json
import subprocess
import sys
import time

SERVER = [sys.executable, "-m", "everything_ai.server"]
EXPECTED_TOOLS = {"search", "recent_files", "list_folder", "file_info", "index_status", "rebuild_index"}


def send(proc, obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()


def recv(proc, want_id=None, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout")
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if want_id is None or msg.get("id") == want_id:
            return msg
    raise TimeoutError(f"no response for id={want_id}")


def call(proc, rid, name, args):
    send(proc, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": name, "arguments": args}})
    return json.loads(recv(proc, rid)["result"]["content"][0]["text"])


def main():
    proc = subprocess.Popen(SERVER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    failures = []
    try:
        send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "e2e-test", "version": "0"}}})
        init = recv(proc, 1)
        print("initialize ok:", init["result"]["serverInfo"]["name"])

        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(proc, 2)["result"]["tools"]
        names = {t["name"] for t in tools}
        print("tools:", sorted(names))
        if names != EXPECTED_TOOLS:
            failures.append(f"tools/list 期望 {sorted(EXPECTED_TOOLS)},得到 {sorted(names)}")

        status = {}
        for attempt in range(120):
            status = call(proc, 10 + attempt, "index_status", {})
            print(f"  [{attempt}] status={status['status']} "
                  f"records={[v['records'] for v in status['volumes']]}")
            if status["status"] == "ready":
                break
            time.sleep(5)
        if status.get("status") != "ready":
            failures.append(f"索引未就绪: {status.get('detail')}")
            return 1

        r = call(proc, 100, "search", {"query": "ext:py path:everything_ai", "limit": 5})
        print(f"\nsearch ext:py path:everything_ai -> total={r['total_matched']} "
              f"{r['elapsed_ms']}ms truncated={r['truncated']}")
        for item in r["items"][:5]:
            print("   ", item["path"], item["size"], item["modified"], "exact=", item["exact"])
        if r["returned"] > 5:
            failures.append("limit 未生效")
        if r["total_matched"] < 3:
            failures.append(f"ext:py path:everything_ai 命中过少: {r['total_matched']}")

        r = call(proc, 101, "search", {"query": "*.py", "kind": "files", "sort": "modified", "limit": 3})
        print(f"\nsearch *.py sort=modified -> {r['elapsed_ms']}ms")
        for item in r["items"][:3]:
            print("   ", item["path"], item["modified"])

        r = call(proc, 102, "search", {"query": "!node_modules ext:json", "limit": 3})
        print(f"\nsearch !node_modules ext:json -> total={r['total_matched']} {r['elapsed_ms']}ms")

        r = call(proc, 103, "search", {"query": "", "limit": 3})
        print(f"\nsearch 空 query -> {r['elapsed_ms']}ms")
        for item in r["items"]:
            print("   ", item["path"], item["modified"])

        r = call(proc, 104, "recent_files", {"days": 7, "extensions": ["py", "md"], "limit": 5})
        print(f"\nrecent_files days=7 -> total={r['total_matched']} {r['elapsed_ms']}ms")
        for item in r["items"][:5]:
            print("   ", item["path"], item["modified"])

        r = call(proc, 105, "list_folder", {"path": r"E:\xiu\ctf-test\everything-ai", "kind": "all", "limit": 10})
        print(f"\nlist_folder -> returned={r['returned']} total={r.get('total_matched')}")
        for item in r["items"][:10]:
            print("   ", item["type"], item["name"], item["size"])

        r = call(proc, 106, "file_info", {"path": r"E:\xiu\ctf-test\everything-ai\README.md"})
        print(f"\nfile_info -> {json.dumps(r, ensure_ascii=False)[:200]}")

        r = call(proc, 107, "file_info", {"path": r"E:\definitely\missing\file.txt"})
        print(f"\nfile_info 不存在 -> {r}")
        if r.get("exists") is not False:
            failures.append("file_info 对不存在的路径应返回 exists=false")

        r = call(proc, 108, "search", {"query": "size:abc"})
        print(f"\nsearch 语法错误 -> {r}")
        if "error" not in r:
            failures.append("非法 size: 语法应报错")
    finally:
        proc.kill()

    print("\nE2E:", "PASS" if not failures else "FAIL")
    for f in failures:
        print("  -", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
