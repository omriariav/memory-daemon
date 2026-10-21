"""Local-model chat triage: protection, shadow/enforce, fail-open, ledger, report."""
import datetime as dt
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import daemon
from workspace_daemon import config, llm, local_llm, local_triage, runner, state

from test_capture_quality import chat


def routine(**tri):
    cfg = {"enabled": True}
    cfg.update(tri)
    return {"id": "r", "analyze": {"provider": "claude", "model": "haiku",
                                   "instruction": "x", "local_triage": cfg},
            "memory": {"store": "/store"}}


def fake(label="small_talk", confidence=0.95, reason="greeting only"):
    return mock.patch.object(
        local_llm, "chat_json",
        return_value={"label": label, "confidence": confidence,
                      "reason": reason})


class Ledger:
    def __init__(self):
        self.rows = {}

    def record_resolving(self, key, record, source_id):
        self.rows[key] = record

    def record(self, key, record):
        self.rows[key] = record

    def get(self, key):
        return self.rows.get(key)


def process(r, item, paid="summary"):
    ledger, totals = Ledger(), {"errors": 0, "fallbacks": 0,
                                "pending_actions": 0}
    with mock.patch.object(llm, "analyze", return_value=paid) as analyze, \
            mock.patch.object(runner.memory_sink, "capture",
                              return_value={"memory": "created"}), \
            mock.patch.object(runner, "log"):
        record = runner._process(
            r, {"kind": "slack_mcp"}, {"id": item["id"], "title": "t"},
            lambda *a: item, ledger, [], False, totals)
    return record, ledger, totals, analyze


def talk(text="we should move the launch to Friday"):
    return chat(("A", text))


class ProtectionTest(unittest.TestCase):
    def check_protected(self, item):
        with fake() as model, mock.patch.object(runner, "log"):
            verdict = runner._local_triage_verdict(
                routine(mode="enforce"), item)
        model.assert_not_called()
        self.assertTrue(verdict["protected"])
        self.assertFalse(verdict["skip"])

    def test_saved(self):
        item = talk()
        item["frontmatter"]["slack_saved"] = True
        self.check_protected(item)

    def test_owner_evidence(self):
        for ev in ("mentioned", "channel-rule", "saved"):
            item = talk()
            item["frontmatter"]["slack_owner_evidence"] = [ev]
            self.check_protected(item)

    def test_guards(self):
        for text in ("see https://example.com/x", "can you send it",
                     "please look"):
            self.check_protected(talk(text))
        item = talk()
        item["body"] += "\nFiles: a.pdf"
        self.check_protected(item)

    def test_too_long(self):
        item = talk("x" * 5000)
        with fake() as model, mock.patch.object(runner, "log"):
            verdict = runner._local_triage_verdict(
                routine(mode="enforce", max_chars=100), item)
        model.assert_not_called()
        self.assertEqual(verdict["protected"], "longer than max_chars")

    def test_not_applicable(self):
        item = talk()
        self.assertIsNone(runner._local_triage_verdict(
            {**routine(), "analyze": {"provider": "claude"}}, item))
        self.assertIsNone(runner._local_triage_verdict(
            routine(enabled=False), item))
        mail = dict(item, source_kind="gmail")
        self.assertIsNone(runner._local_triage_verdict(routine(), mail))
        with_output = routine()
        with_output["output"] = {"vault_dir": "/v"}
        self.assertIsNone(runner._local_triage_verdict(with_output, item))


class ModeTest(unittest.TestCase):
    def test_shadow_never_skips_and_records(self):
        item = talk("nice weather")
        with fake("small_talk", 0.99):
            record, ledger, totals, analyze = process(routine(), item)
        analyze.assert_called_once()
        self.assertEqual(record["memory"], "created")
        self.assertEqual(record["local_triage"], {
            "label": "small_talk", "confidence": 0.99,
            "reason": "greeting only", "mode": "shadow"})
        self.assertNotIn("trivial", totals)

    def test_enforce_skips_small_talk_over_threshold(self):
        item = talk("nice weather")
        with fake("small_talk", 0.9):
            record, ledger, totals, analyze = process(
                routine(mode="enforce"), item)
        analyze.assert_not_called()
        self.assertEqual(record["memory"], "skipped_trivial")
        self.assertEqual(record["trivial_reason"], "local-triage:small_talk")
        self.assertEqual(ledger.rows[item["id"]]["local_triage"]["label"],
                         "small_talk")
        self.assertEqual(totals["trivial"], 1)

    def test_enforce_keeps_everything_else(self):
        for label, conf in (("small_talk", 0.5), ("logistics", 0.99),
                            ("substantive", 0.99)):
            with self.subTest(label=label, conf=conf), fake(label, conf):
                record, _l, _t, analyze = process(
                    routine(mode="enforce"), talk("nice weather"))
                analyze.assert_called_once()
                self.assertEqual(record["local_triage"]["label"], label)

    def test_logistics_skippable_only_when_configured(self):
        with fake("logistics", 0.99):
            record, _l, _t, analyze = process(
                routine(mode="enforce", skip_labels=["logistics"]),
                talk("nice weather"))
        analyze.assert_not_called()
        self.assertEqual(record["trivial_reason"], "local-triage:logistics")

    def test_protected_item_is_ledgered_and_paid(self):
        item = talk()
        item["frontmatter"]["slack_saved"] = True
        with fake() as model:
            record, _l, _t, analyze = process(routine(mode="enforce"), item)
        model.assert_not_called()
        analyze.assert_called_once()
        self.assertEqual(record["local_triage"]["protected"],
                         "saved by reaction")


