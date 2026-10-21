import unittest
from unittest import mock

from workspace_daemon import config, identity, llm, memory_sink, runner, triviality


def chat(*messages, kind="slack_mcp"):
    lines = [f"[2026-01-05T10:0{i}:00.000Z] {who}: {text}"
             for i, (who, text) in enumerate(messages)]
    return {"id": "slack:C1:1.0", "source_id": "slack:C1:1.0", "title": "t",
            "date": "2026-01-05", "body": "\n".join(lines),
            "source_kind": kind, "frontmatter": {}}


class TrivialityTest(unittest.TestCase):
    def assertTrivial(self, item, extra=()):
        self.assertTrue(triviality.assess(item, extra_words=extra)[0], item["body"])

    def assertSubstantive(self, item, extra=()):
        self.assertFalse(triviality.assess(item, extra_words=extra)[0], item["body"])

    def test_small_talk_is_trivial(self):
        for text in (
            "hi hi",
            "Sure, no problem :slightly_smiling_face: Reactions: thankyouwave (1)",
            "Hey! Sounds good",
            "<@U123|jane> thanks :wave:",
            "תודה!",
            "תודה רבה",
            "סבבה \U0001F44D",
            "אין בעיה, בכיף",
            "Reactions: ack_button (1)",
        ):
            self.assertTrivial(chat(("Jane Doe", text)))

    def test_lone_reaction_is_trivial(self):
        self.assertTrivial(chat(("Jane Doe", "Reactions: ack_button (1)")))
        self.assertTrivial(chat(("Jane Doe", ":ack_button:")))

    def test_anything_outside_allowlist_is_kept(self):
        for text in (
            "סגרנו, הולכים על אופציה ב",
            "תעביר לי את המצגת עד מחר",
            "can you send me the deck?",
            "we go with option B for EU dunning",
            "you're on the 3rd floor, right?",
            "Hey! Sounds good I accepted the invite",
            "thanks, please send it",
            "see https://example.com/doc",
            "ok\nFiles: plan.pdf",
            "call moved to 14:30",
            "Can you confirm the renewal pricing?",
        ):
            self.assertSubstantive(chat(("Jane Doe", text)))

    def test_owner_mention_is_kept(self):
        item = chat(("Jane Doe", "<@U999|me> thanks"))
        item["frontmatter"]["slack_owner_evidence"] = ["mentioned"]
        self.assertSubstantive(item)

    def test_extra_words(self):
        item = chat(("Jane Doe", "ack roger"))
        self.assertSubstantive(item)
        self.assertTrivial(item, extra=["ack", "Roger"])

    def test_long_text_is_kept(self):
        self.assertSubstantive(chat(("Jane Doe", "thanks " * 40)))

    def test_non_chat_source_never_trivial(self):
        self.assertFalse(triviality.assess(chat(("A", "hi hi"), kind="gmail"))[0])


class TrivialVerdictTest(unittest.TestCase):
    def routine(self, **analyze):
        return {"id": "r", "analyze": {"provider": "claude", "model": "haiku",
                                       "instruction": "x", **analyze},
                "memory": {"store": "/store"}}

    def test_default_off(self):
        self.assertIsNone(runner._trivial_verdict(
            self.routine(), chat(("A", "hi hi"))))

    def test_on_skips_and_respects_output_sink(self):
        r = self.routine(skip_trivial=True)
        self.assertTrue(runner._trivial_verdict(r, chat(("A", "hi hi"))))
        r["analyze"]["trivial_extra_words"] = ["zzz"]
        self.assertTrue(runner._trivial_verdict(r, chat(("A", "hi zzz"))))
        r["output"] = {"vault_dir": "/v"}
        self.assertIsNone(runner._trivial_verdict(r, chat(("A", "hi hi"))))

    def test_config_validation(self):
        base = {"id": "r", "source": {"kind": "slack_mcp"},
                "analyze": {"provider": "claude", "model": "haiku",
                            "instruction": "x", "skip_trivial": "yes",
                            "trivial_max_chars": 0}}
        problems = "\n".join(config.validate(base))
        self.assertIn("skip_trivial must be true or false", problems)
        self.assertIn("trivial_max_chars must be a positive integer", problems)

    def test_process_skips_without_model_call(self):
        class Ledger:
            def __init__(self):
                self.rows = {}

            def record_resolving(self, key, record, source_id):
                self.rows[key] = record

            def get(self, key):
                return self.rows.get(key)

        item = chat(("A", "hi hi"))
        ledger, totals = Ledger(), {"errors": 0}
        with mock.patch.object(llm, "analyze",
                               side_effect=AssertionError("model called")), \
                mock.patch.object(runner, "log"):
            record = runner._process(
                self.routine(skip_trivial=True), {"kind": "slack_mcp"},
                {"id": item["id"], "title": "t"},
                lambda *a: item, ledger, [], False, totals)
        self.assertEqual(record["memory"], "skipped_trivial")
        self.assertEqual(ledger.rows[item["id"]]["memory"], "skipped_trivial")
        self.assertEqual(totals["trivial"], 1)


GOOD = ('{"worthy": true, "owner_attention": false, "type": "note", '
        '"title": "T", "people": [], "tags": ["x"], "follows": [], "body": "b"}')
BAD = GOOD.replace('"body": "b"', '"body": ""')


