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


class UsageScopeAndBudgetTest(BridgeBase):
    def test_usage_scope_tags_log_lines_and_resets(self):
        self.stream(good_stream())
        with claude_bridge.usage_scope("alpha"):
            claude_bridge.call_tools(
                SECRET_PROMPT, {TOOL_A: {}}, base_dir=self.base,
                connect_delay=0,
            )
        claude_bridge.call_tools(
            SECRET_PROMPT, {TOOL_A: {}}, base_dir=self.base, connect_delay=0,
        )
        first, second = self.usage_lines()
        self.assertEqual(first["routine"], "alpha")
        self.assertNotIn("routine", second)

    def write_spend(self, cost):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path = self.base / "state" / "claude-usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": ts, "total_cost_usd": cost}) + "\n")

    def test_global_cap_refuses_before_spawning(self):
        self.stream(good_stream())
        self.write_spend(3.0)
        (self.base / "state" / "budget.yaml").write_text(
            "daily_usd: 2\nday_timezone: UTC\n")
        with self.assertRaisesRegex(
                claude_bridge.BridgeError, "^daily budget exhausted$"):
            claude_bridge.call_tools(
                SECRET_PROMPT, {TOOL_A: {}}, base_dir=self.base,
                connect_delay=0,
            )
        with self.assertRaises(claude_bridge.BridgeError):
            claude_bridge.complete(
                SECRET_PROMPT, model="haiku", base_dir=self.base)
        self.assertEqual(self.invocations(), 0)
        self.assertEqual(len(self.usage_lines()), 1)  # nothing new logged

    def test_global_refusal_is_a_budget_exhausted_error(self):
        self.write_spend(3.0)
        (self.base / "state" / "budget.yaml").write_text("daily_usd: 2\n")
        with self.assertRaises(claude_bridge.BudgetExhausted):
            claude_bridge.complete("p", model="haiku", base_dir=self.base)

    def test_scope_cap_refuses_only_that_routine(self):
        self.stream(good_stream())
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path = self.base / "state" / "claude-usage.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(
            {"ts": ts, "total_cost_usd": 2.0, "routine": "alpha"}) + "\n")
        with claude_bridge.usage_scope("alpha", daily_usd=1.0) as scope:
            with self.assertRaises(claude_bridge.BudgetExhausted):
                claude_bridge.complete("p", model="haiku", base_dir=self.base)
            self.assertIsInstance(scope.stop, claude_bridge.BudgetExhausted)
            self.assertIs(claude_bridge.scope_stop(), scope.stop)
        with claude_bridge.usage_scope("beta", daily_usd=1.0):
            claude_bridge.call_tools(
                "p", {TOOL_A: {}}, base_dir=self.base, connect_delay=0)
        self.assertIsNone(claude_bridge.scope_stop())

    def test_pause_refuses_calls_unless_overridden(self):
        self.stream(good_stream())
        (self.base / "state").mkdir()
        (self.base / "state" / "PAUSED").write_text("hold on\n")
        with self.assertRaisesRegex(claude_bridge.PausedError, "hold on"):
            claude_bridge.call_tools(
                "p", {TOOL_A: {}}, base_dir=self.base, connect_delay=0)
        self.assertEqual(self.invocations(), 0)
        with claude_bridge.pause_override(True):
            out = claude_bridge.call_tools(
                "p", {TOOL_A: {}}, base_dir=self.base, connect_delay=0)
        self.assertEqual(out[0]["text"], "item one")

    def test_run_without_a_result_logs_its_budget_as_an_estimate(self):
        (self.stub_dir / "sleep").write_text("")
        with self.assertRaises(claude_bridge.BridgeError):
            claude_bridge.call_tools(
                "p", {TOOL_A: {}}, base_dir=self.base, connect_delay=0,
                timeout=1, attempts=1, max_budget_usd=0.4)
        entry = self.usage_lines()[0]
        self.assertIsNone(entry["total_cost_usd"])
        self.assertEqual(entry["cost_estimate_usd"], 0.4)
        self.assertAlmostEqual(
            claude_bridge.spent_today(self.base, tz=claude_bridge.zoneinfo.ZoneInfo("UTC")),
            0.4)

    def test_failed_usage_write_leaves_a_marker_until_the_next_success(self):
        self.stream(good_stream())
        real_open = os.open

        def failing_open(path, *args, **kwargs):
            if str(path).endswith("claude-usage.jsonl"):
                raise OSError("disk full")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(claude_bridge.os, "open", failing_open):
            claude_bridge.call_tools(
                "p", {TOOL_A: {}}, base_dir=self.base, connect_delay=0)
        self.assertIsNotNone(claude_bridge.usage_write_failed(self.base))
        claude_bridge.call_tools(
            "p", {TOOL_A: {}}, base_dir=self.base, connect_delay=0)
        self.assertIsNone(claude_bridge.usage_write_failed(self.base))

    def test_under_the_cap_still_runs(self):
        self.stream(good_stream())
        self.write_spend(1.0)
        (self.base / "state" / "budget.yaml").write_text(
            "daily_usd: 2\nday_timezone: UTC\n")
        out = claude_bridge.call_tools(
            SECRET_PROMPT, {TOOL_A: {}}, base_dir=self.base, connect_delay=0,
        )
        self.assertEqual(out[0]["text"], "item one")

    def test_unreadable_budget_file_fails_closed(self):
        self.stream(good_stream())
        (self.base / "state").mkdir()
        (self.base / "state" / "budget.yaml").write_text("daily_usd: nope\n")
        with self.assertRaises(claude_bridge.BridgeError):
            claude_bridge.call_tools(
                SECRET_PROMPT, {TOOL_A: {}}, base_dir=self.base,
                connect_delay=0,
            )
        self.assertEqual(self.invocations(), 0)


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
        self.assertNotIn("--append-system-prompt", argv)
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


