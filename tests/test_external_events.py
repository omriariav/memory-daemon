"""The `external_events` maintenance kind: a configured command feeds the
event stream."""
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import daemon as daemon_cli
from workspace_daemon import config, external_events, maintenance, runner


def routine(**overrides):
    cfg = {
        "kind": "external_events",
        "command": ["my-script.py"],
        "source": "sheetwatch",
    }
    cfg.update(overrides)
    return {
        "id": "ext-events",
        "enabled": True,
        "description": "Feed events from a local script.",
        "role": "maintenance",
        "schedule": {"every": "15m"},
        "maintenance": cfg,
    }


def problems(**overrides):
    return config.validate(routine(**overrides))


class ConfigTest(unittest.TestCase):
    def test_minimal_config_is_valid(self):
        self.assertEqual(problems(), [])

    def test_full_config_is_valid(self):
        self.assertEqual(problems(
            command=["~/bin/my-script.py", "--flag", "x"],
            timeout_s=1800, max_events=5, env={"MY_VAR": "1"},
        ), [])

    def test_command_must_be_nonempty_list_of_strings(self):
        for bad in ("my-script.py", [], [""], ["a", 3], None):
            with self.subTest(bad=bad):
                self.assertTrue(
                    any("maintenance.command" in p for p in problems(command=bad)))

    def test_command_required(self):
        r = routine()
        del r["maintenance"]["command"]
        self.assertTrue(
            any("maintenance.command" in p for p in config.validate(r)))

    def test_source_rules(self):
        for bad in ("", "Upper", "1abc", "a b", "x" * 33, 5, "gcal", "daemon",
                    "slack", "gdrive", "person", "slack-profile", "zoom"):
            with self.subTest(bad=bad):
                self.assertTrue(
                    any("maintenance.source" in p for p in problems(source=bad)))
        self.assertEqual(problems(source="a_b-c9"), [])
        r = routine()
        del r["maintenance"]["source"]
        self.assertTrue(any("maintenance.source" in p for p in config.validate(r)))

    def test_timeout_bounds(self):
        for bad in (0, -1, 1801, "5", True, 1.5):
            with self.subTest(bad=bad):
                self.assertTrue(
                    any("timeout_s" in p for p in problems(timeout_s=bad)))

    def test_max_events_bounds(self):
        for bad in (0, -3, "10", True):
            with self.subTest(bad=bad):
                self.assertTrue(
                    any("max_events" in p for p in problems(max_events=bad)))

    def test_env_must_map_names_to_strings(self):
        for bad in ({"A": 1}, {"1A": "x"}, ["A=1"], {"A B": "x"}):
            with self.subTest(bad=bad):
                self.assertTrue(any("env" in p for p in problems(env=bad)))

    def test_unknown_key_and_no_checkpoint_needed(self):
        self.assertTrue(any("unknown key" in p for p in problems(bogus=1)))
        self.assertFalse(any("checkpoint" in p for p in problems()))


SCRIPT = textwrap.dedent('''\
    import json, sys
    print(json.dumps({"events": [
        {"kind": "row_changed", "key": "sheet1:row7", "title": "Row 7 changed",
         "people": ["jane-doe"], "details": {"cell": "B7", "old": 1, "new": 2}},
        {"kind": "row_added", "key": "sheet1:row8", "title": "Row 8 added"},
    ]}))
''')


class RunFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name) / "base"
        self.base.mkdir()
        self.script_dir = Path(self._tmp.name) / "scripts"
        self.script_dir.mkdir()

    def script(self, body, name="s.py"):
        path = self.script_dir / name
        path.write_text(body)
        return [sys.executable, str(path)]

    def lines(self):
        path = self.base / "state" / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines()]

    def run_routine(self, body, dry_run=False, **overrides):
        r = routine(command=self.script(body), **overrides)
        return maintenance.run(self.base, r, dry_run=dry_run)