class ExtractRetryTest(unittest.TestCase):
    def setUp(self):
        memory_sink._slug_cache["/store"] = set()
        self.routine = {"id": "r", "analyze": {"provider": "claude", "model": "haiku"},
                        "memory": {"store": "/store"}}
        self.item = {"title": "t", "date": "2026-01-05", "frontmatter": {}}

    def run_extract(self, answers):
        calls = []

        def fake(routine, prompt, model=None):
            calls.append((prompt, model))
            return answers[len(calls) - 1]

        with mock.patch.object(llm, "analyze", side_effect=fake):
            try:
                return memory_sink._extract(
                    self.routine, self.item, "summary", "/store"), calls
            except ValueError as exc:
                return exc, calls

    def test_retry_appends_error_then_succeeds(self):
        data, calls = self.run_extract([BAD, GOOD])
        self.assertEqual(data["title"], "T")
        self.assertEqual(len(calls), 2)
        self.assertIn("Your previous JSON failed: body must be a non-empty string.",
                      calls[1][0])
        self.assertIn("Return corrected JSON only.", calls[1][0])
        self.assertNotIn("Your previous JSON failed", calls[0][0])

    def test_malformed_json_is_retried_once_only(self):
        result, calls = self.run_extract(["not json", "still not json", GOOD])
        self.assertIsInstance(result, ValueError)
        self.assertEqual(len(calls), 2)

    def test_extract_model_override(self):
        _, calls = self.run_extract([GOOD])
        self.assertIsNone(calls[0][1])
        self.routine["memory"]["extract_model"] = "sonnet"
        _, calls = self.run_extract([BAD, GOOD])
        self.assertEqual([c[1] for c in calls], ["sonnet", "sonnet"])

    def test_extract_model_passthrough_to_provider(self):
        with mock.patch.object(llm.claude_bridge, "complete",
                               return_value="ok") as complete:
            llm._base_dir = "/b"
            try:
                llm.analyze(self.routine, "p", model="sonnet")
                llm.analyze(self.routine, "p")
            finally:
                llm._base_dir = None
        self.assertEqual([c.kwargs["model"] for c in complete.call_args_list],
                         ["sonnet", "haiku"])

    def test_validate_extract_model(self):
        self.routine["memory"]["extract_model"] = ""
        self.assertTrue(any("extract_model" in p
                            for p in memory_sink.validate(self.routine)))


class DegradedFallbackTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(identity._providers.clear)
        memory_sink._slug_cache["/store"] = set()
        self.routine = {"id": "r", "analyze": {"provider": "claude", "model": "haiku"},
                        "memory": {"store": "/store", "type": "note"}}

    def capture(self, item, summary, exc=None):
        added = {}

        def fake_cli(store, args, stdin_text=None, timeout=120):
            added["args"] = args
            return mock.Mock(returncode=0, stdout="✓ created e1\n", stderr="")

        identity = {"email": None, "person": None, "safe": False}
        with mock.patch.object(memory_sink, "_extract",
                               side_effect=exc or memory_sink.ExtractionInvalid(
                                   "body must be a non-empty string")), \
                mock.patch.object(memory_sink, "_authenticated_identity",
                                  return_value=identity), \
                mock.patch.object(memory_sink, "_cli", side_effect=fake_cli), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log"):
            return memory_sink.capture(self.routine, item, summary), added

    def long_item(self):
        return {"id": "slack:C1:1", "title": "raw message text here",
                "date": "2026-01-05", "source_kind": "slack_mcp",
                "frontmatter": {},
                "body": "[t] A: " + "We agreed the EU dunning rollout plan details. " * 6}

    def test_short_summary_chat_item_with_long_text_is_stored(self):
        out, added = self.capture(self.long_item(), "Short summary.")
        self.assertEqual(out["memory"], "created")

    def test_borderline_chat_item_invalid_twice_is_not_stored(self):
        item = self.long_item()
        item["body"] = "[t] A: hi there"
        out, added = self.capture(item, "x" * 400)
        self.assertEqual(out["memory"], "skipped_extract_failed")
        self.assertEqual(out["memory_warn"], 1)
        self.assertEqual(added, {})

    def test_transport_failure_on_borderline_chat_is_not_dropped(self):
        item = self.long_item()
        item["body"] = "[t] A: hi there"
        out, added = self.capture(item, "short", exc=RuntimeError("timeout"))
        self.assertEqual(out["memory"], "created")
        self.assertIn("extract-degraded",
                      added["args"][added["args"].index("--tags") + 1])

    def test_gmail_and_mila_short_summary_still_stored(self):
        for kind in ("gmail", "mila"):
            for exc in (None, RuntimeError("yoetz timed out")):
                item = self.long_item()
                item.update(source_kind=kind, body="short")
                out, added = self.capture(item, "Short summary.", exc=exc)
                self.assertEqual(out["memory"], "created", (kind, exc))

    def test_long_summary_stored_degraded_with_summary_title(self):
        summary = "Team chose option B for EU dunning. " + "Detail. " * 40
        out, added = self.capture(self.long_item(), summary)
        self.assertEqual(out["memory"], "created")
        args = added["args"]
        title = args[args.index("--title") + 1]
        self.assertEqual(title, "Team chose option B for EU dunning.")
        self.assertIn("extract-degraded", args[args.index("--tags") + 1])

    def test_summary_title_never_empty_or_long(self):
        self.assertEqual(memory_sink._summary_title("## **Heading text.** More"),
                         "Heading text.")
        self.assertLessEqual(len(memory_sink._summary_title("a" * 500)), 120)
        self.assertEqual(memory_sink._summary_title(""), "untitled")


if __name__ == "__main__":
    unittest.main()
