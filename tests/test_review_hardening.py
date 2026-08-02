"""Regression tests for the independent whole-project safety review."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from workspace_daemon import config, memory_sink, runner, shell, state


def result(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class GmailCatchUpTest(unittest.TestCase):
    def test_cursor_window_preserves_timeless_inbox_queue(self):
        source = {
            "kind": "gmail",
            "query": "in:sent OR in:inbox",
            "queue_query": "in:inbox (is:unread OR is:starred)",
            "_since": "2026-08-01T10:00:00Z",
            "max_results": 0,
        }
        with mock.patch.object(runner.gmail, "search", return_value=[]) as search:
            self.assertEqual(runner._gmail_candidates(source), [])
        query = search.call_args.args[0]
        self.assertIn("in:inbox (is:unread OR is:starred)", query)
        self.assertIn("after:1785578400", query)

    def test_gmail_catch_up_rejects_rolling_query_and_caps(self):
        routine = {
            "id": "gmail",
            "source": {
                "kind": "gmail", "query": "newer_than:1d", "catch_up": True,
                "catch_up_after": "2026-08-01T00:00:00Z", "max_results": 20,
            },
            "analyze": {"provider": "gemini", "model": "m", "instruction": "x"},
        }
        problems = config.validate(routine)
        self.assertTrue(any("fixed temporal operator" in p for p in problems))
        self.assertTrue(any("max_results: 0" in p for p in problems))


class ConfigSchemaTest(unittest.TestCase):
    def test_unknown_keys_fail_closed_at_each_level(self):
        routine = {
            "id": "typos",
            "enabeld": True,
            "source": {"kind": "gmail", "query": "in:inbox", "max_reuslts": 0},
            "analyze": {
                "provider": "gemini", "model": "m", "instruction": "x",
                "max_output_toknes": 4096,
            },
            "memory": {"store": "/tmp/store", "tyep": "note"},
        }
        problems = config.validate(routine)
        rendered = "\n".join(problems)
        for typo in ("enabeld", "max_reuslts", "max_output_toknes", "tyep"):
            self.assertIn(typo, rendered)


class CursorIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    @staticmethod
    def routine(rid, source):
        return {
            "id": rid,
            "source": source,
            "analyze": {"provider": "gemini", "model": "m", "instruction": "x"},
        }

    def test_one_source_failure_does_not_hold_an_unrelated_cursor(self):
        gchat = {
            "kind": "gchat", "catch_up": True,
            "catch_up_after": "2026-08-01T00:00:00Z",
        }
        gmail = {
            "kind": "gmail", "query": "in:sent", "catch_up": True,
            "catch_up_after": "2026-08-01T00:00:00Z", "max_results": 0,
        }

        def failed(_source):
            raise RuntimeError("Google Chat unavailable")

        with mock.patch.dict(
            runner.SOURCES,
            {
                "gchat": (failed, mock.Mock()),
                "gmail": (lambda _source: [], mock.Mock()),
            },
        ), mock.patch.object(config, "validate", return_value=[]), mock.patch.object(
            runner, "utc_now_iso", return_value="2026-08-02T10:00:00Z"
        ):
            totals = runner.run(
                self.base,
                [self.routine("chat", gchat), self.routine("mail", gmail)],
            )

        cursors = state.CursorStore(self.base)
        self.assertEqual(totals["errors"], 1)
        self.assertIsNone(cursors.checkpoint(
            "chat", runner._catch_up_cursor_id(gchat), "gchat"
        ))
        self.assertEqual(cursors.checkpoint(
            "mail", runner._catch_up_cursor_id(gmail), "gmail"
        ), "2026-08-02T10:00:00Z")

    def test_shared_outage_opens_circuit_after_first_candidate(self):
        source = {
            "kind": "gmail", "query": "in:sent", "catch_up": True,
            "catch_up_after": "2026-08-01T00:00:00Z", "max_results": 0,
        }
        candidates = [
            {"id": "m1", "title": "one", "raw": {"thread_id": "t1"}},
            {"id": "m2", "title": "two", "raw": {"thread_id": "t2"}},
        ]

        def fetch(_routine, _source, candidate):
            return {
                "id": candidate["id"], "source_id": f"gmail:{candidate['id']}",
                "title": candidate["title"], "date": "2026-08-02", "body": "body",
                "frontmatter": {},
            }

        routine = self.routine("mail", source)
        with mock.patch.dict(runner.SOURCES, {"gmail": (lambda _s: candidates, fetch)}), \
             mock.patch.object(config, "validate", return_value=[]), \
             mock.patch.object(
                 runner.llm, "analyze", side_effect=RuntimeError("could not resolve host")
             ) as analyze:
            totals = runner.run(self.base, [routine])

        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(totals["errors"], 1)
        self.assertEqual(totals["skipped"], 1)


class MemorySinkProofTest(unittest.TestCase):
    def test_structured_extraction_rejects_missing_and_unknown_fields(self):
        memory_sink._slug_cache["/store"] = set()
        routine = {"analyze": {"provider": "gemini", "model": "m"}}
        bad = (
            '{"worthy":true,"owner_attention":false,"type":"note",'
            '"title":"x","people":[],"tags":["x"],"body":"b",'
            '"surprise":"ignored?"}'
        )
        with mock.patch.object(runner.llm, "analyze", return_value=bad):
            with self.assertRaisesRegex(ValueError, "unknown keys: surprise"):
                memory_sink._extract(
                    routine, {"title": "x", "date": "2026-08-02"},
                    "body", "/store",
                )

    def test_unrecognized_success_is_a_failure_for_every_capture(self):
        routine = {
            "id": "mail", "memory": {"store": "/store", "extract": False},
        }
        item = {
            "id": "m1", "source_id": "gmail:t1", "title": "title",
            "date": "2026-08-02", "frontmatter": {},
        }
        with mock.patch.object(
            memory_sink, "_cli", return_value=result("completed\n")
        ), mock.patch.object(memory_sink, "_commit_store"):
            with self.assertRaisesRegex(RuntimeError, "no entry id"):
                memory_sink.capture(routine, item, "durable summary")

    def test_git_add_and_dirty_commit_failures_surface(self):
        with mock.patch.object(
            memory_sink.subprocess, "run", return_value=result(stderr="no repo", returncode=1)
        ):
            with self.assertRaisesRegex(RuntimeError, "git add failed"):
                memory_sink._commit_store("/store", "message")

        with mock.patch.object(
            memory_sink.subprocess,
            "run",
            side_effect=[result(), result(stderr="hook failed", returncode=1), result(" M x\n")],
        ):
            with self.assertRaisesRegex(RuntimeError, "git commit failed"):
                memory_sink._commit_store("/store", "message")


class PrivateStateTest(unittest.TestCase):
    def test_state_and_log_files_are_owner_only_and_logs_rotate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = state.Store(root)
            store.record("x", {"rule_id": "r"})
            self.assertEqual(os.stat(root / "state").st_mode & 0o777, 0o700)
            self.assertEqual(
                os.stat(root / "state" / "processed.json").st_mode & 0o777,
                0o600,
            )

            # Existing installations are migrated on the next real open.
            (root / "state" / "processed.json").chmod(0o644)
            state.Store(root)
            self.assertEqual(
                os.stat(root / "state" / "processed.json").st_mode & 0o777,
                0o600,
            )

            path = root / "logs" / "run.log"
            path.parent.mkdir()
            path.write_text("old content that is large enough")
            old_backup = path.with_name("run.log.2")
            old_backup.write_text("older")
            old_backup.chmod(0o644)
            with mock.patch.object(shell, "LOG_ROTATE_BYTES", 10):
                shell.set_log_file(path)
                shell.log("new")
            shell._log_file = None
            self.assertTrue(path.with_name("run.log.1").exists())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            shifted_backup = path.with_name("run.log.3")
            self.assertEqual(os.stat(shifted_backup).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
