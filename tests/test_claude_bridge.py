import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from workspace_daemon import claude_bridge, claude_bridge_hook, llm, shell

TOOL_A = "mcp__claude_ai_Example__list_items"
TOOL_B = "mcp__claude_ai_Example__get_item"
SECRET_PROMPT = "PROMPT-SECRET-7731"
SECRET_OUTPUT = "STUB-STDOUT-SECRET-9042"

STUB = textwrap.dedent(
    """\
    #!%(python)s
    import json, os, sys, time
    d = os.environ["STUB_DIR"]
    n = len([f for f in os.listdir(d) if f.startswith("argv-")]) + 1
    with open(os.path.join(d, "argv-%%d.json" %% n), "w") as f:
        json.dump(sys.argv[1:], f)
    with open(os.path.join(d, "cwd-%%d.txt" %% n), "w") as f:
        f.write(os.getcwd())
    settings = sys.argv[sys.argv.index("--settings") + 1]
    with open(os.path.join(d, "settings-%%d.json" %% n), "w") as f:
        f.write(open(settings).read())
    if os.path.exists(os.path.join(d, "noread")):
        time.sleep(30)
    with open(os.path.join(d, "stdin-%%d.txt" %% n), "w") as f:
        f.write(sys.stdin.read())
    if os.path.exists(os.path.join(d, "sleep")):
        time.sleep(30)
    stream = os.path.join(d, "stream-%%d.jsonl" %% n)
    if not os.path.exists(stream):
        stream = os.path.join(d, "stream-last.jsonl")
    sys.stdout.write(open(stream).read())
    code = os.path.join(d, "exit")
    sys.exit(int(open(code).read()) if os.path.exists(code) else 0)
    """
)


def line(obj):
    return json.dumps(obj) + "\n"


def init(tools=("ToolSearch", "Read", "Glob", "Grep")):
    return line({"type": "system", "subtype": "init", "tools": list(tools)})


def tool_use(use_id, name, tool_input=None):
    return line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": use_id, "name": name,
         "input": tool_input or {}}]}})


def tool_result(use_id, content, is_error=False):
    block = {"type": "tool_result", "tool_use_id": use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return line({"type": "user", "message": {"content": [block]}})


def result(subtype="success", is_error=False, denials=None, text="done"):
    return line({
        "type": "result", "subtype": subtype, "is_error": is_error,
        "num_turns": 2, "duration_ms": 5, "total_cost_usd": 0.01,
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "permission_denials": denials or [], "result": text,
    })


def good_stream(tool=TOOL_A, content="item one", extra=""):
    return (
        init()
        + tool_use("t1", "ToolSearch", {"query": f"select:{tool}"})
        + tool_result("t1", [{"type": "tool_reference", "tool_name": tool}])
        + tool_use("t2", tool, {"q": "x"})
        + tool_result("t2", content)
        + extra
        + result()
    )


class BridgeBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base = self.root / "base"
        self.base.mkdir()
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.stub = self.root / "claude"
        self.stub.write_text(STUB % {"python": sys.executable})
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IXUSR)
        env = mock.patch.dict(os.environ, {
            "WORKSPACE_DAEMON_CLAUDE_BIN": str(self.stub),
            "STUB_DIR": str(self.stub_dir),
        })
        env.start()
        self.addCleanup(env.stop)

    def stream(self, text, n="last"):
        (self.stub_dir / f"stream-{n}.jsonl").write_text(text)

    def invocations(self):
        return len(list(self.stub_dir.glob("argv-*.json")))

    def argv(self, n=1):
        return json.loads((self.stub_dir / f"argv-{n}.json").read_text())

    def usage_lines(self):
        path = self.base / "state" / "claude-usage.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()]