class McpConfigTest(BridgeBase):
    """`call_tools(..., mcp_config=<file>)`: the argv, the file checks and the
    unchanged default."""

    def call(self, tools=None, **kw):
        kw.setdefault("base_dir", self.base)
        kw.setdefault("connect_delay", 0)
        return claude_bridge.call_tools(
            SECRET_PROMPT, tools if tools is not None else {TOOL_A: {}}, **kw
        )

    def make(self, mode=0o600, name="mcp.json"):
        path = self.root / name
        path.write_text('{"mcpServers": {}}')
        os.chmod(path, mode)
        return path

    def test_the_default_leaves_the_argv_unchanged(self):
        self.stream(good_stream())
        self.call()
        argv = self.argv()
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--strict-mcp-config", argv)
        # the same list the builder produced before the option existed
        built = claude_bridge._build_command(
            "haiku", "s.json", ["ToolSearch", TOOL_A], 6, 0.25,
            claude_bridge.DEFAULT_DISALLOWED_TOOLS)
        self.assertEqual(built, claude_bridge._build_command(
            "haiku", "s.json", ["ToolSearch", TOOL_A], 6, 0.25,
            claude_bridge.DEFAULT_DISALLOWED_TOOLS, None))
        self.assertEqual(built[-2:], ["--allowedTools", f"ToolSearch,{TOOL_A}"])
        self.assertNotIn("--strict-mcp-config", built)

    def test_a_file_adds_the_two_flags_and_keeps_the_restrictions(self):
        path = self.make()
        self.stream(good_stream())
        out = self.call(mcp_config=str(path))
        self.assertEqual(out[0]["name"], TOOL_A)
        argv = self.argv()
        # claude reads a private copy of the checked bytes, not the original
        copy = argv[argv.index("--mcp-config") + 1]
        self.assertTrue(copy.endswith("/mcp-config.json"))
        self.assertNotEqual(copy, str(path))
        self.assertEqual(argv.count("--strict-mcp-config"), 1)
        self.assertEqual(argv.index("--strict-mcp-config"),
                         argv.index("--mcp-config") + 2)
        self.assertEqual(argv[argv.index("--allowedTools") + 1],
                         f"ToolSearch,{TOOL_A}")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("Bash", argv[argv.index("--disallowedTools") + 1].split(","))
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        settings_doc = json.loads((self.stub_dir / "settings-1.json").read_text())
        self.assertIn("claude_bridge_hook.py",
                      settings_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"])

    def test_the_stream_recheck_still_limits_the_run_to_the_pinned_tool(self):
        path = self.make()
        self.stream(good_stream(extra=tool_use(
            "t9", "mcp__other_server__do_thing", {"x": 1})))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(mcp_config=str(path))
        self.assertIn("outside allowlist", str(ctx.exception))

    def test_the_path_is_not_in_errors_or_the_prompt(self):
        path = self.make()
        self.stream(good_stream())
        self.call(mcp_config=str(path))
        stdin = (self.stub_dir / "stdin-1.txt").read_text()
        self.assertNotIn(str(path), stdin)

    def refuses(self, path, fragment):
        before = self.invocations()
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(mcp_config=path)
        self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(self.invocations(), before)       # nothing was spawned
        self.assertFalse((self.base / "state").exists())    # and nothing logged

    def test_relative_and_odd_paths_are_refused(self):
        self.make()
        for bad in ("mcp.json", "./mcp.json", "", "~/mcp.json"):
            self.refuses(bad, "mcp_config")
        for bad in (5, None.__class__, ["a"]):
            self.refuses(bad, "mcp_config")

    def test_a_missing_file_a_directory_and_a_fifo_are_refused(self):
        self.refuses(str(self.root / "missing.json"), "cannot open")
        self.refuses(str(self.root), "not a regular file")
        fifo = self.root / "fifo.json"
        os.mkfifo(fifo, 0o600)
        self.refuses(str(fifo), "not a regular file")

    def test_a_symlink_is_refused_even_to_a_good_file(self):
        target = self.make()
        link = self.root / "link.json"
        link.symlink_to(target)
        self.refuses(str(link), "cannot open")

    def test_a_file_anyone_else_can_reach_is_refused(self):
        for mode in (0o644, 0o640, 0o660, 0o604, 0o606, 0o666, 0o610):
            path = self.make(mode, name=f"m{mode:o}.json")
            self.refuses(str(path), "accessible to others")
        for mode in (0o600, 0o400):
            path = self.make(mode, name=f"ok{mode:o}.json")
            self.assertEqual(claude_bridge.check_mcp_config(path), str(path))

    def test_a_file_of_another_owner_is_refused(self):
        path = self.make()
        with mock.patch.object(claude_bridge.os, "getuid",
                               return_value=os.getuid() + 1):
            self.refuses(str(path), "not owned by you")

    def test_the_check_judges_the_open_file(self):
        path = self.make()
        real_fstat = os.fstat
        seen = []

        def spy(fd):
            seen.append(fd)
            return real_fstat(fd)
        with mock.patch.object(claude_bridge.os, "fstat", spy):
            claude_bridge.check_mcp_config(str(path))
        self.assertEqual(len(seen), 1)

    def test_claude_gets_a_private_copy_of_the_checked_bytes(self):
        path = self.make()
        original = path.read_bytes()
        seen = {}

        def spawn(cmd, prompt, cwd, timeout, connect_delay):
            copy = Path(cmd[cmd.index("--mcp-config") + 1])
            seen["copy"] = copy
            seen["bytes"] = copy.read_bytes()
            seen["mode"] = stat.S_IMODE(copy.stat().st_mode)
            seen["dir_mode"] = stat.S_IMODE(copy.parent.stat().st_mode)
            path.write_text('{"mcpServers": {"evil": {}}}')   # swapped after the check
            seen["after"] = copy.read_bytes()
            return 0, good_stream()
        with mock.patch.object(claude_bridge, "_spawn", spawn):
            self.call(mcp_config=str(path))
        self.assertEqual(seen["bytes"], original)
        self.assertEqual(seen["after"], original)           # the copy is unaffected
        self.assertEqual((seen["mode"], seen["dir_mode"]), (0o600, 0o700))
        self.assertNotEqual(seen["copy"], path)
        self.assertFalse(seen["copy"].exists())             # removed with the run dir

    def test_read_mcp_config_returns_the_bytes_and_caps_the_size(self):
        path = self.make()
        self.assertEqual(claude_bridge.read_mcp_config(path),
                         b'{"mcpServers": {}}')
        big = self.root / "big.json"
        big.write_bytes(b"x" * (claude_bridge.MCP_CONFIG_MAX_BYTES + 1))
        os.chmod(big, 0o600)
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            claude_bridge.read_mcp_config(big)
        self.assertIn("too large", str(ctx.exception))

    def test_dry_run_and_empty_tools_still_refuse_first(self):
        path = self.make()
        with self.assertRaises(claude_bridge.BridgeDryRun):
            self.call(mcp_config=str(path), dry_run=True)
        with self.assertRaises(claude_bridge.BridgeError):
            claude_bridge.call_tools("p", {}, base_dir=self.base,
                                     mcp_config=str(path))
        self.assertEqual(self.invocations(), 0)

    def test_the_file_is_rechecked_on_every_attempt(self):
        path = self.make()
        self.stream(init() + result())
        with self.assertRaises(claude_bridge.BridgeError):
            self.call(mcp_config=str(path), attempts=2)       # no tool result
        self.assertEqual(self.invocations(), 2)
        for argv_n in (1, 2):
            self.assertIn("--mcp-config", self.argv(argv_n))


class MaxCallsTest(BridgeBase):
    """`call_tools(..., max_calls=N)`: the hook allows N calls of a pinned
    tool and denies the next; the stream re-check fails a run in which a call
    beyond the cap ran anyway. The default (None) is unchanged."""

    def call(self, tools=None, **kw):
        kw.setdefault("base_dir", self.base)
        kw.setdefault("connect_delay", 0)
        return claude_bridge.call_tools(
            SECRET_PROMPT, tools if tools is not None else {TOOL_A: {"q": "x"}},
            **kw)

    def twice(self, second_error=False):
        return (init()
                + tool_use("t1", "ToolSearch", {"query": f"select:{TOOL_A}"})
                + tool_result("t1", "ok")
                + tool_use("t2", TOOL_A, {"q": "x"}) + tool_result("t2", "sent")
                + tool_use("t3", TOOL_A, {"q": "x"})
                + tool_result("t3", "denied" if second_error else "sent again",
                              is_error=second_error)
                + result())

    def test_the_default_allows_repeated_calls_as_before(self):
        self.stream(self.twice())
        out = self.call()
        self.assertEqual([r["text"] for r in out], ["sent", "sent again"])

    def test_a_second_call_that_ran_anyway_fails_the_run(self):
        self.stream(self.twice())
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(max_calls=1)
        self.assertIn("ran despite allowlist check", str(ctx.exception))

    def test_a_second_call_the_hook_denied_is_ignored(self):
        self.stream(self.twice(second_error=True))
        out = self.call(max_calls=1)
        self.assertEqual([r["text"] for r in out], ["sent"])    # one result only

    def parallel(self, first_error, second_error, texts=("denied", "sent")):
        """Two identical calls issued together; the results come back in the
        order given, each one an error or not."""
        blocks = line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": TOOL_A, "input": {"q": "x"}},
            {"type": "tool_use", "id": "t3", "name": TOOL_A, "input": {"q": "x"}}]}})
        return (init()
                + tool_use("t1", "ToolSearch", {"query": f"select:{TOOL_A}"})
                + tool_result("t1", "ok") + blocks
                + tool_result("t2", texts[0], is_error=first_error)
                + tool_result("t3", texts[1], is_error=second_error)
                + result())

    def test_parallel_calls_where_only_the_second_ran_is_a_success(self):
        # The hook gave the slot to the second process: call #1 was denied
        # (an error result), call #2 ran. The slot is not decided by stream
        # order, so this is the one real send, not "tool returned an error".
        self.stream(self.parallel(True, False))
        out = self.call(max_calls=1)
        self.assertEqual([(r["text"], r["is_error"]) for r in out],
                         [("sent", False)])

    def test_parallel_calls_where_only_the_first_ran_is_a_success(self):
        self.stream(self.parallel(False, True, texts=("sent", "denied")))
        out = self.call(max_calls=1)
        self.assertEqual([(r["text"], r["is_error"]) for r in out],
                         [("sent", False)])

    def test_parallel_calls_that_both_ran_fail_the_run(self):
        self.stream(self.parallel(False, False, texts=("sent", "sent again")))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(max_calls=1)
        self.assertIn("ran despite allowlist check", str(ctx.exception))

    def test_parallel_calls_with_no_result_left_are_unclear_not_an_error(self):
        self.stream(self.parallel(True, True, texts=("denied", "denied")))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(max_calls=1)
        message = str(ctx.exception)
        self.assertIn("unclear after 2 calls", message)
        self.assertNotIn("tool returned an error", message)

    def test_one_call_that_errors_is_still_a_plain_tool_error(self):
        stream = (init()
                  + tool_use("t2", TOOL_A, {"q": "x"})
                  + tool_result("t2", "invalid_auth", is_error=True) + result())
        self.stream(stream)
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call(max_calls=1)
        self.assertIn("tool returned an error", str(ctx.exception))

    def test_without_a_cap_an_error_result_still_fails_at_once(self):
        self.stream(self.parallel(True, False))
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.call()
        self.assertIn("tool returned an error", str(ctx.exception))

    def test_two_allowed_calls_pass_with_a_cap_of_two(self):
        self.stream(self.twice())
        self.assertEqual(len(self.call(max_calls=2)), 2)

    def test_a_call_with_other_pins_does_not_use_the_slot(self):
        stream = (init()
                  + tool_use("t2", TOOL_A, {"q": "other"}) + tool_result(
                      "t2", "denied", is_error=True)
                  + tool_use("t3", TOOL_A, {"q": "x"}) + tool_result("t3", "sent")
                  + result())
        self.stream(stream)
        out = self.call(max_calls=1)
        self.assertEqual([r["text"] for r in out], ["sent"])

    def test_bad_values_are_refused_before_any_run(self):
        for bad in (0, -1, True, 1.5, "1"):
            with self.assertRaises(claude_bridge.BridgeError):
                self.call(max_calls=bad)
        self.assertEqual(self.invocations(), 0)

    def test_the_cap_travels_in_the_allowlist_file(self):
        seen = {}

        def spawn(cmd, prompt, cwd, timeout, connect_delay):
            settings_path = Path(cmd[cmd.index("--settings") + 1])
            seen["doc"] = json.loads((settings_path.parent / "allowlist.json")
                                     .read_text())
            return 0, good_stream()
        with mock.patch.object(claude_bridge, "_spawn", spawn):
            self.call(max_calls=1, exact_args=True)
            self.call()
        self.assertEqual(seen["doc"]["max_calls"], None)       # the last (default) call
        with mock.patch.object(claude_bridge, "_spawn", spawn):
            self.call(max_calls=1, exact_args=True)
        self.assertEqual(seen["doc"], {"tools": {TOOL_A: {"q": "x"}},
                                       "exact_args": True, "max_calls": 1})