class RunTest(RunFixture):
    def test_valid_events_are_emitted(self):
        report = self.run_routine(SCRIPT)
        self.assertEqual(report["emitted"], 2)
        first, second = self.lines()
        self.assertEqual(first["source"], "sheetwatch")
        self.assertEqual(first["kind"], "row_changed")
        self.assertEqual(first["source_id"], "sheetwatch:sheet1:row7")
        self.assertEqual(first["title"], "Row 7 changed")
        self.assertEqual(first["people"], ["jane-doe"])
        self.assertEqual(first["details"], {"cell": "B7", "old": 1, "new": 2})
        self.assertIsNone(first["memory_id"])
        self.assertEqual(first["routine"], "ext-events")
        self.assertEqual(second["people"], [])
        self.assertNotIn("details", second)
        self.assertRegex(second["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_invalid_event_rejects_whole_batch(self):
        body = ('import json\nprint(json.dumps({"events": ['
                '{"kind": "ok", "key": "a", "title": "A"},'
                '{"kind": "Bad Kind", "key": "b", "title": "B"}]}))')
        with self.assertRaisesRegex(RuntimeError, "index 1"):
            self.run_routine(body)
        self.assertEqual(self.lines(), [])

    def test_event_field_rules(self):
        good = {"kind": "k", "key": "a", "title": "T"}
        cases = [
            {**good, "key": ""}, {**good, "key": "x" * 257},
            {**good, "title": "x" * 301}, {**good, "title": ""},
            {**good, "people": "jane"}, {**good, "people": [1]},
            {**good, "people": ["Jane Doe"]},
            {**good, "key": "a\nb"}, {**good, "title": "a\x00b"},
            {**good, "details": {"n": float("nan")}},
            {**good, "details": []},
            {**good, "details": {"big": "x" * 9000}},
            {**good, "ts": "2026-01-02T03:04:05Z"},
            {**good, "extra": 1}, "nope",
        ]
        for case in cases:
            with self.subTest(case=str(case)[:60]):
                with self.assertRaises(ValueError):
                    external_events.validate_event(case)
        external_events.validate_event({**good, "key": "x" * 256})

    def test_duplicate_kind_key_rejected(self):
        body = ('import json\nev = {"kind": "k", "key": "a", "title": "A"}\n'
                'print(json.dumps({"events": [ev, ev]}))')
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.run_routine(body)
        self.assertEqual(self.lines(), [])

    def test_ids_unique_within_and_across_runs(self):
        body = ('import json\nprint(json.dumps({"events": ['
                '{"kind": "k", "key": "a", "title": "A"},'
                '{"kind": "k", "key": "b", "title": "A"}]}))')
        self.run_routine(body)
        self.run_routine(body)
        ids = [e["id"] for e in self.lines()]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)

    def test_event_id_default_unchanged_without_salt(self):
        from workspace_daemon import events
        a = events.build("r", "k", None, "s:1", "t", [], ts="2026-01-01T00:00:00Z")
        b = events.build("r", "k", None, "s:1", "t", [], ts="2026-01-01T00:00:00Z",
                         salt="x")
        self.assertEqual(
            a["id"], events.event_id("s:1", None, "k", "2026-01-01T00:00:00Z"))
        self.assertNotEqual(a["id"], b["id"])

    def test_non_json_stdout(self):
        with self.assertRaisesRegex(RuntimeError, "not JSON"):
            self.run_routine("print('hello')")
        self.assertEqual(self.lines(), [])

    def test_wrong_shape(self):
        with self.assertRaisesRegex(RuntimeError, "events"):
            self.run_routine("print('[]')")

    def test_nonzero_exit(self):
        body = ('import sys, json\nprint(json.dumps({"events": ['
                '{"kind": "k", "key": "a", "title": "A"}]}))\n'
                'sys.stderr.write("boom"); sys.exit(3)')
        with self.assertRaisesRegex(RuntimeError, "exited 3: boom"):
            self.run_routine(body)
        self.assertEqual(self.lines(), [])

    def test_missing_command(self):
        r = routine(command=["/nonexistent/my-script.py"])
        with self.assertRaisesRegex(RuntimeError, "cannot start"):
            maintenance.run(self.base, r)

    def test_timeout_kills_process_group(self):
        marker = self.script_dir / "child-alive"
        body = textwrap.dedent(f'''\
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c",
                "import time, pathlib; time.sleep(2); "
                "pathlib.Path({str(marker)!r}).write_text('x')"])
            time.sleep(60)
        ''')
        started = time.time()
        with self.assertRaisesRegex(RuntimeError, "timed out after 1s"):
            self.run_routine(body, timeout_s=1)
        self.assertLess(time.time() - started, 10)
        time.sleep(2.5)
        self.assertFalse(marker.exists(), "child outlived the group kill")
        self.assertEqual(self.lines(), [])

    def test_max_events_cap_emits_none(self):
        body = ('import json\nprint(json.dumps({"events": ['
                '{"kind": "k", "key": str(i), "title": "t"} '
                'for i in range(3)]}))')
        with self.assertRaisesRegex(RuntimeError, "over max_events=2"):
            self.run_routine(body, max_events=2)
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.run_routine(body, max_events=3)["emitted"], 3)

    def test_dry_run_emits_nothing_but_logs(self):
        with mock.patch.object(external_events, "log") as log:
            report = self.run_routine(SCRIPT, dry_run=True)
        self.assertEqual(report["events"], 2)
        self.assertEqual(report["emitted"], 0)
        self.assertEqual(self.lines(), [])
        self.assertEqual(log.call_count, 2)
        self.assertFalse((self.base / "state" / "external-events").exists())
        self.assertIn("sheetwatch:sheet1:row7", log.call_args_list[0].args[0])

    def test_env_tilde_and_stdin(self):
        body = ('import json, os, sys\n'
                'assert sys.stdin.read() == ""\n'
                'print(json.dumps({"events": [{"kind": "k", "key": "a", '
                '"title": os.environ["MY_VAR"] + ":" + sys.argv[1]}]}))')
        r = routine(command=self.script(body) + ["~/x"],
                    env={"MY_VAR": "v"})
        maintenance.run(self.base, r)
        self.assertEqual(
            self.lines()[0]["title"], "v:" + os.path.expanduser("~/x"))


