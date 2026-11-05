"""The chat tab with a stand-in for the Claude CLI: nothing here reaches Claude or real data."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import chat
import market_tools
from market import Store, handler_class

ROOT = Path(__file__).resolve().parents[1]
STAND_IN = r'''
import json, os, sys, time
text = sys.stdin.buffer.read().decode("utf-8")
with open(os.path.join(os.path.dirname(__file__), "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": sys.argv[1:], "text": text, "env": sorted(os.environ)}) + "\n")
def out(v):
    print(json.dumps(v), flush=True)
def delta(s):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": s}}})
if text == "시작 전 실패":
    print("not json at all", flush=True)
    sys.exit(3)
out({"type": "system", "subtype": "init"})
if text == "로그인 안 됨":
    out({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"})
    sys.exit(1)
out({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "mcp__albion__item_prices"}}})
delta("첫 부분")
if text == "오래 걸림":
    time.sleep(30)
out({"type": "stream_event", "event": {"type": "message_start"}})
delta("끝 부분")
out({"type": "result", "is_error": False, "result": "끝 부분"})
'''


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        script = self.root / "stand_in.py"
        script.write_text(STAND_IN, encoding="utf-8")
        self.room = chat.ChatRoom(self.root, 18000, [sys.executable, str(script)])

    def tearDown(self):
        for _ in range(100):
            if not chat.JOBS:
                break
            time.sleep(.05)
        self.tmp.cleanup()

    def calls(self):
        return [json.loads(line) for line in (self.root / "calls.jsonl").read_text(encoding="utf-8").splitlines()]

    def ask(self, text, conversation=None):
        cid = self.room.send({"text": text, "conversation": conversation}, {"items": {}})["conversation"]
        for _ in range(400):
            if cid not in chat.JOBS:
                return cid, self.room.conversation(cid)
            time.sleep(.025)
        self.fail("answer never finished")

    def test_answer_is_saved_and_the_next_message_resumes_the_session(self):
        cid, data = self.ask("T7 유물 재고는?")
        self.assertEqual([m["role"] for m in data["messages"]], ["user", "assistant"])
        self.assertEqual(data["messages"][1]["text"], "첫 부분\n\n끝 부분")
        self.assertFalse(data["messages"][1]["failed"])
        self.assertEqual(data["title"], "T7 유물 재고는?")
        self.assertIsNone(data["running"])
        self.ask("다음 질문", cid)
        first, second = self.calls()
        self.assertEqual(first["args"][first["args"].index("--session-id") + 1], cid)
        self.assertEqual(second["args"][second["args"].index("--resume") + 1], cid)
        self.assertEqual(second["text"], "다음 질문")
        self.assertEqual([c["id"] for c in self.room.conversations()["conversations"]], [cid])

    def test_only_read_tools_are_given_and_private_files_are_denied(self):
        self.ask("질문")
        args = self.calls()[0]["args"]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--strict-mcp-config", args)
        allowed = args[args.index("--allowedTools") + 1].split(",")
        self.assertFalse([t for t in allowed if t not in ("Read", "Grep", "Glob") and not t.startswith("mcp__albion__")])
        denied = args[args.index("--disallowedTools") + 1]
        self.assertIn("Read(./config.json)", denied)
        self.assertIn("Read(./data/**)", denied)
        self.assertNotIn("질문", " ".join(args))

    def test_session_variables_of_a_parent_claude_are_not_inherited(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "x", "ANTHROPIC_BASE_URL": "http://x", "CLAUDE_CODE_OAUTH_TOKEN": "synthetic"}):
            self.ask("질문")
        env = self.calls()[0]["env"]
        self.assertFalse({"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_BASE_URL"} & set(env))
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_snapshot_leaves_out_the_token_mail_address_and_full_books(self):
        book = {"seen": "t", "levels": [{"price": p, "amount": 1, "order_id": "x"} for p in range(9)]}
        result = chat.snapshot_of({"token": "secret", "email": {"to": "a@example.com"}, "holdings": [1], "books": {"T7_RELIC": {"1002": {"sell": book, "buy": {"levels": []}}}}})
        self.assertEqual(set(result), {"holdings", "books_top"})
        top = result["books_top"]["T7_RELIC"]["1002"]
        self.assertEqual(set(top), {"sell"})
        self.assertEqual(top["sell"]["levels"], [{"price": p, "amount": 1} for p in range(5)])

    def test_failures_are_saved_as_failed_with_their_reason(self):
        _, data = self.ask("시작 전 실패")
        self.assertTrue(data["messages"][-1]["failed"])
        self.assertIn("not json at all", data["messages"][-1]["text"])
        _, data = self.ask("로그인 안 됨")
        self.assertTrue(data["messages"][-1]["failed"])
        self.assertIn("claude auth login", data["messages"][-1]["text"])

    def test_session_is_created_again_when_the_first_try_never_started(self):
        cid, _ = self.ask("시작 전 실패")
        self.ask("다시", cid)
        self.assertIn("--session-id", self.calls()[1]["args"])

    def test_stop_keeps_what_was_written_and_one_question_runs_at_a_time(self):
        cid = self.room.send({"text": "오래 걸림"}, {})["conversation"]
        for _ in range(400):
            if self.room.conversation(cid)["running"]["text"]:
                break
            time.sleep(.025)
        self.assertEqual(self.room.conversation(cid)["running"]["status"], "답을 쓰는 중")
        with self.assertRaises(ValueError):
            self.room.send({"text": "또", "conversation": cid}, {})
        self.assertTrue(self.room.stop(cid)["stopped"])
        for _ in range(400):
            if cid not in chat.JOBS:
                break
            time.sleep(.025)
        last = self.room.conversation(cid)["messages"][-1]
        self.assertEqual(last["text"], "첫 부분\n\n(중간에 멈췄습니다)")
        self.assertFalse(last["failed"])
        self.assertEqual(len([m for m in self.room.conversation(cid)["messages"] if m["role"] == "user"]), 1)

    def test_bad_input_is_refused(self):
        for payload in ({"text": "  "}, {"text": "x" * (chat.MAX_TEXT + 1)}, {"text": "질문", "conversation": "없는 대화"}):
            with self.assertRaises(ValueError):
                self.room.send(payload, {})
        with self.assertRaises(ValueError):
            self.room.conversation("없는 대화")
        self.assertFalse(chat.JOBS)


class ChatHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.example.json", self.root / "config.json")
        script = self.root / "stand_in.py"
        script.write_text(STAND_IN, encoding="utf-8")
        self.store = Store(self.root)
        self.store.chat_command = [sys.executable, str(script)]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(self.store, "local-token"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        for _ in range(100):
            if not chat.JOBS:
                break
            time.sleep(.05)
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        self.tmp.cleanup()

    def post(self, path, body, token="local-token", origin=None):
        headers = {"Content-Type": "application/json", "X-Local-Token": token}
        if origin:
            headers["Origin"] = origin
        return urllib.request.urlopen(urllib.request.Request(self.base + path, json.dumps(body).encode(), headers))

    def test_send_needs_the_local_token_and_a_local_origin(self):
        for kwargs in ({"token": "wrong"}, {"origin": "http://evil.example"}):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("/api/chat/send", {"text": "질문"}, **kwargs)
            self.assertEqual(caught.exception.code, 403)
        self.assertFalse((self.root / "calls.jsonl").exists())

    def test_send_writes_a_snapshot_without_the_token_and_the_answer_can_be_read(self):
        cid = json.load(self.post("/api/chat/send", {"text": "질문"}))["conversation"]
        for _ in range(400):
            data = json.load(urllib.request.urlopen(f"{self.base}/api/chat/conversation?id={cid}"))
            if not data["running"]:
                break
            time.sleep(.025)
        self.assertEqual(data["messages"][-1]["text"], "첫 부분\n\n끝 부분")
        snapshot = json.loads((self.root / "data" / "chat" / "snapshot.json").read_text(encoding="utf-8"))
        self.assertIn("holdings", snapshot)
        self.assertNotIn("token", snapshot)
        self.assertNotIn("books", snapshot)
        self.assertEqual(json.load(urllib.request.urlopen(self.base + "/api/chat"))["conversations"][0]["id"], cid)

    def test_unknown_conversation_is_a_client_error(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/api/chat/conversation?id=nope")
        self.assertEqual(caught.exception.code, 400)


class MarketToolsTests(unittest.TestCase):
    def test_tools_only_read(self):
        for name, tool in market_tools.TOOLS.items():
            self.assertTrue(tool[1].startswith("/api/"), name)
        self.assertEqual(set(market_tools.TOOLS), set(chat.TOOL_NAMES))

    def test_protocol_answers(self):
        self.assertEqual(market_tools.answer(1, {"method": "initialize", "params": {"protocolVersion": "v9"}})["protocolVersion"], "v9")
        names = [t["name"] for t in market_tools.answer(1, {"method": "tools/list"})["tools"]]
        self.assertEqual(names, list(market_tools.TOOLS))
        self.assertTrue(market_tools.answer(1, {"method": "tools/call", "params": {"name": "write_anything"}})["isError"])
        self.assertIsNone(market_tools.answer(1, {"method": "other"}))

    def test_call_reads_only_the_named_arguments(self):
        seen = {}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return "x".encode() * (market_tools.MAX_RESULT + 5)

        def fake_open(url, timeout=0):
            seen["url"] = url
            return Reply()
        with mock.patch.object(market_tools.NO_PROXY, "open", fake_open):
            text, failed = market_tools.call(1234, "search_items", {"q": "클레이모어", "other": "빠짐"})
        self.assertTrue(seen["url"].startswith("http://127.0.0.1:1234/api/lookup/search?q="))
        self.assertNotIn("other", seen["url"])
        self.assertFalse(failed)
        self.assertIn("잘렸습니다", text)


if __name__ == "__main__":
    unittest.main()
