"""Read-only market tools for the Claude chat tab, as a small MCP server over stdin and stdout.

Started by the Claude CLI with the receiver's port. Every tool is a GET on the local receiver,
so a tool can read what the screen reads and can change nothing.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

MAX_RESULT = 60_000
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def text_field(description):
    return {"type": "string", "description": description}


TOOLS = {
    "search_items": ("품목 이름(한국어·영어)으로 품목을 찾습니다.", "/api/lookup/search", {"q": text_field("품목 이름, 예: 클레이모어")}, ["q"]),
    "item_prices": ("품목 하나의 도시별 시세와 자료 나이를 봅니다.", "/api/lookup/item", {"id": text_field("품목 ID, 예: T7_RELIC. 인챈트는 @1처럼 붙입니다"), "quality": text_field("품질 0~5. 0은 아무 품질")}, ["id"]),
    "family_prices": ("같은 품목의 티어·인챈트별 시세를 비교합니다.", "/api/lookup/family", {"family": text_field("search_items 결과의 family 값")}, ["family"]),
    "watches": ("시세 감시 목록과 투자 예측을 봅니다.", "/api/watches", {}, []),
}


def call(port, name, arguments):
    path = TOOLS[name][1]
    query = urllib.parse.urlencode({k: str(v) for k, v in arguments.items() if k in TOOLS[name][2]})
    url = f"http://127.0.0.1:{port}{path}" + ("?" + query if query else "")
    try:
        with NO_PROXY.open(url, timeout=60) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", "replace")[:2000], True
    if len(text) > MAX_RESULT:
        text = text[:MAX_RESULT] + "\n(너무 길어 잘렸습니다)"
    return text, False


def answer(port, request):
    method = request.get("method")
    if method == "initialize":
        version = (request.get("params") or {}).get("protocolVersion") or "2024-11-05"
        return {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": {"name": "albion", "version": "1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": name, "description": t[0], "inputSchema": {"type": "object", "properties": t[2], "required": t[3]}} for name, t in TOOLS.items()]}
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        if name not in TOOLS:
            return {"content": [{"type": "text", "text": "없는 도구입니다"}], "isError": True}
        try:
            text, failed = call(port, name, params.get("arguments") or {})
        except OSError as exc:
            text, failed = f"시장 프로그램에 연결하지 못했습니다: {exc}", True
        return {"content": [{"type": "text", "text": text}], "isError": failed}
    return None


def main():
    port = int(sys.argv[1])
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for line in stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if "id" not in request:
            continue  # A notification needs no answer.
        result = answer(port, request)
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        if result is None:
            reply["error"] = {"code": -32601, "message": "Method not found"}
        else:
            reply["result"] = result
        stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        stdout.flush()


if __name__ == "__main__":
    main()