class CursorAndFailureTest(RunFixture):

    def cursor_file(self):
        return self.base / "state" / "external-events" / "ext-events.cursor"

    BODY = textwrap.dedent('''\
        import json, os
        path = os.environ["MEMORY_DAEMON_CURSOR_FILE"]
        try:
            prev = json.load(open(path))
        except FileNotFoundError:
            prev = None
        print(json.dumps({"events": [{"kind": "k", "key": "a",
            "title": "prev=%s dry=%s" % (prev, os.environ["MEMORY_DAEMON_DRY_RUN"])}],
            "cursor": {"n": (prev or {}).get("n", 0) + 1}}))
    ''')

    def test_cursor_saved_after_emit_and_passed_next_run(self):
        self.run_routine(self.BODY)
        self.assertEqual(json.loads(self.cursor_file().read_text()), {"n": 1})
        self.assertEqual(self.cursor_file().stat().st_mode & 0o777, 0o600)
        self.run_routine(self.BODY)
        self.assertEqual(json.loads(self.cursor_file().read_text()), {"n": 2})
        titles = [e["title"] for e in self.lines()]
        self.assertEqual(titles, ["prev=None dry=0", "prev={'n': 1} dry=0"])

    def test_dry_run_never_saves_cursor_and_sets_flag(self):
        with mock.patch.object(external_events, "log") as log:
            self.run_routine(self.BODY, dry_run=True)
        self.assertFalse(self.cursor_file().exists())
        self.assertIn("dry=1", log.call_args_list[0].args[0])

    def test_no_cursor_key_leaves_file_unchanged(self):
        self.run_routine(self.BODY)
        body = ('import json\nprint(json.dumps({"events": ['
                '{"kind": "k", "key": "z", "title": "Z"}]}))')
        self.run_routine(body)
        self.assertEqual(json.loads(self.cursor_file().read_text()), {"n": 1})

    def test_cursor_not_saved_when_batch_rejected_or_command_fails(self):
        self.run_routine(self.BODY)
        bad = ('import json\nprint(json.dumps({"events": [{"kind": "K"}], '
               '"cursor": 99}))')
        with self.assertRaises(RuntimeError):
            self.run_routine(bad)
        crash = ('import json, sys\nprint(json.dumps({"events": [], '
                 '"cursor": 99}))\nsys.exit(1)')
        with self.assertRaises(RuntimeError):
            self.run_routine(crash)
        self.assertEqual(json.loads(self.cursor_file().read_text()), {"n": 1})

    def test_cursor_size_and_shape_limits(self):
        big = ('import json\nprint(json.dumps({"events": [], '
               '"cursor": "x" * 300000}))')
        with self.assertRaisesRegex(RuntimeError, "cursor exceeds"):
            self.run_routine(big)
        with self.assertRaisesRegex(RuntimeError, "unknown key"):
            self.run_routine('print(\'{"events": [], "other": 1}\')')
        self.assertFalse(self.cursor_file().exists())

    def test_emit_failure_raises_and_keeps_cursor(self):
        self.run_routine(self.BODY)
        calls = []

        def flaky(base_dir, event):
            calls.append(event)
            return len(calls) == 1

        body = ('import json\nprint(json.dumps({"events": ['
                '{"kind": "k", "key": "a", "title": "A"},'
                '{"kind": "k", "key": "b", "title": "B"}], "cursor": {"n": 9}}))')
        with mock.patch.object(external_events.event_stream, "emit", flaky):
            with self.assertRaisesRegex(RuntimeError, "emitted 1 of 2"):
                self.run_routine(body)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(self.cursor_file().read_text()), {"n": 1})

    def test_stderr_tail_in_failures(self):
        body = ('import sys\nsys.stderr.write("noise\\n" * 5000 + '
                '"final   line\\nreason: bad")\nsys.exit(2)')
        with self.assertRaises(RuntimeError) as ctx:
            self.run_routine(body)
        msg = str(ctx.exception)
        self.assertIn("exited 2", msg)
        self.assertTrue(msg.endswith("final line reason: bad"), msg[-80:])
        self.assertLess(len(msg), 700)

    def test_stderr_tail_is_redacted(self):
        body = ('import sys\nsys.stderr.write("failed\\npass" "word: swordfish\\n")'
                '\nsys.exit(2)')
        with self.assertRaises(RuntimeError) as ctx:
            self.run_routine(body)
        msg = str(ctx.exception)
        self.assertIn("exited 2", msg)
        self.assertNotIn("swordfish", msg)
        self.assertIn("[REDACTED]", msg)

    def test_non_json_stdout_snippet_is_redacted(self):
        body = 'import sys\nsys.stdout.write("oops pass" "word: swordfish")'
        with self.assertRaises(RuntimeError) as ctx:
            self.run_routine(body)
        msg = str(ctx.exception)
        self.assertIn("not JSON", msg)
        self.assertNotIn("swordfish", msg)
        self.assertIn("[REDACTED]", msg)

    def test_stderr_tail_on_timeout(self):
        body = ('import sys, time\nsys.stderr.write("stuck on step 3")\n'
                'sys.stderr.flush()\ntime.sleep(60)')
        with self.assertRaisesRegex(RuntimeError, "timed out.*stuck on step 3"):
            self.run_routine(body, timeout_s=1)

    def test_stdout_must_be_utf8(self):
        body = 'import sys\nsys.stdout.buffer.write(b\'{"events": [\\xff]}\')'
        with self.assertRaisesRegex(RuntimeError, "not valid UTF-8"):
            self.run_routine(body)

    def test_stdout_cap(self):
        body = ('import sys\nsys.stdout.write("x" * (9 * 1024 * 1024))')
        with self.assertRaisesRegex(RuntimeError, "stdout exceeds"):
            self.run_routine(body)

    def test_undecodable_stderr_is_replaced(self):
        body = ('import sys\nsys.stderr.buffer.write(b"bad \\xff byte")\n'
                'sys.exit(1)')
        with self.assertRaisesRegex(RuntimeError, "bad .* byte"):
            self.run_routine(body)

    def test_no_hang_when_grandchild_escapes_group(self):
        body = textwrap.dedent('''\
            import os, subprocess, sys, time
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"],
                             start_new_session=True)
            time.sleep(60)
        ''')
        started = time.time()
        with self.assertRaisesRegex(RuntimeError, "timed out after 1s"):
            self.run_routine(body, timeout_s=1)
        self.assertLess(time.time() - started, 5 + 1 + 3)

    def test_child_holding_stdout_after_exit_gets_the_timeout(self):
        body = textwrap.dedent('''\
            import subprocess, sys
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            print('{"events": []}')
        ''')
        started = time.time()
        with self.assertRaisesRegex(RuntimeError, "timed out after 1s"):
            self.run_routine(body, timeout_s=1)
        self.assertLess(time.time() - started, 8)