class CallToolsTest(BridgeBase):
    def call(self, tools=None, **kw):
        kw.setdefault("base_dir", self.base)
        kw.setdefault("connect_delay", 0)
        return claude_bridge.call_tools(
            SECRET_PROMPT, tools if tools is not None else {TOOL_A: {}}, **kw
        )

    def test_returns_real_text_result(self):
        self.stream(good_stream())
        out = self.call()
        self.assertEqual(out, [{
            "name": TOOL_A, "input": {"q": "x"}, "text": "item one",
            "is_error": False,
        }])

    def test_list_content_text_parts_concatenated(self):
        self.stream(good_stream(content=[
            {"type": "text", "text": "a"}, {"type": "tool_reference"},
            {"type": "text", "text": "b"},
        ]))
        self.assertEqual(self.call()[0]["text"], "ab")

    def test_prompt_on_stdin_only_with_search_instruction(self):
        self.stream(good_stream())
        self.call()
        raw = (self.stub_dir / "stdin-1.txt").read_text()
        self.assertTrue(raw.endswith("\n"))
        message = json.loads(raw)
        self.assertEqual(message["type"], "user")
        stdin = message["message"]["content"]
        self.assertIn(SECRET_PROMPT, stdin)
        self.assertTrue(stdin.startswith(
            f'First call ToolSearch with query "select:{TOOL_A}".'
        ))
        self.assertNotIn(SECRET_PROMPT, " ".join(self.argv()))

    def test_prompt_waits_for_connectors_by_default(self):
        self.stream(good_stream())
        with mock.patch.object(claude_bridge.time, "sleep") as sleep:
            self.call(connect_delay=claude_bridge.CONNECT_DELAY_SECONDS)
        sleep.assert_called_once_with(claude_bridge.CONNECT_DELAY_SECONDS)
        argv = self.argv()
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")

    def test_required_flags_and_settings(self):
        self.stream(good_stream())
        self.stream(good_stream())
        with self.assertRaises(claude_bridge.BridgeError):
            self.call({TOOL_A: {}, TOOL_B: {}}, attempts=1)
        argv = self.argv()
        self.assertEqual(argv[0], "-p")

        def value(flag):
            return argv[argv.index(flag) + 1]

        self.assertEqual(value("--setting-sources"), "")
        self.assertEqual(value("--permission-mode"), "dontAsk")
        self.assertEqual(value("--output-format"), "stream-json")
        self.assertIn("--verbose", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--disable-slash-commands", argv)
        self.assertEqual(value("--disallowedTools"),
                         claude_bridge.DEFAULT_DISALLOWED_TOOLS)
        self.assertIn("Bash", value("--disallowedTools").split(","))
        self.assertEqual(value("--allowedTools"),
                         f"ToolSearch,{TOOL_B},{TOOL_A}")
        self.assertNotIn("--tools", argv)
        settings = json.loads((self.stub_dir / "settings-1.json").read_text())
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], ".*")
        command = hook["hooks"][0]["command"]
        self.assertIn("claude_bridge_hook.py", command)
        self.assertIn(" -I ", command)
        self.assertNotIn("disableAllHooks", json.dumps(settings))

    def test_hallucinated_text_retries_then_fails(self):
        self.stream(
            init()
            + line({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "<invoke>fake</invoke> item one"}]}})
            + result()
        )
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(attempts=3)
        self.assertEqual(self.invocations(), 3)
        self.assertIn("missing", str(ctx.exception))
        self.assertIn(TOOL_A, str(ctx.exception))
        self.assertEqual([u["attempt"] for u in self.usage_lines()], [1, 2, 3])

    def test_succeeds_on_later_attempt(self):
        self.stream(init() + result(), n=1)
        self.stream(good_stream(), n=2)
        self.assertEqual(self.call(attempts=3)[0]["text"], "item one")
        self.assertEqual(self.invocations(), 2)

    def test_needs_result_for_every_tool(self):
        self.stream(good_stream(TOOL_A))
        with self.assertRaises(claude_bridge.BridgeError):
            self.call({TOOL_A: {}, TOOL_B: {}}, attempts=2)
        self.assertEqual(self.invocations(), 2)

    def test_reported_denials_do_not_fail_required_success(self):
        denial = [{"tool_name": "Read", "tool_input": {"file_path": SECRET_OUTPUT}}]
        self.stream(good_stream().replace(
            '"permission_denials": []', f'"permission_denials": {json.dumps(denial)}'))
        self.assertEqual(self.call()[0]["text"], "item one")
        self.assertEqual(self.invocations(), 1)
        entry = self.usage_lines()[0]
        self.assertEqual(entry["denials"], 1)
        self.assertNotIn(SECRET_OUTPUT, json.dumps(entry))

    def test_denied_read_alongside_required_tool_succeeds(self):
        self.stream(good_stream(extra=(
            tool_use("t8", "Read", {"file_path": SECRET_OUTPUT})
            + tool_result("t8", "denied by hook " + SECRET_OUTPUT, is_error=True)
            + tool_use("t9", "Grep", {"pattern": "x"}))))
        out = self.call()
        self.assertEqual([r["name"] for r in out], [TOOL_A])
        entry = self.usage_lines()[0]
        self.assertEqual((entry["outcome"], entry["denials"]), ("ok", 2))
        self.assertNotIn(SECRET_OUTPUT, json.dumps(entry))

    def test_denied_optional_tool_does_not_fail_run(self):
        pinned = {TOOL_A: {}, TOOL_B: {"id": "1"}}
        self.stream(good_stream(extra=(
            tool_use("t8", TOOL_B, {"id": "2"})
            + tool_result("t8", "denied", is_error=True))))
        out = self.call(pinned, required=[TOOL_A], attempts=1)
        self.assertEqual([r["name"] for r in out], [TOOL_A])
        self.assertEqual(self.usage_lines()[0]["denials"], 1)

    def test_denied_required_tool_yields_no_result_and_retries(self):
        pinned = {TOOL_A: {"q": "pinned"}}
        self.stream(good_stream(extra=""
                                ).replace('"q": "x"', '"q": "other"')
                    .replace('"item one"', '"item one", "is_error": true'))
        with self.assertRaises(claude_bridge.BridgeError):
            self.call(pinned, attempts=2)
        self.assertEqual(self.invocations(), 2)

    def test_pin_mismatch_that_ran_anyway_fails(self):
        # The hook "allowed" it (real non-error result) but the pins differ.
        self.stream(good_stream())
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call({TOOL_A: {"q": "pinned"}})
        self.assertIn("ran despite allowlist", str(ctx.exception))
        self.assertIn(TOOL_A, str(ctx.exception))
        self.assertEqual(self.invocations(), 1)

    def test_builtin_that_ran_anyway_fails(self):
        self.stream(good_stream(extra=(
            tool_use("t8", "Read", {"file_path": "x"})
            + tool_result("t8", SECRET_OUTPUT))))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertNotIn(SECRET_OUTPUT, str(ctx.exception))

    def test_tool_search_query_rechecked(self):
        self.stream(
            init()
            + tool_use("t1", "ToolSearch", {"query": "select:mcp__other__x"})
            + tool_result("t1", [{"type": "tool_reference"}])
            + result())
        with self.assertRaises(claude_bridge.BridgeError):
            self.call()

    def test_complete_still_fails_on_denials(self):
        denial = [{"tool_name": "Bash", "tool_input": {}}]
        self.stream(init() + result().replace(
            '"permission_denials": []', f'"permission_denials": {json.dumps(denial)}'))
        with self.assertRaises(claude_bridge.BridgeError):
            claude_bridge.complete("p", model="sonnet", base_dir=self.base)

    def test_no_init_event_fails(self):
        self.stream(good_stream().replace(init(), "", 1))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertIn("init", str(ctx.exception))

    def test_off_allowlist_tool_use_fails(self):
        self.stream(good_stream(extra=tool_use(
            "t9", "mcp__claude_ai_Example__delete_item", {"id": SECRET_OUTPUT})))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertIn("outside allowlist", str(ctx.exception))
        self.assertNotIn(SECRET_OUTPUT, str(ctx.exception))
        self.assertNotIn("delete_item", str(ctx.exception))
        self.assertIn("<invalid>", str(ctx.exception))
        self.assertEqual(self.invocations(), 1)
        self.assertNotIn("delete_item", json.dumps(self.usage_lines()))

    def test_known_builtin_name_is_reported(self):
        self.stream(good_stream(extra=tool_use("t9", "Bash", {"command": "x"})))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertIn("Bash", str(ctx.exception))

    def test_malformed_tool_names_never_echoed(self):
        for bad in ("evil\nname " + SECRET_OUTPUT, "a" * 129, 7, None):
            self.stream(good_stream(extra=tool_use("t9", bad)))
            with self.assertRaises(claude_bridge.BridgeError) as ctx:
                self.call()
            self.assertIn("<invalid>", str(ctx.exception))
            self.assertNotIn(SECRET_OUTPUT, str(ctx.exception))
        self.assertNotIn(SECRET_OUTPUT, json.dumps(self.usage_lines()))

    def test_partial_default_still_raises(self):
        self.stream(good_stream().replace(
            result(), result(subtype="error_max_turns", is_error=True)))
        with self.assertRaises(claude_bridge.BridgeError):
            self.call()

    def test_accept_partial_returns_collected_results(self):
        for subtype in ("error_max_turns", "error_max_budget_usd"):
            self.stream(good_stream().replace(
                result(), result(subtype=subtype, is_error=True)))
            out = self.call(accept_partial=True)
            self.assertTrue(out.partial)
            self.assertEqual(out[0]["text"], "item one")
            self.assertEqual(self.usage_lines()[-1]["outcome"], "partial")

    def test_accept_partial_needs_every_required_tool(self):
        self.stream(good_stream().replace(
            result(), result(subtype="error_max_turns", is_error=True)))
        with self.assertRaises(claude_bridge.BridgeError):
            self.call({TOOL_A: {}, TOOL_B: {}}, accept_partial=True)
        self.assertEqual(self.invocations(), 1)

    def test_accept_partial_ignores_other_failures(self):
        self.stream(good_stream().replace(
            result(), result(subtype="error_during_execution", is_error=True)))
        with self.assertRaises(claude_bridge.BridgeError):
            self.call(accept_partial=True)

    def test_normal_success_is_not_partial(self):
        self.stream(good_stream())
        self.assertFalse(self.call(accept_partial=True).partial)
        self.assertFalse(self.call().partial)

    def test_unexpected_builtin_in_init_fails(self):
        self.stream(good_stream().replace(
            '"Grep"]', '"Grep", "NewShinyTool", "mcp__claude_ai_Example__x"]', 1))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertIn("NewShinyTool", str(ctx.exception))

    def test_non_success_subtype_fails(self):
        for subtype, is_error in (("error_max_turns", False), ("success", True)):
            self.stream(good_stream().replace(
                result(), result(subtype=subtype, is_error=is_error)))
            with self.assertRaises(claude_bridge.BridgeError):
                self.call()

    def test_error_subtype_named_in_message(self):
        self.stream(init() + result(subtype="error_max_budget_usd", is_error=True))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertIn("error_max_budget_usd", str(ctx.exception))

    def test_tool_result_error_fails(self):
        self.stream(
            init() + tool_use("t2", TOOL_A) + tool_result(
                "t2", SECRET_OUTPUT, is_error=True) + result())
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertNotIn(SECRET_OUTPUT, str(ctx.exception))
        self.assertEqual(self.invocations(), 1)

    def test_optional_tool_error_is_returned_not_fatal(self):
        self.stream(
            init() + tool_use("t1", TOOL_A) + tool_result("t1", "listing")
            + tool_use("t2", TOOL_B) + tool_result("t2", SECRET_OUTPUT, is_error=True)
            + result())
        out = self.call({TOOL_A: {}, TOOL_B: {}}, required=[TOOL_A], attempts=1)
        self.assertEqual([(r["name"], r["is_error"]) for r in out],
                         [(TOOL_A, False), (TOOL_B, True)])

    def test_nonzero_exit_fails(self):
        self.stream(good_stream() + SECRET_OUTPUT)
        (self.stub_dir / "exit").write_text("3")
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertIn("3", str(ctx.exception))
        self.assertNotIn(SECRET_OUTPUT, str(ctx.exception))
        self.assertEqual(self.invocations(), 1)

    def test_timeout_fails(self):
        (self.stub_dir / "sleep").write_text("")
        self.stream(good_stream())
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(timeout=1)
        self.assertIn("timed out", str(ctx.exception))
        self.assertEqual(self.invocations(), 1)

    def test_stdin_write_is_covered_by_timeout(self):
        (self.stub_dir / "noread").write_text("")
        self.stream(good_stream())
        big = "x" * 5_000_000
        started = time.monotonic()
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            claude_bridge.call_tools(big, {TOOL_A: {}}, base_dir=self.base,
                                     connect_delay=0, timeout=1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("timed out", str(ctx.exception))

    def test_connect_delay_counts_against_timeout(self):
        self.stream(good_stream())
        started = time.monotonic()
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(connect_delay=30, timeout=1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("timed out", str(ctx.exception))

    def test_malformed_stream_line_fails(self):
        self.stream(init() + SECRET_OUTPUT + "\n" + result())
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertIn("unparseable", str(ctx.exception))
        self.assertNotIn(SECRET_OUTPUT, str(ctx.exception))

    def test_missing_result_event_fails(self):
        self.stream(init())
        with self.assertRaises(claude_bridge.BridgeError):
            self.call()
        self.assertEqual(self.invocations(), 1)

    def test_errors_never_contain_prompt(self):
        self.stream(init() + result())
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(attempts=1)
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))

    def test_usage_log_has_metadata_only(self):
        self.stream(good_stream(content=SECRET_OUTPUT))
        self.call(model="haiku")
        path = self.base / "state" / "claude-usage.jsonl"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text()
        self.assertNotIn(SECRET_PROMPT, text)
        self.assertNotIn(SECRET_OUTPUT, text)
        entry = self.usage_lines()[0]
        self.assertEqual(entry["model"], "haiku")
        self.assertEqual(entry["tools"], [TOOL_A])
        self.assertEqual(entry["attempt"], 1)
        self.assertEqual(entry["outcome"], "ok")
        self.assertEqual(entry["total_cost_usd"], 0.01)
        self.assertEqual((entry["input_tokens"], entry["output_tokens"]), (11, 7))
        self.assertIn("duration_ms", entry)
        self.assertIn("ts", entry)

    def test_run_dir_private_then_removed(self):
        self.stream(good_stream())
        self.call()
        cwd = Path((self.stub_dir / "cwd-1.txt").read_text())
        self.assertEqual(cwd.parent.name, "claude-runs")
        runs = self.base / "state" / "claude-runs"
        self.assertEqual(stat.S_IMODE(runs.stat().st_mode), 0o700)
        self.assertEqual(list(runs.iterdir()), [])

    def test_run_dir_removed_on_error(self):
        self.stream(init())
        with self.assertRaises(claude_bridge.BridgeError):
            self.call()
        self.assertEqual(list((self.base / "state" / "claude-runs").iterdir()), [])

    def test_required_subset_allows_missing_optional_tool(self):
        self.stream(good_stream())
        out = self.call({TOOL_A: {}, TOOL_B: {}}, required=[TOOL_A], attempts=1)
        self.assertEqual([r["name"] for r in out], [TOOL_A])
        self.assertEqual(self.invocations(), 1)
        self.assertEqual(self.usage_lines()[0]["outcome"], "ok")

    def test_required_tool_missing_still_retries_and_fails(self):
        self.stream(good_stream())
        with self.assertRaises(claude_bridge.BridgeError):
            self.call({TOOL_A: {}, TOOL_B: {}}, required=[TOOL_B], attempts=2)
        self.assertEqual(self.invocations(), 2)

    def test_required_must_be_allowlisted(self):
        with self.assertRaises(claude_bridge.BridgeError):
            self.call({TOOL_A: {}}, required=[TOOL_B])
        self.assertEqual(self.invocations(), 0)

    def test_dry_run_spawns_nothing(self):
        with self.assertRaises(claude_bridge.BridgeDryRun):
            self.call(dry_run=True)
        self.assertEqual(self.invocations(), 0)
        self.assertFalse((self.base / "state").exists())


