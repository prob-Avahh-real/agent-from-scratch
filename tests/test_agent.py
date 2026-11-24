from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_tools
import memory


def _load_stage5_module():
    """Load 05_agent.py even if optional network library `requests` is absent."""
    spec = importlib.util.spec_from_file_location("stage05_agent", ROOT / "05_agent.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    if "requests" not in sys.modules:
        fake_requests = types.ModuleType("requests")
        fake_exc = types.ModuleType("requests.exceptions")

        class RequestException(Exception):
            pass

        fake_exc.RequestException = RequestException
        fake_requests.exceptions = fake_exc
        fake_requests.post = lambda *a, **kw: None
        with mock.patch.dict(
            sys.modules,
            {"requests": fake_requests, "requests.exceptions": fake_exc},
        ):
            spec.loader.exec_module(module)
    else:
        spec.loader.exec_module(module)
    return module


stage05 = _load_stage5_module()


class AgentToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._orig_session_dir = memory.SESSION_DIR
        self._orig_legacy_dir = memory.LEGACY_DIR
        self._orig_legacy_file = memory.LEGACY_FILE
        memory.SESSION_DIR = self.tmp_path / "sessions"
        memory.LEGACY_DIR = self.tmp_path / "legacy_sessions"
        memory.LEGACY_FILE = self.tmp_path / "legacy_memory.json"

    def tearDown(self) -> None:
        memory.SESSION_DIR = self._orig_session_dir
        memory.LEGACY_DIR = self._orig_legacy_dir
        memory.LEGACY_FILE = self._orig_legacy_file
        self._tmp.cleanup()

    def test_calculator_evaluates_arithmetic_and_blocks_unsafe_ast(self) -> None:
        self.assertEqual(agent_tools.calculator("(17 * 23 + 5) / 6"), "66.0")
        self.assertEqual(agent_tools.calculator("-3 + 2 ** 4 % 5"), "-2")
        obs = agent_tools.execute("calculator", {"expression": "__import__('os').system('echo x')"})
        self.assertTrue(obs.startswith("ERROR:"))

    def test_workspace_boundary_and_session_isolation(self) -> None:
        outside = agent_tools.execute("read_file", {"path": "../outside.txt"})
        self.assertIn("ERROR:", outside)
        self.assertIn("只能访问工作目录内的文件", outside)

        # Even if SESSION_DIR were misconfigured inside WORKSPACE, tools must deny access
        memory.SESSION_DIR = agent_tools.WORKSPACE / ".isolated_test_sessions"
        denied = agent_tools.execute("read_file", {"path": ".isolated_test_sessions/default.json"})
        self.assertIn("会话隔离", denied)

    def test_read_file_and_list_dir_and_unknown_tool(self) -> None:
        listing = agent_tools.execute("list_dir", {"path": "."})
        self.assertIn("05_agent.py", listing)
        self.assertIn("agent_tools.py", listing)

        snippet = agent_tools.execute("read_file", {"path": "requirements.txt", "start_line": 1, "max_lines": 5})
        self.assertIn("1 |", snippet)

        unknown = agent_tools.execute("non_existent_tool", {})
        self.assertEqual(unknown, "ERROR: 没有名为 non_existent_tool 的工具")


class MemoryLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._orig_session_dir = memory.SESSION_DIR
        self._orig_legacy_dir = memory.LEGACY_DIR
        self._orig_legacy_file = memory.LEGACY_FILE
        memory.SESSION_DIR = self.tmp_path / "sessions"
        memory.LEGACY_DIR = self.tmp_path / "legacy_sessions"
        memory.LEGACY_FILE = self.tmp_path / "legacy_memory.json"

    def tearDown(self) -> None:
        memory.SESSION_DIR = self._orig_session_dir
        memory.LEGACY_DIR = self._orig_legacy_dir
        memory.LEGACY_FILE = self._orig_legacy_file
        self._tmp.cleanup()

    def test_session_persistence_and_safe_naming(self) -> None:
        self.assertEqual(memory.safe_name("../bad name!"), "badname")
        self.assertEqual(memory.safe_name(""), memory.DEFAULT_SESSION)

        sess = memory.new_session("work-01")
        sess["messages"].append({"role": "user", "content": "你好"})
        memory.save(sess)

        self.assertEqual(memory.list_sessions(), ["work-01"])
        reloaded = memory.load("work-01")
        self.assertEqual(reloaded["messages"][0]["content"], "你好")

        memory.reset("work-01")
        self.assertEqual(memory.list_sessions(), [])

    def test_tiered_trim_archives_older_messages_and_injects_summary(self) -> None:
        sess = memory.new_session("trim-test")
        for i in range(6):
            sess["messages"].append({"role": "user", "content": f"msg-{i}"})

        seen = {}

        def fake_summarize(prev: str, transcript: str) -> str:
            seen["prev"] = prev
            seen["transcript"] = transcript
            return "用户连续发送了早期测试消息"

        moved = memory.trim(sess, fake_summarize, max_messages=4, keep_recent=2, max_archive=10)
        self.assertTrue(moved)
        self.assertEqual(len(sess["messages"]), 2)
        self.assertEqual(len(sess["archive"]), 4)
        self.assertIn("msg-0", seen["transcript"])
        self.assertEqual(sess["summary"], "用户连续发送了早期测试消息")

        ctx = memory.context(sess)
        self.assertEqual(len(ctx), 1)
        self.assertEqual(ctx[0]["role"], "system")
        self.assertIn("用户连续发送了早期测试消息", ctx[0]["content"])

    def test_usage_and_cost_accounting(self) -> None:
        sess = memory.new_session("cost-test")
        delta = memory.record_usage(
            sess,
            {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "prompt_cache_hit_tokens": 400,
            },
        )
        self.assertGreater(delta, 0.0)
        self.assertEqual(sess["stats"]["calls"], 1)
        self.assertEqual(sess["stats"]["cache_hit_tokens"], 400)
        self.assertEqual(sess["stats"]["cache_miss_tokens"], 600)
        self.assertEqual(sess["stats"]["total_tokens"], 1500)


class Stage05ReActLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._orig_session_dir = memory.SESSION_DIR
        self._orig_legacy_dir = memory.LEGACY_DIR
        self._orig_legacy_file = memory.LEGACY_FILE
        memory.SESSION_DIR = self.tmp_path / "sessions"
        memory.LEGACY_DIR = self.tmp_path / "legacy_sessions"
        memory.LEGACY_FILE = self.tmp_path / "legacy_memory.json"

    def tearDown(self) -> None:
        memory.SESSION_DIR = self._orig_session_dir
        memory.LEGACY_DIR = self._orig_legacy_dir
        memory.LEGACY_FILE = self._orig_legacy_file
        self._tmp.cleanup()

    def test_parse_args_and_streaming_tool_call_reassembly(self) -> None:
        opts = stage05.parse_args(["--session", "demo", "--no-stream", "算一下", "6*7"])
        self.assertEqual(opts["session"], "demo")
        self.assertFalse(opts["stream"])
        self.assertEqual(opts["prompt"], "算一下 6*7")

        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"cal","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"culator","arguments":"{\\"expression\\":"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"6*7\\"}"}}]},"finish_reason":"tool_calls"}]}',
            'data: {"usage":{"prompt_tokens":20,"completion_tokens":10,"total_tokens":30}}',
            "data: [DONE]",
        ]

        class FakeStreamResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_lines(self, decode_unicode: bool = True):
                return iter(sse_lines)

        with mock.patch.object(stage05, "post_with_retry", return_value=FakeStreamResp()):
            msg, finish, usage = stage05.one_call([{"role": "user", "content": "6*7"}], agent_tools.TOOLS, stream=True)

        self.assertEqual(finish, "tool_calls")
        self.assertEqual(usage["total_tokens"], 30)
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "calculator")
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"expression": "6*7"})

    def test_run_turn_executes_react_tool_and_saves_final_answer(self) -> None:
        sess = memory.new_session("react")
        responses = [
            (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_calc",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": '{"expression": "12 * 12"}'},
                        }
                    ],
                },
                "tool_calls",
                {"prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65},
            ),
            (
                {"role": "assistant", "content": "12 乘 12 的结果是 144。"},
                "stop",
                {"prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92},
            ),
        ]

        buf = io.StringIO()
        with mock.patch.object(stage05, "one_call", side_effect=responses), redirect_stdout(buf):
            answer = stage05.run_turn(sess, "12 * 12 是多少？", stream=False)

        self.assertEqual(answer, "12 乘 12 的结果是 144。")
        self.assertEqual(len(sess["messages"]), 2)
        self.assertEqual(sess["stats"]["calls"], 2)
        self.assertIn("144", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