class HookMaxCallsTest(unittest.TestCase):
    """claude_bridge_hook.main: with `max_calls` in the allowlist file the
    hook claims one marker per allowed call in the run directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / "allowlist.json"

    def run_hook(self, name, tool_input, max_calls=1, tools=None):
        self.path.write_text(json.dumps({
            "tools": tools or {TOOL_A: {"q": "x"}, TOOL_B: {}},
            "exact_args": True, "max_calls": max_calls}))
        out = io.StringIO()
        payload = io.StringIO(json.dumps({"tool_name": name,
                                          "tool_input": tool_input}))
        with redirect_stdout(out):
            code = claude_bridge_hook.main(["hook", str(self.path)], payload)
        decision = json.loads(out.getvalue())["hookSpecificOutput"]
        return code, decision["permissionDecision"], decision["permissionDecisionReason"]

    def test_the_second_identical_call_is_denied(self):
        self.assertEqual(self.run_hook(TOOL_A, {"q": "x"})[:2], (0, "allow"))
        code, decision, reason = self.run_hook(TOOL_A, {"q": "x"})
        self.assertEqual((code, decision), (0, "deny"))
        self.assertIn("at most 1 time(s)", reason)
        self.assertEqual(self.run_hook(TOOL_A, {"q": "x"})[1], "deny")

    def test_a_denied_call_does_not_use_a_slot_and_slots_are_per_tool(self):
        self.assertEqual(self.run_hook(TOOL_A, {"q": "other"})[1], "deny")   # pin
        self.assertEqual(self.run_hook(TOOL_A, {"q": "x", "extra": 1})[1], "deny")
        self.assertEqual(self.run_hook(TOOL_A, {"q": "x"})[1], "allow")      # still free
        self.assertEqual(self.run_hook(TOOL_B, {})[1], "allow")              # own slot
        self.assertEqual(self.run_hook(TOOL_B, {})[1], "deny")

    def test_a_cap_of_three_allows_three(self):
        got = [self.run_hook(TOOL_A, {"q": "x"}, max_calls=3)[1] for _ in range(4)]
        self.assertEqual(got, ["allow", "allow", "allow", "deny"])

    def test_tool_search_is_not_counted(self):
        for _ in range(3):
            self.assertEqual(self.run_hook(
                "ToolSearch", {"query": f"select:{TOOL_A}"})[1], "allow")
        self.assertEqual(self.run_hook(TOOL_A, {"q": "x"})[1], "allow")

    def test_without_max_calls_nothing_is_counted_and_no_marker_is_written(self):
        self.path.write_text(json.dumps({"tools": {TOOL_A: {}}, "exact_args": False}))
        for _ in range(3):
            out = io.StringIO()
            with redirect_stdout(out):
                claude_bridge_hook.main(["hook", str(self.path)], io.StringIO(
                    json.dumps({"tool_name": TOOL_A, "tool_input": {}})))
            self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]
                             ["permissionDecision"], "allow")
        self.assertFalse((self.dir / "calls").exists())

    def test_a_bad_cap_in_the_file_denies_everything(self):
        for bad in (0, -2, True, "1", 1.5):
            self.assertEqual(self.run_hook(TOOL_A, {"q": "x"}, max_calls=bad)[:2],
                             (2, "deny"), repr(bad))

    def test_markers_are_private_and_unreadable_dir_fails_closed(self):
        self.run_hook(TOOL_A, {"q": "x"})
        marks = list((self.dir / "calls").iterdir())
        self.assertEqual(len(marks), 1)
        self.assertEqual(stat.S_IMODE(marks[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.dir / "calls").stat().st_mode), 0o700)
        blocker = self.dir / "calls"
        for m in marks:
            m.unlink()
        blocker.rmdir()
        blocker.write_text("not a directory")                 # cannot claim
        self.assertEqual(self.run_hook(TOOL_A, {"q": "x"})[:2], (2, "deny"))

    def test_the_real_bridge_denies_a_second_call_through_the_hook(self):
        # end to end with the hook as claude would run it: two processes
        import subprocess
        tools = {TOOL_A: {"q": "x"}}
        self.path.write_text(json.dumps(
            {"tools": tools, "exact_args": True, "max_calls": 1}))
        payload = json.dumps({"tool_name": TOOL_A, "tool_input": {"q": "x"}})
        got = []
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, "-I", str(claude_bridge.HOOK_PATH), str(self.path)],
                input=payload, capture_output=True, text=True)
            got.append(json.loads(proc.stdout)["hookSpecificOutput"]
                       ["permissionDecision"])
        self.assertEqual(got, ["allow", "deny"])


class StrictMcpCompleteTest(BridgeBase):
    def test_default_complete_argv_is_unchanged(self):
        self.stream(init() + result(text="x"))
        claude_bridge.complete("p", model="sonnet", base_dir=self.base)
        argv = self.argv()
        self.assertNotIn("--strict-mcp-config", argv)
        self.assertNotIn("--mcp-config", argv)

    def test_strict_mcp_loads_no_server_and_stays_a_no_tools_run(self):
        self.stream(init() + result(text="rewritten"))
        out = claude_bridge.complete("p", model="sonnet", base_dir=self.base,
                                     strict_mcp=True)
        self.assertEqual(out, "rewritten")
        argv = self.argv()
        self.assertEqual(argv.count("--strict-mcp-config"), 1)
        self.assertNotIn("--mcp-config", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "1")


class CompleteTest(BridgeBase):
    def test_returns_result_text_with_no_allowed_tools(self):
        self.stream(init() + result(text="the summary"))
        out = claude_bridge.complete(SECRET_PROMPT, model="sonnet",
                                     base_dir=self.base)
        self.assertEqual(out, "the summary")
        argv = self.argv()
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1],
                         claude_bridge.NO_TOOLS_NOTE)
        self.assertNotIn("--tools", argv)
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
