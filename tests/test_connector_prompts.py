"""connector_prompts: layered resolution, composition, and loud failure."""
import os
import tempfile
import unittest
from unittest import mock

from workspace_daemon import config, connector_prompts, llm

TEMPLATE_BODY = """# What is memory-worthy in Slack

Capture decisions, commitments, and incidents. Ignore bot noise and chatter."""

OVERRIDE_BODY = """# What is memory-worthy in Slack (personalized)

Capture rollout steps verbatim, including config keys. Ignore standups."""


def write(path, frontmatter, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{frontmatter}\n---\n\n{body}\n")


class StoreFixture(unittest.TestCase):
    """A temp dir shaped like a personal-memory instance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def template(self, name="slack", body=TEMPLATE_BODY):
        write(os.path.join(self.store, "connectors", f"{name}.md"),
              f"name: {name}\nenabled: true", body)

    def override(self, name="slack", body=OVERRIDE_BODY):
        write(os.path.join(self.store, "memory", "connectors", f"{name}.md"),
              f"name: {name}\nenabled: true", body)

    def routine(self, **analyze):
        base = {"id": "r", "memory": {"store": self.store},
                "analyze": {"provider": "gemini", "model": "m", **analyze}}
        return base


class ResolutionTest(StoreFixture):
    def test_template_used_when_no_override(self):
        self.template()
        body, origin = connector_prompts.read_connector_body(self.store, "slack")
        self.assertEqual(origin, "template")
        self.assertIn("Ignore bot noise", body)

    def test_override_wins(self):
        self.template()
        self.override()
        body, origin = connector_prompts.read_connector_body(self.store, "slack")
        self.assertEqual(origin, "override")
        self.assertIn("config keys", body)
        self.assertNotIn("bot noise", body)

    def test_frontmatter_is_stripped(self):
        self.template()
        body, _ = connector_prompts.read_connector_body(self.store, "slack")
        self.assertFalse(body.startswith("---"))
        self.assertNotIn("enabled: true", body)

    def test_body_without_frontmatter_still_reads(self):
        path = os.path.join(self.store, "connectors", "plain.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(TEMPLATE_BODY)
        body, _ = connector_prompts.read_connector_body(self.store, "plain")
        self.assertIn("Capture decisions", body)

    def test_missing_connector_raises(self):
        with self.assertRaises(connector_prompts.PromptError) as cm:
            connector_prompts.read_connector_body(self.store, "nope")
        self.assertIn("not found", str(cm.exception))

    def test_stub_body_raises_rather_than_degrading(self):
        """An emptied prompt must fail loudly, not summarize vaguely for hours."""
        self.template(body="TODO")
        with self.assertRaises(connector_prompts.PromptError) as cm:
            connector_prompts.read_connector_body(self.store, "slack")
        self.assertIn("no usable prompt body", str(cm.exception))


class InstructionCompositionTest(StoreFixture):
    def test_inline_instruction_passthrough(self):
        r = self.routine(instruction="  Summarize the thread.  ")
        self.assertEqual(connector_prompts.resolve_instruction(r),
                         "Summarize the thread.")

    def test_connector_instruction(self):
        self.override()
        r = self.routine(instruction_from_connector="slack")
        with mock.patch.object(connector_prompts, "log"):
            out = connector_prompts.resolve_instruction(r)
        self.assertIn("config keys", out)

    def test_extra_is_appended_to_connector_base(self):
        self.override()
        r = self.routine(instruction_from_connector="slack",
                         instruction_extra="In this channel, keep every config flip.")
        with mock.patch.object(connector_prompts, "log"):
            out = connector_prompts.resolve_instruction(r)
        self.assertTrue(out.startswith("# What is memory-worthy"))
        self.assertTrue(out.endswith("keep every config flip."))

    def test_extra_is_appended_to_inline_base(self):
        r = self.routine(instruction="Base.", instruction_extra="Also this.")
        self.assertEqual(connector_prompts.resolve_instruction(r), "Base.\n\nAlso this.")

    def test_connector_without_store_raises(self):
        r = {"id": "r", "analyze": {"instruction_from_connector": "slack"}}
        with self.assertRaises(connector_prompts.PromptError) as cm:
            connector_prompts.resolve_instruction(r)
        self.assertIn("memory.store", str(cm.exception))

    def test_neither_source_raises(self):
        with self.assertRaises(connector_prompts.PromptError):
            connector_prompts.resolve_instruction(self.routine())


class ValidationTest(StoreFixture):
    def test_valid_connector_reference(self):
        self.template()
        self.assertEqual(
            connector_prompts.validate(self.routine(instruction_from_connector="slack")),
            [],
        )

    def test_typo_fails_at_validate_time(self):
        """Catching this on `daemon.py validate` beats failing mid-sweep."""
        self.template()
        probs = connector_prompts.validate(self.routine(instruction_from_connector="slak"))
        self.assertTrue(any("not found" in p for p in probs))

    def test_both_sources_rejected(self):
        self.template()
        probs = connector_prompts.validate(
            self.routine(instruction="inline", instruction_from_connector="slack"))
        self.assertTrue(any("not both" in p for p in probs))

    def test_neither_source_rejected(self):
        probs = connector_prompts.validate(self.routine())
        self.assertTrue(any("instruction_from_connector" in p for p in probs))

    def test_path_traversal_rejected(self):
        probs = connector_prompts.validate(
            self.routine(instruction_from_connector="../../etc/passwd"))
        self.assertTrue(any("bare connector name" in p for p in probs))

    def test_connector_without_store_rejected(self):
        r = {"id": "r", "analyze": {"provider": "g", "model": "m",
                                    "instruction_from_connector": "slack"}}
        probs = connector_prompts.validate(r)
        self.assertTrue(any("requires memory.store" in p for p in probs))

    def test_placeholder_store_skips_resolution(self):
        """A template routine's placeholder path is a store problem, not a
        connector problem — don't report a misleading 'connector not found'."""
        r = {"id": "r", "memory": {"store": "/absolute/path/to/your/store"},
             "analyze": {"provider": "g", "model": "m",
                         "instruction_from_connector": "slack"}}
        self.assertEqual(connector_prompts.validate(r), [])

    def test_non_string_extra_rejected(self):
        probs = connector_prompts.validate(
            self.routine(instruction="x", instruction_extra=["a"]))
        self.assertTrue(any("must be a string" in p for p in probs))


class RoutineValidationTest(StoreFixture):
    """The core validator delegates instruction checks but keeps its own."""

    def _routine(self, **analyze):
        return {
            "id": "r",
            "source": {"kind": "slack", "channels": ["C1"]},
            "analyze": {"provider": "gemini", "model": "m", "max_output_tokens": 4096,
                        **analyze},
            "memory": {"store": self.store, "type": "note"},
        }

    def test_connector_backed_routine_is_valid(self):
        self.override()
        self.assertEqual(config.validate(self._routine(instruction_from_connector="slack")), [])

    def test_inline_routine_still_valid(self):
        self.assertEqual(config.validate(self._routine(instruction="Summarize it.")), [])

    def test_missing_instruction_reported_once(self):
        probs = config.validate(self._routine())
        self.assertEqual(sum("instruction" in p for p in probs), 1)

    def test_connector_sweep_requires_every_source_to_match_connector(self):
        self.override()
        routine = self._routine(
            instruction_from_connector="slack",
            connector_sweep=True,
        )
        routine["source"] = {"kind": "gchat", "all_spaces": True}
        probs = config.validate(routine)
        self.assertTrue(
            any("every source block to use 'slack'" in p for p in probs),
            probs,
        )

    def test_gchat_connector_sweep_requires_all_spaces(self):
        self.override(name="gchat")
        routine = self._routine(
            instruction_from_connector="gchat",
            connector_sweep=True,
        )
        routine["source"] = {
            "kind": "gchat",
            "spaces": ["spaces/EXAMPLE"],
        }
        probs = config.validate(routine)
        self.assertTrue(
            any("exactly one source with all_spaces: true" in p for p in probs),
            probs,
        )

    def test_gchat_connector_sweep_rejects_per_space_cap(self):
        self.override(name="gchat")
        routine = self._routine(
            instruction_from_connector="gchat",
            connector_sweep=True,
        )
        routine["source"] = {
            "kind": "gchat",
            "all_spaces": True,
            "max_results": 0,
            "max_per_space": 25,
            "catch_up": True,
            "batch_messages": "daily",
        }
        probs = config.validate(routine)
        self.assertTrue(
            any("source.max_per_space: 0" in p for p in probs),
            probs,
        )

    def test_slack_connector_sweep_requires_catch_up(self):
        self.override()
        routine = self._routine(
            instruction_from_connector="slack",
            connector_sweep=True,
        )
        routine["source"]["max_results"] = 0
        probs = config.validate(routine)
        self.assertTrue(
            any("catch_up: true on every source" in p for p in probs),
            probs,
        )

    def test_connector_sweep_must_be_boolean(self):
        self.override()
        probs = config.validate(self._routine(
            instruction_from_connector="slack",
            connector_sweep="yes",
        ))
        self.assertTrue(
            any("connector_sweep must be true or false" in p for p in probs),
            probs,
        )

    def test_connector_sweep_rejects_capped_source(self):
        self.override(name="gmail")
        routine = self._routine(
            instruction_from_connector="gmail",
            connector_sweep=True,
        )
        routine["source"] = {
            "kind": "gmail",
            "query": "in:inbox",
            "max_results": 25,
        }
        probs = config.validate(routine)
        self.assertTrue(
            any("max_results: 0 on every source" in p for p in probs),
            probs,
        )


class PromptBuildTest(StoreFixture):
    def test_build_prompt_embeds_connector_body(self):
        self.override()
        routine = self.routine(instruction_from_connector="slack")
        routine["analyze"]["focus_domains"] = ["Tracking"]
        item = {"title": "t", "date": "2026-07-27", "body": "conversation",
                "frontmatter": {}}
        with mock.patch.object(connector_prompts, "log"):
            prompt = llm.build_prompt(routine, item, [])
        self.assertIn("config keys", prompt)      # connector body reached the model
        self.assertIn("Focus domains: Tracking", prompt)
        self.assertIn("conversation", prompt)

    def test_build_prompt_embeds_gchat_space_context(self):
        self.override()
        routine = self.routine(instruction_from_connector="slack")
        item = {
            "source_kind": "gchat",
            "title": "An active slice",
            "date": "2026-07-27",
            "body": "Jane Doe: We decided to proceed.",
            "frontmatter": {
                "gchat_space": "spaces/DM1",
                "gchat_space_display_name": "",
                "gchat_space_type": "DIRECT_MESSAGE",
                "gchat_space_members": ["Jane Doe", "Omri Ariav"],
                "gchat_space_member_count": 2,
                "gchat_participants": ["Jane Doe"],
                "message_count": 1,
            },
        }
        with mock.patch.object(connector_prompts, "log"):
            prompt = llm.build_prompt(routine, item, [])
        self.assertIn("Source: Google Chat", prompt)
        self.assertIn("Conversation type: DIRECT_MESSAGE", prompt)
        self.assertIn("Members: Jane Doe, Omri Ariav", prompt)
        self.assertIn("Participants in supplied messages: Jane Doe", prompt)
        self.assertIn("Messages in supplied window: 1", prompt)

    def test_build_prompt_uses_count_for_large_gchat_space(self):
        self.override()
        routine = self.routine(instruction_from_connector="slack")
        item = {
            "source_kind": "gchat",
            "title": "An active slice",
            "date": "2026-07-27",
            "body": "Jane Doe: A material update.",
            "frontmatter": {
                "gchat_space": "spaces/LARGE",
                "gchat_space_display_name": "Large room",
                "gchat_space_type": "SPACE",
                "gchat_space_members": [],
                "gchat_space_member_count": 250,
                "gchat_participants": ["Jane Doe"],
                "message_count": 1,
            },
        }
        with mock.patch.object(connector_prompts, "log"):
            prompt = llm.build_prompt(routine, item, [])
        self.assertIn("Space: Large room", prompt)
        self.assertIn("Member count: 250", prompt)
        self.assertNotIn("Members:", prompt)

    def test_build_prompt_embeds_gmail_recipients_and_thread_coverage(self):
        self.override()
        routine = self.routine(instruction_from_connector="slack")
        item = {
            "source_kind": "gmail",
            "title": "Review request",
            "date": "2026-07-27",
            "body": "Please review the proposal by Friday.",
            "frontmatter": {
                "email_from": "Sender <sender@example.com>",
                "email_to": "Leader <leader@example.com>",
                "email_cc": "Reviewer <reviewer@example.com>",
                "gmail_thread_message_count": 80,
                "gmail_thread_messages_included": 50,
                "gmail_thread_truncated": True,
                "gmail_labels": ["INBOX", "SENT", "STARRED"],
                "gmail_manual_chat_followup": True,
                "gmail_chat_followup_managed": True,
                "gmail_chat_followup_active": True,
            },
        }
        with mock.patch.object(connector_prompts, "log"):
            prompt = llm.build_prompt(routine, item, [])
        self.assertIn("From: Sender <sender@example.com>", prompt)
        self.assertIn("To: Leader <leader@example.com>", prompt)
        self.assertIn("Cc: Reviewer <reviewer@example.com>", prompt)
        self.assertIn("Messages in supplied thread: 50 of 80", prompt)
        self.assertIn("Coverage warning:", prompt)
        self.assertIn("Gmail labels: INBOX, SENT, STARRED", prompt)
        self.assertIn("manually forwarded this Chat message", prompt)
        self.assertIn("it remains open", prompt)


if __name__ == "__main__":
    unittest.main()