class SharedConstantsTest(unittest.TestCase):
    def test_reserved_sources_cover_capture_prefixes(self):
        from workspace_daemon import memory_sink
        for prefix in memory_sink.CAPTURE_SOURCE_ID_PREFIXES:
            self.assertIn(prefix, memory_sink.RESERVED_EVENT_SOURCES)
            self.assertTrue(
                memory_sink.OPERATOR_CONFIRMED_SOURCE_ID_RE.match(f"{prefix}:x"))
        self.assertIsNone(
            memory_sink.OPERATOR_CONFIRMED_SOURCE_ID_RE.match("gcal:x"))
        self.assertEqual(
            external_events.RESERVED_SOURCES,
            memory_sink.RESERVED_EVENT_SOURCES)

    def test_two_routines_may_not_share_a_source(self):
        a, b = routine(), routine()
        a["id"], b["id"] = "one", "two"
        found = config.cross_problems([a, b])
        self.assertEqual(set(found), {"one", "two"})
        b["maintenance"]["source"] = "other"
        self.assertEqual(config.cross_problems([a, b]), {})
        b["maintenance"]["source"] = "sheetwatch"
        b["enabled"] = False
        self.assertEqual(config.cross_problems([a, b]), {})


class ScheduleTest(unittest.TestCase):
    def test_scheduled_in_maintenance_tick_group(self):
        r = routine()
        self.assertTrue(daemon_cli._in_tick_group(r, "maintenance"))
        self.assertFalse(daemon_cli._in_tick_group(r, "calendar"))
        self.assertFalse(daemon_cli._in_tick_group(r, "capture"))
        groups = daemon_cli._execution_groups([r], {"ext-events"})
        self.assertEqual(
            [(ids, name) for ids, name in groups if ids],
            [({"ext-events"}, "external-events")],
        )

    def test_runner_dispatches_and_counts_failure(self):
        r = routine()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(maintenance, "run") as run:
                totals = runner.run(Path(tmp), [r], active_ids={"ext-events"})
            run.assert_called_once()
            self.assertEqual(totals["errors"], 0)
            with mock.patch.object(
                maintenance, "run", side_effect=RuntimeError("boom")
            ):
                totals = runner.run(Path(tmp), [r], active_ids={"ext-events"})
            self.assertEqual(totals["errors"], 1)


if __name__ == "__main__":
    unittest.main()
