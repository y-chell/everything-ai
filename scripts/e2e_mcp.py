# -*- coding: utf-8 -*-
"""MCP server 端到端验证:stdio JSON-RPC 握手 + tools/list + tools/call。"""
import json
import subprocess
import sys
import time

SERVER = [sys.executable, "-m", "everything_ai.server"]


def send(proc, obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()


def recv(proc, want_id=None, timeout=120):
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


def main():
    proc = subprocess.Popen(SERVER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "e2e-test", "version": "0"}}})
        init = recv(proc, 1)
        print("initialize ok:", init["result"]["serverInfo"]["name"])

        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(proc, 2)["result"]["tools"]
        print("tools:", [t["name"] for t in tools])

        # 等索引就绪(首次冷启动建索引;有缓存时秒级)
        for attempt in range(60):
            send(proc, {"jsonrpc": "2.0", "id": 10 + attempt, "method": "tools/call", "params": {
                "name": "index_status", "arguments": {}}})
            status = json.loads(recv(proc, 10 + attempt)["result"]["content"][0]["text"])
            print(f"  [{attempt}] status={status['status']} records={[v['records'] for v in status['volumes']]}")
            if status["status"] == "ready":
                break
            time.sleep(5)
        if status["status"] != "ready":
            print("FAIL: index not ready")
            return 1

        send(proc, {"jsonrpc": "2.0", "id": 100, "method": "tools/call", "params": {
            "name": "search", "arguments": {"query": "ext:py path:everything-ai", "max_results": 5}}})
        r = json.loads(recv(proc, 100)["result"]["content"][0]["text"])
        print(f"search ext:py path:everything-ai -> total={r['total_matched']}")
        for item in r["results"][:5]:
            print("   ", item["path"])

        send(proc, {"jsonrpc": "2.0", "id": 101, "method": "tools/call", "params": {
            "name": "search", "arguments": {"query": "everything.exe path:bin", "max_results": 3}}})
        r = json.loads(recv(proc, 101)["result"]["content"][0]["text"])
        print(f"search everything.exe path:bin -> total={r['total_matched']}")
        for item in r["results"][:3]:
            print("   ", item["path"], item["size"], item["modified"])

        send(proc, {"jsonrpc": "2.0", "id": 102, "method": "tools/call", "params": {
            "name": "search", "arguments": {"query": "size:>500mb dm:today", "max_results": 3, "sort": "size"}}})
        r = json.loads(recv(proc, 102)["result"]["content"][0]["text"])
        print(f"search size:>500mb dm:today -> total={r['total_matched']}")
        for item in r["results"][:3]:
            print("   ", item["path"], item["size"])

        ok = r["total_matched"] >= 0 and len(tools) == 3
        print("E2E:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        proc.kill()


if __name__ == "__main__":
    sys.exit(main())