class CompleteTest(BridgeBase):
    def test_returns_result_text_with_no_allowed_tools(self):
        self.stream(init() + result(text="the summary"))
        out = claude_bridge.complete(SECRET_PROMPT, model="sonnet",
                                     base_dir=self.base)
        self.assertEqual(out, "the summary")
        argv = self.argv()
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
        self.assertIn(SECRET_PROMPT, (self.stub_dir / "stdin-1.txt").read_text())
        self.assertNotIn(SECRET_PROMPT, " ".join(argv))

    def test_any_tool_use_fails(self):
        self.stream(init() + tool_use("t1", "ToolSearch", {"query": "select:x"})
                    + result())
        with self.assertRaises(claude_bridge.BridgeError):
            claude_bridge.complete("p", model="sonnet", base_dir=self.base)

    def test_empty_result_fails(self):
        self.stream(init() + result(text=""))
        with self.assertRaises(claude_bridge.BridgeError):
            claude_bridge.complete("p", model="sonnet", base_dir=self.base)


class HookTest(unittest.TestCase):
    TOOLS = {TOOL_A: {"calendar": "primary"}, TOOL_B: {}}

    def decide(self, name, tool_input):
        return claude_bridge_hook.decide(
            {"tool_name": name, "tool_input": tool_input}, self.TOOLS)[0]

    def test_allows_pinned_match(self):
        self.assertTrue(self.decide(TOOL_A, {"calendar": "primary", "x": 1}))
        self.assertTrue(self.decide(TOOL_B, {"anything": 1}))

    def test_denies_pin_mismatch_or_missing(self):
        self.assertFalse(self.decide(TOOL_A, {"calendar": "other"}))
        self.assertFalse(self.decide(TOOL_A, {}))

    def test_denies_off_list_and_builtins(self):
        for name in ("mcp__claude_ai_Example__delete_item", "Read", "Glob",
                     "Grep", "Bash"):
            self.assertFalse(self.decide(name, {"file_path": "x"}), name)

    def test_pins_need_exact_json_type(self):
        tools = {TOOL_A: {"flag": True, "n": 1, "tags": ["a"]}}

        def ok(**given):
            return claude_bridge_hook.decide(
                {"tool_name": TOOL_A, "tool_input": given}, tools)[0]

        self.assertTrue(ok(flag=True, n=1, tags=["a"]))
        self.assertFalse(ok(flag=1, n=1, tags=["a"]))
        self.assertFalse(ok(flag=True, n=True, tags=["a"]))
        self.assertFalse(ok(flag=True, n=1.0, tags=["a"]))
        self.assertFalse(ok(flag=True, n=1, tags=[True]))

    def test_tool_search_select(self):
        self.assertTrue(self.decide(
            "ToolSearch", {"query": f"select:{TOOL_A},{TOOL_B}"}))
        self.assertFalse(self.decide(
            "ToolSearch", {"query": f"select:{TOOL_A},mcp__other__x"}))
        self.assertFalse(self.decide("ToolSearch", {"query": "calendar"}))
        self.assertFalse(self.decide("ToolSearch", {"query": "select:"}))
        self.assertFalse(self.decide("ToolSearch", {}))

    def run_main(self, stdin_text, allowlist=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.json"
            path.write_text(json.dumps({"tools": self.TOOLS})
                            if allowlist is None else allowlist)
            out = io.StringIO()
            with redirect_stdout(out):
                code = claude_bridge_hook.main(
                    ["hook", str(path)], io.StringIO(stdin_text))
        return code, json.loads(out.getvalue())["hookSpecificOutput"]

    def test_main_allow_and_policy_deny(self):
        code, out = self.run_main(json.dumps(
            {"tool_name": TOOL_B, "tool_input": {}}))
        self.assertEqual((code, out["permissionDecision"]), (0, "allow"))
        code, out = self.run_main(json.dumps(
            {"tool_name": "Bash", "tool_input": {}}))
        self.assertEqual((code, out["permissionDecision"]), (0, "deny"))

    def test_malformed_input_denies_with_exit_2(self):
        for stdin_text, allowlist in (("not json", None), ("[]", None),
                                      ("{}", None),
                                      (json.dumps({"tool_name": TOOL_B,
                                                   "tool_input": {}}), "{bad")):
            code, out = self.run_main(stdin_text, allowlist)
            self.assertEqual((code, out["permissionDecision"]), (2, "deny"))

    def test_emit_failure_denies_with_exit_2(self):
        real = claude_bridge_hook._emit
        calls = []

        def flaky(allowed, reason):
            calls.append(allowed)
            if len(calls) == 1:
                raise OSError("stdout closed")
            real(allowed, reason)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.json"
            path.write_text(json.dumps({"tools": self.TOOLS}))
            out = io.StringIO()
            with mock.patch.object(claude_bridge_hook, "_emit", flaky), \
                    redirect_stdout(out):
                code = claude_bridge_hook.main(["hook", str(path)], io.StringIO(
                    json.dumps({"tool_name": TOOL_B, "tool_input": {}})))
        self.assertEqual(code, 2)
        self.assertEqual(calls, [True, False])
        self.assertEqual(json.loads(out.getvalue())[
            "hookSpecificOutput"]["permissionDecision"], "deny")

    def test_emit_always_failing_still_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.json"
            path.write_text(json.dumps({"tools": self.TOOLS}))
            with mock.patch.object(claude_bridge_hook, "_emit",
                                   side_effect=OSError("x")):
                code = claude_bridge_hook.main(["hook", str(path)], io.StringIO(
                    json.dumps({"tool_name": TOOL_B, "tool_input": {}})))
        self.assertEqual(code, 2)

    def test_runs_as_isolated_script(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            allow = Path(tmp) / "allow.json"
            allow.write_text(json.dumps({"tools": self.TOOLS}))
            proc = subprocess.run(
                [sys.executable, "-I", str(claude_bridge.HOOK_PATH), str(allow)],
                input=json.dumps({"tool_name": TOOL_B, "tool_input": {}}),
                capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('"allow"', proc.stdout)


class ClaudeBinTest(unittest.TestCase):
    def test_falls_back_to_local_bin(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / ".local" / "bin" / "claude"
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\n")
            env = {k: v for k, v in os.environ.items()
                   if k != "WORKSPACE_DAEMON_CLAUDE_BIN"}
            env["PATH"] = ""
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("pathlib.Path.home", return_value=Path(home)):
                self.assertEqual(shell.claude_bin(), str(target))
                target.unlink()
                with self.assertRaises(shell.MissingBinary):
                    shell.claude_bin()

    def test_env_override_must_exist(self):
        with mock.patch.dict(os.environ, {
                "WORKSPACE_DAEMON_CLAUDE_BIN": "/nonexistent/claude"}):
            with self.assertRaises(shell.MissingBinary):
                shell.claude_bin()


class AnalyzeDispatchTest(unittest.TestCase):
    def tearDown(self):
        llm.configure(None)

    def test_claude_provider_uses_bridge(self):
        llm.configure("/base")
        routine = {"analyze": {"provider": "claude", "model": "sonnet"}}
        with mock.patch.object(claude_bridge, "complete",
                               return_value="text") as complete, \
                mock.patch.object(llm, "run_json") as run_json:
            self.assertEqual(llm.analyze(routine, "prompt"), "text")
        complete.assert_called_once_with("prompt", model="sonnet",
                                         base_dir="/base")
        run_json.assert_not_called()

    def test_claude_provider_needs_base_dir(self):
        routine = {"analyze": {"provider": "claude", "model": "sonnet"}}
        with self.assertRaises(RuntimeError):
            llm.analyze(routine, "prompt")

    def test_other_providers_still_use_yoetz(self):
        routine = {"analyze": {"provider": "example", "model": "m"}}
        with mock.patch.object(llm, "yoetz_bin", return_value="yoetz"), \
                mock.patch.object(llm, "run_json",
                                  return_value={"content": "hi"}) as run_json, \
                mock.patch.object(claude_bridge, "complete") as complete:
            self.assertEqual(llm.analyze(routine, "p"), "hi")
        cmd = run_json.call_args[0][0]
        self.assertEqual(cmd[:2], ["yoetz", "ask"])
        self.assertIn("example", cmd)
        complete.assert_not_called()



class ClaudeProviderConfigTest(unittest.TestCase):
    def _problems(self, kind):
        from workspace_daemon import config
        routine = {
            "id": "r", "source": {"kind": kind},
            "analyze": {"provider": "claude", "model": "sonnet", "instruction": "x"},
        }
        return [p for p in config.validate(routine) if "provider claude" in p]

    def test_transcript_sources_reject_claude_provider(self):
        self.assertTrue(self._problems("mila"))
        self.assertTrue(self._problems("whisper"))

    def test_other_sources_accept_claude_provider(self):
        self.assertEqual(self._problems("gmail"), [])


if __name__ == "__main__":
    unittest.main()