class FailOpenTest(unittest.TestCase):
    def check(self, **patch):
        with mock.patch.object(local_llm, "chat_json", **patch):
            record, _l, _t, analyze = process(
                routine(mode="enforce"), talk("nice weather"))
        analyze.assert_called_once()
        self.assertTrue(record["local_triage"]["failed"])
        self.assertEqual(record["local_triage"]["label"], "substantive")

    def test_errors(self):
        self.check(side_effect=local_llm.LocalLLMUnavailable("down"))
        self.check(side_effect=local_llm.LocalLLMError("bad"))
        self.check(side_effect=ValueError("boom"))

    def test_invalid_shapes(self):
        bad = [{"label": "nope", "confidence": 0.99, "reason": "r"},
               {"label": "small_talk", "confidence": 1.5, "reason": "r"},
               {"label": "small_talk", "confidence": True, "reason": "r"},
               {"label": "small_talk", "confidence": "0.9", "reason": "r"},
               {"label": "small_talk", "confidence": 0.9}]
        for value in bad:
            with self.subTest(value=value):
                self.check(return_value=value)

    def test_reason_truncated_and_prompt_marks_data_untrusted(self):
        cfg = local_triage.settings(routine()["analyze"])
        with fake(reason=" ".join(["w"] * 30)) as model:
            verdict = local_triage.triage(talk("nice weather"), cfg)
        self.assertEqual(len(verdict["reason"].split()), 12)
        self.assertIn("untrusted", model.call_args[0][2][0]["content"])


class ConfigTest(unittest.TestCase):
    def problems(self, **tri):
        return "\n".join(
            local_triage.validate("r", routine(**tri)["analyze"]))

    def test_defaults(self):
        cfg = local_triage.settings(routine()["analyze"])
        self.assertEqual(cfg["mode"], "shadow")
        self.assertEqual(cfg["model"], "qwen2.5:7b-instruct")
        self.assertEqual(cfg["min_confidence"], 0.85)
        self.assertEqual(cfg["skip_labels"], ["small_talk"])

    def test_validation(self):
        self.assertEqual(self.problems(), "")
        text = self.problems(mode="x", min_confidence=2, max_chars=0,
                             skip_labels=["substantive"], bogus=1,
                             provider="cloud")
        for needle in ("mode", "min_confidence", "max_chars", "skip_labels",
                       "unknown keys", "provider"):
            self.assertIn(needle, text)
        self.assertIn("local_triage", config.ANALYZE_KEYS)

    def test_routine_validation_requires_chat_source(self):
        base = routine()
        base["source"] = {"kind": "gmail"}
        self.assertIn("chat sources only", "\n".join(config.validate(base)))


class ReportTest(unittest.TestCase):
    def rows(self):
        def row(label, memory, **extra):
            return {"memory": memory, "processed_at": "2026-01-05T10:00:00Z",
                    "local_triage": {"label": label, "confidence": 0.9,
                                     "reason": "r", "mode": "shadow"},
                    **extra}
        return [
            row("small_talk", "skipped_not_worthy"),
            row("small_talk", "created"),
            row("substantive", "created"),
            {"memory": "created", "processed_at": "2026-01-05T10:00:00Z",
             "local_triage": {"protected": "saved by reaction"}},
            {"memory": "created", "processed_at": "2026-01-05T10:00:00Z",
             "local_triage": {"label": "substantive", "failed": True}},
            {"memory": "created"},
            row("small_talk", "skipped_trivial",
                trivial_reason="local-triage:small_talk"),
        ]

    def test_matrix(self):
        matrix = local_triage.report(self.rows())
        self.assertEqual(matrix["small_talk"], {
            "skipped_not_worthy": 1, "created": 1,
            "skipped-by-local-triage": 1})
        self.assertEqual(matrix["substantive"], {"created": 1})
        self.assertEqual(matrix["protected"], {"created": 1})
        self.assertEqual(matrix["error"], {"created": 1})
        self.assertIn("small_talk agreement: 2/3",
                      local_triage.render_report(matrix))

    def test_days_filter(self):
        now = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(local_triage.report(self.rows(), days=7, now=now), {})
        self.assertTrue(local_triage.report(self.rows(), days=90, now=now))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(Path(tmp))
            for i, row in enumerate(self.rows()):
                store.record(f"k{i}", row)
            out = io.StringIO()
            with mock.patch.object(daemon, "BASE_DIR", Path(tmp)), \
                    redirect_stdout(out):
                rc = daemon.main(["triage-report"])
        self.assertEqual(rc, 0)
        self.assertIn("small_talk", out.getvalue())


if __name__ == "__main__":
    unittest.main()
