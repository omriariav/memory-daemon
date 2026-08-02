"""Multi-source routine, ownership, and cadence regression tests."""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import daemon as daemon_cli
from workspace_daemon import (
    actions,
    config,
    llm,
    memory_sink,
    runner,
    slack_source,
    state,
    time_utils,
)


def multi_routine(vault, routine_id="domain", **extra):
    routine = {
        "id": routine_id,
        "enabled": True,
        "schedule": {"every": "4h"},
        "sources": [
            {"kind": "gmail", "query": "is:unread", "actions": ["archive"]},
            {"kind": "gchat", "spaces": ["spaces/EXAMPLE"]},
        ],
        "analyze": {
            "provider": "gemini",
            "model": "m",
            "instruction": "Keep durable decisions.",
        },
        "output": {"vault_dir": str(vault), "slug_prefix": routine_id},
    }
    routine.update(extra)
    return routine


class MultiSourceValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_valid_multi_source_routine(self):
        self.assertEqual(config.validate(multi_routine(self.tmp.name)), [])

    def test_legacy_single_source_remains_valid(self):
        routine = multi_routine(self.tmp.name)
        routine["source"] = routine.pop("sources")[0]
        routine["actions"] = routine["source"].pop("actions")
        self.assertEqual(config.validate(routine), [])

    def test_source_and_sources_are_mutually_exclusive(self):
        routine = multi_routine(self.tmp.name)
        routine["source"] = {"kind": "gmail", "query": "in:inbox"}
        self.assertTrue(any("not both" in p for p in config.validate(routine)))

    def test_non_gmail_source_rejects_actions(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"][1]["actions"] = ["archive"]
        self.assertTrue(any("does not support Gmail actions" in p
                            for p in config.validate(routine)))

    def test_read_thread_is_boolean_and_gmail_only(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"][0]["read_thread"] = "yes"
        routine["sources"][1]["read_thread"] = True
        problems = config.validate(routine)
        self.assertTrue(
            any("read_thread must be true or false" in p for p in problems),
            problems,
        )
        self.assertTrue(
            any("read_thread is supported only for gmail" in p for p in problems),
            problems,
        )

    def test_read_thread_rejects_message_oriented_streams(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"][0]["read_thread"] = True
        routine["streams"] = {
            "Weekly report": {"message_updates": True},
        }

        problems = config.validate(routine)

        self.assertTrue(
            any(
                "read_thread cannot be combined with "
                "streams.*.message_updates" in problem
                for problem in problems
            ),
            problems,
        )

    def test_self_forwarded_chat_followups_require_gmail_and_memory(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"][0]["self_forwarded_chat_followups"] = True
        routine["sources"][1]["self_forwarded_chat_followups"] = True

        problems = config.validate(routine)

        self.assertTrue(
            any("supported only for gmail" in problem for problem in problems),
            problems,
        )
        self.assertTrue(
            any("requires a memory block" in problem for problem in problems),
            problems,
        )

    def test_self_forwarded_chat_followup_flag_must_be_boolean(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"][0]["self_forwarded_chat_followups"] = "yes"

        problems = config.validate(routine)

        self.assertTrue(
            any("must be true or false" in problem for problem in problems),
            problems,
        )

    def test_self_forwarded_chat_followups_require_read_only_actions(self):
        routine = multi_routine(self.tmp.name)
        routine["memory"] = {"store": self.tmp.name, "type": "note"}
        routine["sources"][0]["self_forwarded_chat_followups"] = True

        problems = config.validate(routine)

        self.assertTrue(
            any("requires actions: []" in problem for problem in problems),
            problems,
        )

        routine["sources"][0]["actions"] = []
        self.assertFalse(
            any(
                "requires actions: []" in problem
                for problem in config.validate(routine)
            )
        )

    def test_self_forwarded_chat_followups_reject_legacy_actions(self):
        routine = multi_routine(self.tmp.name)
        routine["source"] = routine.pop("sources")[0]
        routine["actions"] = routine["source"].pop("actions")
        routine["source"]["self_forwarded_chat_followups"] = True
        routine["memory"] = {"store": self.tmp.name, "type": "note"}

        problems = config.validate(routine)

        self.assertTrue(
            any("requires actions: []" in problem for problem in problems),
            problems,
        )

    def test_multi_source_rejects_routine_level_actions(self):
        routine = multi_routine(self.tmp.name)
        routine["actions"] = ["archive"]
        self.assertTrue(any("put `actions` on each Gmail source" in p
                            for p in config.validate(routine)))

    def test_schedule_and_routing_validation(self):
        routine = multi_routine(self.tmp.name)
        routine["schedule"] = {"every": "sometimes"}
        routine["routing"] = {"fallback": "yes", "priority": True}
        problems = config.validate(routine)
        self.assertTrue(any("schedule.every" in p for p in problems), problems)
        self.assertTrue(any("routing.fallback" in p for p in problems), problems)
        self.assertTrue(any("routing.priority" in p for p in problems), problems)

    def test_named_handler_inherits_defaults_and_satisfies_source_actions(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"] = [
            {
                "kind": "gmail",
                "query": 'from:notes@example.com',
                "max_results": 0,
                "handler": "meeting-notes",
                "actions": ["apply_label", "archive"],
            },
            {
                "kind": "gmail",
                "query": "in:inbox",
                "catch_up": True,
                "catch_up_after": "2026-07-01T00:00:00Z",
                "max_results": 0,
                "actions": [],
            },
        ]
        routine["handlers"] = {
            "meeting-notes": {
                "analyze": {
                    "instruction": "Extract decisions and commitments.",
                    "pick_label": True,
                },
                "memory": {"type": "meeting", "tags": ["meeting"]},
            }
        }
        routine["memory"] = {"store": self.tmp.name, "type": "note"}

        self.assertEqual(config.validate(routine), [])
        effective = config.routine_for_source(routine, routine["sources"][0])
        self.assertEqual(effective["_handler_id"], "meeting-notes")
        self.assertEqual(effective["analyze"]["provider"], "gemini")
        self.assertEqual(effective["analyze"]["model"], "m")
        self.assertEqual(
            effective["analyze"]["instruction"],
            "Extract decisions and commitments.",
        )
        self.assertEqual(effective["memory"]["store"], self.tmp.name)
        self.assertEqual(effective["memory"]["type"], "meeting")

    def test_inline_handler_replaces_inherited_connector_prompt_metadata(self):
        routine = multi_routine(self.tmp.name)
        routine["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gmail",
            "instruction_extra": "General-only guidance.",
            "connector_sweep": True,
        }
        routine["handlers"] = {
            "special": {
                "analyze": {"instruction": "Specialized guidance."},
            }
        }
        routine["sources"][0]["handler"] = "special"

        effective = config.routine_for_source(routine, routine["sources"][0])

        self.assertEqual(effective["analyze"]["instruction"], "Specialized guidance.")
        self.assertNotIn("instruction_from_connector", effective["analyze"])
        self.assertNotIn("instruction_extra", effective["analyze"])
        self.assertNotIn("connector_sweep", effective["analyze"])

    def test_invalid_handler_instruction_cannot_fall_back_to_connector_prompt(self):
        store = Path(self.tmp.name)
        connector = store / "memory" / "connectors" / "gmail.md"
        connector.parent.mkdir(parents=True)
        connector.write_text(
            "Keep durable decisions, commitments, blockers, and reusable facts."
        )
        routine = multi_routine(self.tmp.name)
        routine["sources"] = [{
            "kind": "gmail",
            "query": "known source",
            "max_results": 0,
            "handler": "known",
            "actions": [],
        }]
        routine["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gmail",
        }
        routine["handlers"] = {
            "known": {"analyze": {"instruction": []}},
        }
        routine["memory"] = {"store": self.tmp.name, "type": "note"}

        effective = config.routine_for_source(routine, routine["sources"][0])
        problems = config.validate(routine)

        self.assertNotIn("instruction_from_connector", effective["analyze"])
        self.assertEqual(effective["analyze"]["instruction"], [])
        self.assertTrue(
            any("instruction must be a non-empty string" in p for p in problems),
            problems,
        )

    def test_handler_references_and_profiles_fail_closed(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"][0]["handler"] = "missing"
        problems = config.validate(routine)
        self.assertTrue(any("no `handlers` mapping" in p for p in problems), problems)

        routine["handlers"] = {
            "missing": {"actions": ["archive"]},
            "unused": {"analyze": {"instruction": "Never selected."}},
        }
        problems = config.validate(routine)
        self.assertTrue(any("unknown key(s) actions" in p for p in problems), problems)
        self.assertTrue(any("unused handler(s): unused" in p for p in problems), problems)

    def test_connector_sweep_accepts_uncapped_same_medium_handler_sources(self):
        store = Path(self.tmp.name)
        connector = store / "memory" / "connectors" / "gmail.md"
        connector.parent.mkdir(parents=True)
        connector.write_text(
            "Keep durable decisions, commitments, blockers, and reusable facts."
        )
        routine = multi_routine(self.tmp.name)
        routine["sources"] = [
            {
                "kind": "gmail",
                "query": 'from:reports@example.com',
                "max_results": 0,
                "handler": "reports",
                "actions": [],
            },
            {
                "kind": "gmail",
                "query": "in:inbox",
                "catch_up": True,
                "catch_up_after": "2026-07-01T00:00:00Z",
                "max_results": 0,
                "actions": [],
            },
        ]
        routine["handlers"] = {
            "reports": {
                "analyze": {"instruction": "Extract durable report facts."},
            }
        }
        routine["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gmail",
            "connector_sweep": True,
        }
        routine["memory"] = {"store": self.tmp.name, "type": "note"}

        self.assertEqual(config.validate(routine), [])

        routine["sources"][0]["max_results"] = 20
        problems = config.validate(routine)
        self.assertTrue(
            any("max_results: 0 on every source" in p for p in problems),
            problems,
        )

    def test_every_source_effective_profile_requires_a_sink(self):
        routine = multi_routine(self.tmp.name)
        routine.pop("output")
        routine["sources"] = [
            {
                "kind": "gmail",
                "query": 'from:reports@example.com',
                "handler": "reports",
                "actions": [],
            },
            {
                "kind": "gmail",
                "query": "newer_than:1d",
                "actions": [],
            },
        ]
        routine["handlers"] = {
            "reports": {
                "memory": {"store": self.tmp.name, "type": "note"},
            }
        }

        problems = config.validate(routine)

        self.assertTrue(
            any("sources[1] needs an `output:`" in p for p in problems),
            problems,
        )
        self.assertFalse(
            any("sources[0]" in p and "needs an `output:`" in p for p in problems),
            problems,
        )

    def test_empty_handler_memory_is_not_a_sink(self):
        routine = multi_routine(self.tmp.name)
        routine.pop("output")
        routine["sources"] = [{
            "kind": "gmail",
            "query": "known source",
            "max_results": 0,
            "handler": "known",
            "actions": [],
        }]
        routine["handlers"] = {"known": {"memory": {}}}

        problems = config.validate(routine)

        self.assertTrue(any("needs an `output:`" in p for p in problems), problems)
        self.assertTrue(
            any("`memory` must be a non-empty mapping" in p for p in problems),
            problems,
        )

    def test_handler_queries_must_be_uncapped(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"][0].update({
            "handler": "known",
            "max_results": 20,
        })
        routine["handlers"] = {
            "known": {"analyze": {"instruction": "Known-source extraction."}},
        }

        problems = config.validate(routine)

        self.assertTrue(
            any("handler requires max_results: 0" in p for p in problems),
            problems,
        )

    def test_handler_cannot_own_delayed_followup_lifecycle(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"] = [{
            "kind": "gmail",
            "query": 'in:inbox from:me to:me subject:"Fwd: Chat"',
            "max_results": 0,
            "handler": "followup",
            "self_forwarded_chat_followups": True,
            "actions": [],
        }]
        routine["handlers"] = {
            "followup": {
                "memory": {"store": self.tmp.name, "type": "note"},
            }
        }

        problems = config.validate(routine)

        self.assertTrue(
            any("must use the routine-level default handler" in p for p in problems),
            problems,
        )

    def test_operator_confirmed_replays_are_routine_level_only(self):
        routine = multi_routine(self.tmp.name)
        routine["sources"][0].update({
            "handler": "known",
            "max_results": 0,
        })
        routine["handlers"] = {
            "known": {
                "memory": {
                    "store": self.tmp.name,
                    "type": "note",
                    "operator_confirmed_source_ids": ["gmail:thread-1"],
                },
            }
        }

        problems = config.validate(routine)

        self.assertTrue(
            any("put exact replay overrides in the routine-level" in p for p in problems),
            problems,
        )

    def test_runtime_handler_marker_cannot_be_spoofed_in_yaml(self):
        routine = multi_routine(self.tmp.name)
        routine["_handler_id"] = "spoofed"

        problems = config.validate(routine)

        self.assertTrue(any("reserved runtime metadata" in p for p in problems), problems)


class OwnershipRoutingTest(unittest.TestCase):
    @staticmethod
    def claim(routine, item_id="same"):
        return {
            "routine": routine,
            "source": {"kind": "gmail", "query": "in:inbox"},
            "source_index": 0,
            "candidate": {"id": item_id, "title": item_id},
            "fetch": None,
        }

    def test_specific_owner_beats_fallback(self):
        specific = {"id": "specific"}
        fallback = {"id": "fallback", "routing": {"fallback": True}}
        totals = {"errors": 0, "ambiguous": 0}
        owned = runner._route_claims(
            {("gmail", "same"): [self.claim(fallback), self.claim(specific)]},
            totals,
        )
        self.assertEqual(list(owned), ["specific"])
        self.assertEqual(totals, {"errors": 0, "ambiguous": 0})

    def test_gmail_ownership_uses_thread_id_not_matching_message_id(self):
        specific = {"id": "privacy"}
        fallback = {"id": "general", "routing": {"fallback": True}}
        privacy_claim = self.claim(specific, "privacy-message")
        general_claim = self.claim(fallback, "latest-reply")
        privacy_claim["candidate"]["raw"] = {"thread_id": "shared-thread"}
        general_claim["candidate"]["raw"] = {"thread_id": "shared-thread"}
        claims = {}
        for claim in (privacy_claim, general_claim):
            key = ("gmail", runner._routing_id(claim["candidate"]))
            claims.setdefault(key, []).append(claim)

        totals = {"errors": 0, "ambiguous": 0}
        owned = runner._route_claims(claims, totals)

        self.assertEqual(list(claims), [("gmail", "shared-thread")])
        self.assertEqual(list(owned), ["privacy"])
        routed = owned["privacy"][0]
        self.assertEqual(routed["candidate"]["id"], "privacy-message")
        self.assertTrue(
            routed["candidate"]["raw"]["_gmail_routed_thread"]
        )
        self.assertEqual(totals, {"errors": 0, "ambiguous": 0})

    def test_lower_priority_wins(self):
        first = {"id": "first", "routing": {"priority": 10}}
        second = {"id": "second", "routing": {"priority": 20}}
        totals = {"errors": 0, "ambiguous": 0}
        owned = runner._route_claims(
            {("gmail", "same"): [self.claim(second), self.claim(first)]},
            totals,
        )
        self.assertEqual(list(owned), ["first"])

    def test_equal_rank_is_ambiguous_not_file_order(self):
        one, two = {"id": "one"}, {"id": "two"}
        totals = {"errors": 0, "ambiguous": 0}
        owned = runner._route_claims(
            {("gmail", "same"): [self.claim(one), self.claim(two)]},
            totals,
        )
        self.assertEqual(owned, {})
        self.assertEqual(totals, {"errors": 1, "ambiguous": 1})

    def test_managed_followup_claim_beats_ordinary_same_routine_claim(self):
        routine = {"id": "gmail-sweep"}
        ordinary = self.claim(routine)
        managed = self.claim(routine)
        managed["source"] = {
            "kind": "gmail",
            "query": "in:inbox",
            "self_forwarded_chat_followups": True,
            "actions": [],
        }
        managed["candidate"]["raw"] = {
            "_gmail_chat_followup_candidate": True,
        }
        totals = {"errors": 0, "ambiguous": 0}

        owned = runner._route_claims(
            {("gmail", "same"): [ordinary, managed]}, totals,
        )

        self.assertIs(owned["gmail-sweep"][0], managed)

    def test_operator_replay_uses_unhandled_general_source_not_first_handler(self):
        routine = {
            "id": "gmail-general",
            "sources": [
                {
                    "kind": "gmail",
                    "query": "known meeting",
                    "max_results": 0,
                    "handler": "meeting-notes",
                    "actions": ["archive"],
                },
                {
                    "kind": "gmail",
                    "query": "general inbox",
                    "max_results": 0,
                    "actions": [],
                },
            ],
            "handlers": {
                "meeting-notes": {
                    "analyze": {"instruction": "Meeting extraction."},
                }
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "General extraction.",
            },
            "memory": {
                "store": "/tmp/memory",
                "type": "note",
                "operator_confirmed_source_ids": ["gmail:confirmed-thread"],
            },
        }
        processed = SimpleNamespace(
            items=lambda: [(
                "old-message",
                {
                    "memory_source_id": "gmail:confirmed-thread",
                    "memory": "skipped_not_worthy",
                    "processed_at": "2026-08-01T00:00:00Z",
                },
            )]
        )
        saved = runner.SOURCES["gmail"]
        runner.SOURCES["gmail"] = (lambda _source: [], saved[1])
        self.addCleanup(runner.SOURCES.__setitem__, "gmail", saved)
        totals = {"errors": 0}

        claims, failures = runner._collect_claims(
            [routine], totals, processed=processed,
        )

        self.assertEqual(failures, [])
        replay = claims[("gmail", "confirmed-thread")][0]
        self.assertNotIn("handler", replay["source"])
        self.assertEqual(replay["source"]["actions"], [])

    def test_first_same_routine_handler_claim_is_never_replaced_by_fallback(self):
        routine = {"id": "gmail-general"}
        handler = self.claim(routine)
        handler.update({
            "source_index": 0,
            "source": {
                "kind": "gmail",
                "query": "known source",
                "handler": "known",
            },
            "processable": False,
        })
        fallback = self.claim(routine)
        fallback.update({
            "source_index": 1,
            "source": {"kind": "gmail", "query": "general"},
            "processable": True,
        })
        totals = {"errors": 0, "ambiguous": 0}

        owned = runner._route_claims(
            {("gmail", "same"): [handler, fallback]}, totals,
        )

        self.assertEqual(owned, {})
        self.assertEqual(totals, {"errors": 0, "ambiguous": 0})

    def test_explicit_followup_queue_beats_ordinary_specialized_claim(self):
        specialized = self.claim({"id": "specialized"})
        managed = self.claim({
            "id": "gmail-sweep",
            "routing": {"fallback": True},
        })
        managed["source"] = {
            "kind": "gmail",
            "query": "in:inbox",
            "self_forwarded_chat_followups": True,
            "actions": [],
        }
        managed["candidate"]["raw"] = {
            "_gmail_chat_followup_candidate": True,
        }
        totals = {"errors": 0, "ambiguous": 0}

        owned = runner._route_claims(
            {("gmail", "same"): [specialized, managed]}, totals,
        )

        self.assertEqual(list(owned), ["gmail-sweep"])
        self.assertIs(owned["gmail-sweep"][0], managed)

    def test_failed_managed_listing_blocks_ordinary_followup_claim(self):
        specialized = {
            "id": "specialized",
            "source": {
                "kind": "gmail",
                "query": "specialized",
                "max_results": 0,
            },
        }
        managed = {
            "id": "gmail-sweep",
            "routing": {"fallback": True},
            "source": {
                "kind": "gmail",
                "query": "general",
                "max_results": 0,
                "self_forwarded_chat_followups": True,
                "actions": [],
            },
        }

        def candidates(source):
            if source.get("_gmail_chat_followup_listing"):
                raise RuntimeError("queue unavailable")
            if source["query"] == "specialized":
                return [{
                    "id": "message-1",
                    "title": "Fwd: Chat with a colleague",
                    "raw": {},
                }]
            return []

        saved = runner.SOURCES["gmail"]
        runner.SOURCES["gmail"] = (candidates, saved[1])
        self.addCleanup(runner.SOURCES.__setitem__, "gmail", saved)
        totals = {"errors": 0, "ambiguous": 0}

        claims, failures = runner._collect_claims(
            [specialized, managed], totals,
        )
        owned = runner._route_claims(claims, totals, failures)

        self.assertEqual(owned, {})
        self.assertEqual(totals["errors"], 1)

    def test_failed_ordinary_listing_does_not_block_managed_followup(self):
        specialized = {
            "id": "specialized",
            "source": {
                "kind": "gmail",
                "query": "specialized",
                "max_results": 0,
            },
        }
        managed = {
            "id": "gmail-sweep",
            "routing": {"fallback": True},
            "source": {
                "kind": "gmail",
                "query": "general",
                "max_results": 0,
                "self_forwarded_chat_followups": True,
                "actions": [],
            },
        }

        def candidates(source):
            if source["query"] == "specialized":
                raise RuntimeError("ordinary source unavailable")
            if source.get("_gmail_chat_followup_listing"):
                return [{
                    "id": "message-1",
                    "title": "Fwd: Chat with a colleague",
                    "raw": {"_gmail_chat_followup_candidate": True},
                }]
            return []

        saved = runner.SOURCES["gmail"]
        runner.SOURCES["gmail"] = (candidates, saved[1])
        self.addCleanup(runner.SOURCES.__setitem__, "gmail", saved)
        totals = {"errors": 0, "ambiguous": 0}

        claims, failures = runner._collect_claims(
            [specialized, managed], totals,
        )
        owned = runner._route_claims(claims, totals, failures)

        self.assertEqual(list(owned), ["gmail-sweep"])
        self.assertEqual(totals["errors"], 1)

    def test_chat_versions_share_one_routing_identity(self):
        first = {
            "id": "gchat:SPACE:THREAD@2026-07-27T10:00:00Z",
            "raw": {"source_id": "gchat:SPACE:THREAD"},
        }
        second = {
            "id": "gchat:SPACE:THREAD@2026-07-27T11:00:00Z",
            "raw": {"source_id": "gchat:SPACE:THREAD"},
        }
        self.assertEqual(runner._routing_id(first), runner._routing_id(second))


class SlackCrossRoutineOwnershipTest(unittest.TestCase):
    def test_runner_passes_all_declared_channels_to_mention_sweep(self):
        owner = {
            "id": "domain",
            "source": {"kind": "slack", "ada_channels": ["COWNED"]},
        }
        sweep = {
            "id": "sweep",
            "source": {"kind": "slack", "include_mentions": True},
        }
        observed = []

        def candidates(source):
            observed.append(source)
            return []

        saved = runner.SOURCES["slack"]
        runner.SOURCES["slack"] = (candidates, saved[1])
        self.addCleanup(runner.SOURCES.__setitem__, "slack", saved)

        totals = {"errors": 0}
        runner._collect_claims([owner, sweep], totals)

        mention_source = next(
            source for source in observed if source.get("include_mentions")
        )
        self.assertEqual(
            mention_source["_exclude_mention_channels"],
            ["COWNED"],
        )

    def test_disabled_owner_channel_is_reserved_from_mention_sweep(self):
        owner = {
            "id": "domain",
            "enabled": False,
            "source": {"kind": "slack", "private_channels": ["COWNED"]},
        }
        sweep = {
            "id": "sweep",
            "source": {"kind": "slack", "include_mentions": True},
        }
        observed = []

        def candidates(source):
            observed.append(source)
            return []

        saved = runner.SOURCES["slack"]
        runner.SOURCES["slack"] = (candidates, saved[1])
        self.addCleanup(runner.SOURCES.__setitem__, "slack", saved)

        totals = {"errors": 0}
        runner._collect_claims(
            [sweep], totals, routing_context=[owner, sweep],
        )

        mention_source = next(
            source for source in observed if source.get("include_mentions")
        )
        self.assertEqual(
            mention_source["_exclude_mention_channels"],
            ["COWNED"],
        )


class GChatFallbackOwnershipTest(unittest.TestCase):
    def test_specific_space_beats_all_space_fallback(self):
        specific = {
            "id": "domain",
            "source": {"kind": "gchat", "spaces": ["spaces/OWNED"]},
        }
        fallback = {
            "id": "sweep",
            "routing": {"fallback": True},
            "source": {
                "kind": "gchat",
                "all_spaces": True,
                "max_results": 0,
            },
        }

        def candidate(space):
            short = space.split("/")[-1]
            return {
                "id": f"gchat:{short}:thread@1",
                "title": short,
                "raw": {
                    "source_id": f"gchat:{short}:thread",
                    "space": space,
                },
            }

        def candidates(source):
            if source.get("all_spaces"):
                return [candidate("spaces/OWNED"), candidate("spaces/OTHER")]
            return [candidate("spaces/OWNED")]

        saved = runner.SOURCES["gchat"]
        runner.SOURCES["gchat"] = (candidates, saved[1])
        self.addCleanup(runner.SOURCES.__setitem__, "gchat", saved)

        totals = {"errors": 0, "ambiguous": 0}
        claims, failures = runner._collect_claims([specific, fallback], totals)
        owned = runner._route_claims(claims, totals, failures)

        self.assertEqual(
            {
                routine_id: {
                    claim["candidate"]["raw"]["space"]
                    for claim in routine_claims
                }
                for routine_id, routine_claims in owned.items()
            },
            {
                "domain": {"spaces/OWNED"},
                "sweep": {"spaces/OTHER"},
            },
        )
        self.assertEqual(totals, {"errors": 0, "ambiguous": 0})

    def test_disabled_specific_space_is_excluded_from_all_space_fallback(self):
        specific = {
            "id": "domain",
            "enabled": False,
            "source": {"kind": "gchat", "spaces": ["spaces/OWNED"]},
            "analyze": {
                "provider": "gemini",
                "model": "gemini/example",
                "instruction": "Keep durable facts.",
            },
            "memory": {"store": "/tmp/memory", "type": "note"},
        }
        fallback = {
            "id": "sweep",
            "routing": {"fallback": True},
            "source": {"kind": "gchat", "all_spaces": True},
            "analyze": {
                "provider": "gemini",
                "model": "gemini/example",
                "instruction": "Keep durable facts.",
            },
            "memory": {"store": "/tmp/memory", "type": "note"},
        }
        observed = []

        def candidates(source):
            observed.extend(source.get("_exclude_spaces", []))
            return []

        saved = runner.SOURCES["gchat"]
        runner.SOURCES["gchat"] = (candidates, saved[1])
        self.addCleanup(runner.SOURCES.__setitem__, "gchat", saved)

        runner.run(
            Path("/tmp"), [specific, fallback],
            active_ids={"sweep"}, dry_run=True,
        )

        self.assertIn("spaces/OWNED", observed)


class MultiSourceRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vault = self.base / "vault"
        (self.base / "state").mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.saved_sources = dict(runner.SOURCES)
        self.saved_analyze = llm.analyze
        self.saved_handlers = dict(actions._HANDLERS)
        self.applied = []

        def candidates(source):
            kind = source["kind"]
            return [{"id": f"{kind}-1", "title": f"{kind} item"}]

        def fetch(routine, source, candidate):
            kind = source["kind"]
            item = {
                "id": candidate["id"],
                "source_kind": kind,
                "title": candidate["title"],
                "date": "2026-07-27",
                "body": "A durable decision.",
                "frontmatter": {},
            }
            if kind == "gmail":
                item["frontmatter"].update({
                    "gmail_thread_id": "thread-1",
                    "email_subject": "Subject",
                    "email_from": "sender@example.com",
                })
            else:
                item["source_id"] = "gchat:EXAMPLE:thread-1"
            return item

        runner.SOURCES["gmail"] = (candidates, fetch)
        runner.SOURCES["gchat"] = (candidates, fetch)
        llm.analyze = lambda routine, prompt: "A compact summary."
        actions._HANDLERS["archive"] = (
            lambda item_id: self.applied.append(("archive", item_id))
        )

    def tearDown(self):
        runner.SOURCES.clear()
        runner.SOURCES.update(self.saved_sources)
        llm.analyze = self.saved_analyze
        actions._HANDLERS.clear()
        actions._HANDLERS.update(self.saved_handlers)

    def test_combined_sources_share_prompt_but_keep_actions_local(self):
        routine = multi_routine(self.vault)
        totals = runner.run(self.base, [routine])
        self.assertEqual(totals["processed"], 2)
        self.assertEqual(self.applied, [("archive", "gmail-1")])
        notes = list(self.vault.glob("*.md"))
        self.assertEqual(len(notes), 2)
        rendered = "\n".join(path.read_text() for path in notes)
        self.assertIn("source: gmail", rendered)
        self.assertIn("source: gchat", rendered)

    def test_chat_versions_advance_ledger_but_update_one_memory_source(self):
        versions = iter([
            {
                "id": "gchat:EXAMPLE:daily:2026-07-28@10:00:00Z",
                "title": "first slice",
                "raw": {"source_id": "gchat:EXAMPLE:daily:2026-07-28"},
            },
            {
                "id": "gchat:EXAMPLE:daily:2026-07-28@11:00:00Z",
                "title": "updated slice",
                "raw": {"source_id": "gchat:EXAMPLE:daily:2026-07-28"},
            },
        ])

        def candidates(source):
            return [next(versions)]

        def fetch(routine, source, candidate):
            return {
                "id": candidate["id"],
                "source_id": candidate["raw"]["source_id"],
                "source_kind": "gchat",
                "title": candidate["title"],
                "date": "2026-07-28",
                "body": "Complete daily context.",
                "frontmatter": {},
            }

        runner.SOURCES["gchat"] = (candidates, fetch)
        routine = {
            "id": "digest",
            "enabled": True,
            "source": {"kind": "gchat", "spaces": ["spaces/EXAMPLE"]},
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep durable decisions.",
            },
            "memory": {"store": str(self.base / "memory"), "type": "note"},
        }
        captured_sources = []

        def capture(routine, item, summary, dry_run=False):
            captured_sources.append(item["source_id"])
            return {"memory": "updated"}

        with mock.patch.object(memory_sink, "capture", side_effect=capture):
            first = runner.run(self.base, [routine])
            second = runner.run(self.base, [routine])

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 1)
        self.assertEqual(
            captured_sources,
            [
                "gchat:EXAMPLE:daily:2026-07-28",
                "gchat:EXAMPLE:daily:2026-07-28",
            ],
        )
        self.assertEqual(
            set(state.load(self.base)),
            {
                "gchat:EXAMPLE:daily:2026-07-28@10:00:00Z",
                "gchat:EXAMPLE:daily:2026-07-28@11:00:00Z",
            },
        )

    def test_due_fallback_cannot_steal_from_inactive_specific_owner(self):
        fetch = runner.SOURCES["gmail"][1]

        def capped_candidates(source):
            limit = source.get("max_results", 20)
            return [
                {"id": f"gmail-{index}", "title": f"gmail item {index}"}
                for index in range(1, 4)
            ][:limit]

        runner.SOURCES["gmail"] = (capped_candidates, fetch)
        specific = multi_routine(self.vault, "specific")
        specific["sources"] = [specific["sources"][0]]
        specific["sources"][0]["max_results"] = 1
        fallback = multi_routine(
            self.vault, "fallback", routing={"fallback": True}
        )
        fallback["sources"] = [fallback["sources"][0]]
        fallback["sources"][0]["max_results"] = 3
        totals = runner.run(
            self.base, [specific, fallback], active_ids={"fallback"}, dry_run=True
        )
        self.assertEqual(totals["processed"], 0)
        self.assertEqual(totals["matched"], 0)

    def test_owner_cap_is_processing_budget_not_ownership_boundary(self):
        fetch = runner.SOURCES["gmail"][1]

        def capped_candidates(source):
            limit = source.get("max_results", 20)
            return [
                {"id": f"gmail-{index}", "title": f"gmail item {index}"}
                for index in range(1, 4)
            ][:limit]

        runner.SOURCES["gmail"] = (capped_candidates, fetch)
        specific = multi_routine(self.vault, "specific")
        specific["sources"] = [
            {
                "kind": "gmail",
                "query": "from:specific@example.com",
                "max_results": 1,
                "actions": [],
            }
        ]
        fallback = multi_routine(
            self.vault, "fallback", routing={"fallback": True}
        )
        fallback["sources"] = [
            {
                "kind": "gmail",
                "query": "is:unread",
                "max_results": 3,
                "actions": [],
            }
        ]

        totals = runner.run(self.base, [specific, fallback], dry_run=True)

        self.assertEqual(totals["processed"], 1)
        self.assertEqual(totals["matched"], 1)
        self.assertEqual(totals["errors"], 0)

    def test_inactive_unrelated_source_kind_is_not_queried(self):
        fetch = runner.SOURCES["gmail"][1]
        drive_calls = []

        def failed_drive(_source):
            drive_calls.append(True)
            raise RuntimeError("drive unavailable")

        runner.SOURCES["drive_docs"] = (failed_drive, fetch)
        specific = multi_routine(self.vault, "specific")
        specific["sources"] = [
            {"kind": "drive_docs", "query": "specific documents"}
        ]
        fallback = multi_routine(
            self.vault, "fallback", routing={"fallback": True}
        )
        fallback["sources"] = [
            {"kind": "gmail", "query": "is:unread", "actions": []}
        ]

        totals = runner.run(
            self.base, [specific, fallback], active_ids={"fallback"}, dry_run=True
        )

        self.assertEqual(totals["processed"], 1)
        self.assertEqual(totals["matched"], 1)
        self.assertEqual(totals["errors"], 0)
        self.assertEqual(drive_calls, [])

    def test_failed_specific_listing_blocks_only_overlapping_chat_space(self):
        fetch = runner.SOURCES["gchat"][1]

        def candidates(source):
            space = source["spaces"][0]
            if space == "spaces/FAILED":
                raise RuntimeError("space unavailable")
            return [{
                "id": "gchat:SAFE:thread@1",
                "title": "safe item",
                "raw": {
                    "source_id": "gchat:SAFE:thread",
                    "space": "spaces/SAFE",
                },
            }]

        runner.SOURCES["gchat"] = (candidates, fetch)
        specific = multi_routine(self.vault, "specific")
        specific["sources"] = [
            {"kind": "gchat", "spaces": ["spaces/FAILED"]}
        ]
        fallback = multi_routine(
            self.vault, "fallback", routing={"fallback": True}
        )
        fallback["sources"] = [
            {"kind": "gchat", "spaces": ["spaces/SAFE"]}
        ]

        totals = runner.run(
            self.base, [specific, fallback], active_ids={"fallback"}, dry_run=True
        )

        self.assertEqual(totals["processed"], 1)
        self.assertEqual(totals["matched"], 1)
        self.assertEqual(totals["errors"], 1)

    def test_failed_specific_listing_holds_overlapping_fallback(self):
        fetch = runner.SOURCES["gmail"][1]

        def candidates(source):
            if source["query"] == "from:specific@example.com":
                raise RuntimeError("gmail unavailable")
            return [{"id": "gmail-1", "title": "gmail item"}]

        runner.SOURCES["gmail"] = (candidates, fetch)
        specific = multi_routine(self.vault, "specific")
        specific["sources"] = [
            {
                "kind": "gmail",
                "query": "from:specific@example.com",
                "actions": [],
            }
        ]
        fallback = multi_routine(
            self.vault, "fallback", routing={"fallback": True}
        )
        fallback["sources"] = [
            {"kind": "gmail", "query": "is:unread", "actions": []}
        ]

        totals = runner.run(
            self.base, [specific, fallback], active_ids={"fallback"}, dry_run=True
        )

        self.assertEqual(totals["processed"], 0)
        self.assertEqual(totals["matched"], 0)
        self.assertEqual(totals["errors"], 1)

    def test_failed_expanded_scan_keeps_proven_claim_and_holds_overflow(self):
        fetch = runner.SOURCES["gmail"][1]

        def candidates(source):
            if (
                source["query"] == "from:specific@example.com"
                and source.get("max_results") == 3
            ):
                raise RuntimeError("expanded scan unavailable")
            limit = source.get("max_results", 20)
            return [
                {"id": f"gmail-{index}", "title": f"gmail item {index}"}
                for index in range(1, 4)
            ][:limit]

        runner.SOURCES["gmail"] = (candidates, fetch)
        specific = multi_routine(self.vault, "specific")
        specific["sources"] = [
            {
                "kind": "gmail",
                "query": "from:specific@example.com",
                "max_results": 1,
                "actions": [],
            }
        ]
        fallback = multi_routine(
            self.vault, "fallback", routing={"fallback": True}
        )
        fallback["sources"] = [
            {
                "kind": "gmail",
                "query": "is:unread",
                "max_results": 3,
                "actions": [],
            }
        ]

        totals = runner.run(self.base, [specific, fallback], dry_run=True)

        self.assertEqual(totals["processed"], 1)
        self.assertEqual(totals["matched"], 1)
        self.assertEqual(totals["errors"], 1)


class ScheduleStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.routine = {"id": "r", "schedule": {"every": "1h"}}

    @staticmethod
    def epoch(local_time):
        return datetime.datetime.fromisoformat(local_time).replace(
            tzinfo=ZoneInfo("Asia/Dubai")
        ).timestamp()

    def work_hours_routine(self):
        return {
            "id": "r",
            "schedule": {
                "every": "1h",
                "work_hours": {
                    "every": "15m",
                    "days": ["sun", "mon", "tue", "wed", "thu"],
                    "start": "08:00",
                    "end": "20:00",
                    "timezone": "Asia/Dubai",
                },
            },
        }

    def test_first_tick_is_due_then_waits_for_interval(self):
        schedule = state.ScheduleStore(self.base)
        self.assertTrue(schedule.due(self.routine, now=100))
        schedule.mark_attempted({"r"}, now=100)
        self.assertFalse(schedule.due(self.routine, now=3699))
        self.assertTrue(schedule.due(self.routine, now=3700))

    def test_dry_run_never_writes_schedule_state(self):
        schedule = state.ScheduleStore(self.base, dry_run=True)
        schedule.mark_attempted({"r"}, now=100)
        self.assertFalse(state.schedule_file(self.base).exists())

    def test_independent_schedulers_merge_attempts_instead_of_clobbering(self):
        capture = state.ScheduleStore(self.base)
        maintenance = state.ScheduleStore(self.base)

        capture.mark_attempted({"capture"}, now=100)
        maintenance.mark_attempted({"maintenance"}, now=200)

        entries = state.ScheduleStore(self.base).entries
        self.assertEqual(entries["capture"]["last_attempted_epoch"], 100)
        self.assertEqual(entries["maintenance"]["last_attempted_epoch"], 200)
        self.assertEqual(
            state.schedule_file(self.base).with_name("schedule.lock").stat().st_mode
            & 0o777,
            0o600,
        )

    def test_clock_jump_backwards_does_not_freeze_routine(self):
        schedule = state.ScheduleStore(self.base)
        schedule.mark_attempted({"r"}, now=1000)
        self.assertTrue(schedule.due(self.routine, now=900))

    def test_omitted_schedule_keeps_legacy_hourly_cadence(self):
        self.assertEqual(config.schedule_seconds({"id": "legacy"}), 60 * 60)

    def test_work_hours_support_a_custom_sunday_through_thursday_week(self):
        routine = self.work_hours_routine()
        schedule = state.ScheduleStore(self.base)
        sunday_start = self.epoch("2026-08-02T08:00:00")
        schedule.mark_attempted({"r"}, now=sunday_start)

        self.assertFalse(schedule.due(routine, now=sunday_start + 899))
        self.assertTrue(schedule.due(routine, now=sunday_start + 900))

        friday_start = self.epoch("2026-07-31T08:00:00")
        schedule.mark_attempted({"r"}, now=friday_start)
        self.assertFalse(schedule.due(routine, now=friday_start + 3599))
        self.assertTrue(schedule.due(routine, now=friday_start + 3600))

    def test_entering_work_hours_can_make_a_routine_due_immediately(self):
        routine = self.work_hours_routine()
        schedule = state.ScheduleStore(self.base)
        last = self.epoch("2026-08-02T07:30:00")
        schedule.mark_attempted({"r"}, now=last)

        self.assertFalse(
            schedule.due(routine, now=self.epoch("2026-08-02T07:59:00"))
        )
        self.assertTrue(
            schedule.due(routine, now=self.epoch("2026-08-02T08:00:00"))
        )
        self.assertEqual(
            config.next_due_epoch(
                routine,
                last,
                self.epoch("2026-08-02T07:55:00"),
            ),
            self.epoch("2026-08-02T08:00:00"),
        )

    def test_leaving_work_hours_restores_the_base_interval(self):
        routine = self.work_hours_routine()
        schedule = state.ScheduleStore(self.base)
        last = self.epoch("2026-08-02T19:45:00")
        schedule.mark_attempted({"r"}, now=last)

        self.assertFalse(
            schedule.due(routine, now=self.epoch("2026-08-02T20:00:00"))
        )
        self.assertTrue(
            schedule.due(routine, now=self.epoch("2026-08-02T20:45:00"))
        )

    def test_timezone_window_tracks_daylight_saving_time(self):
        routine = self.work_hours_routine()
        routine["schedule"]["work_hours"].update({
            "days": ["mon", "tue", "wed", "thu", "fri"],
            "timezone": "America/New_York",
        })
        summer = datetime.datetime(
            2026, 7, 30, 12, 30, tzinfo=datetime.timezone.utc
        ).timestamp()
        winter = datetime.datetime(
            2026, 1, 1, 12, 30, tzinfo=datetime.timezone.utc
        ).timestamp()

        self.assertEqual(config.schedule_seconds(routine, summer), 15 * 60)
        self.assertEqual(config.schedule_seconds(routine, winter), 60 * 60)

    def test_work_hours_schedule_validation_fails_closed(self):
        invalid_values = [
            {"timezone": "Mars/Olympus"},
            {"timezone": "../UTC"},
            {"days": ["sun", "sunday"]},
            {"start": "20:00", "end": "08:00"},
            {"every": "2h"},
        ]
        for replacement in invalid_values:
            with self.subTest(replacement=replacement):
                routine = multi_routine(self.base)
                work_hours = self.work_hours_routine()["schedule"]["work_hours"]
                routine["schedule"] = {
                    "every": "1h",
                    "work_hours": {**work_hours, **replacement},
                }
                self.assertTrue(config.validate(routine))


class CursorStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_successful_checkpoint_round_trips_by_source(self):
        cursors = state.CursorStore(self.base)
        cursors.mark_successful(
            [("sweep", "gchat:all-spaces", "gchat")],
            "2026-07-28T10:00:00Z",
        )

        loaded = state.CursorStore(self.base)
        self.assertEqual(
            loaded.checkpoint("sweep", "gchat:all-spaces", "gchat"),
            "2026-07-28T10:00:00Z",
        )

    def test_mismatched_cursor_kind_fails_closed(self):
        cursors = state.CursorStore(self.base)
        cursors.mark_successful(
            [("sweep", "gchat:all-spaces", "slack")],
            "2026-07-28T10:00:00Z",
        )

        with self.assertRaisesRegex(state.StateError, "expected 'gchat'"):
            state.CursorStore(self.base).checkpoint(
                "sweep", "gchat:all-spaces", "gchat"
            )

    def test_dry_run_never_writes_cursor_state(self):
        cursors = state.CursorStore(self.base, dry_run=True)
        cursors.mark_successful_at([
            (
                "sweep", "gchat:all-spaces", "gchat",
                "2026-07-28T10:00:00Z",
            ),
        ])
        self.assertFalse(state.cursor_file(self.base).exists())

    def test_malformed_cursor_record_fails_closed(self):
        path = state.cursor_file(self.base)
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"sweep:gchat:all-spaces": {'
            '"kind": "gchat", "last_successful_scan_at": "not-a-timestamp"}}'
        )

        with self.assertRaisesRegex(state.StateError, "RFC3339"):
            state.CursorStore(self.base)


class ConnectorCoverageTest(unittest.TestCase):
    def test_bounded_source_cannot_claim_complete_coverage(self):
        problem = runner._source_coverage_problem(
            {"kind": "gmail", "max_results": 25},
            [],
        )
        self.assertIn("bounded max_results=25", problem)

    def test_ada_service_cap_cannot_claim_complete_coverage(self):
        problem = runner._source_coverage_problem(
            {"kind": "slack", "max_results": 0},
            [{
                "raw": {
                    "mode": "ada_digest",
                    "channel": "CEXAMPLE",
                    "summary": {"message_count": 100},
                },
            }],
        )
        self.assertIn("Ada 100-message cap", problem)

    def test_gchat_per_space_cap_cannot_claim_complete_coverage(self):
        problem = runner._source_coverage_problem(
            {
                "kind": "gchat",
                "all_spaces": True,
                "max_results": 0,
                "max_per_space": 25,
            },
            [],
        )
        self.assertIn("bounded max_per_space=25", problem)

    def test_uncapped_source_is_complete(self):
        self.assertIsNone(runner._source_coverage_problem(
            {"kind": "gchat", "max_results": 0},
            [],
        ))

    def test_active_slack_census_is_a_fixed_window_even_with_catch_up(self):
        self.assertEqual(
            runner._fixed_window_seconds({
                "kind": "slack",
                "active_conversations": {"hours": 48},
                "catch_up": True,
            }),
            48 * 60 * 60,
        )

    def test_consume_only_flag_preserves_existing_slack_cursor_namespaces(self):
        source = {
            "kind": "slack",
            "active_conversations": {
                "checkpoint": "state/slack-census.json",
                "hours": 48,
                "refresh_every": "1d",
                "requests_per_minute": 40,
            },
            "max_results": 0,
            "catch_up": True,
            "catch_up_after": "2026-07-28T08:00:00Z",
            "reply_roots_after": "2026-06-28T08:00:00Z",
        }
        consume_only = {
            **source,
            "active_conversations": {
                **source["active_conversations"],
                "refresh_if_stale": False,
            },
        }

        self.assertEqual(
            runner._catch_up_cursor_id(source),
            runner._catch_up_cursor_id(consume_only),
        )
        self.assertEqual(
            runner._active_conversation_cursor_id(source),
            runner._active_conversation_cursor_id(consume_only),
        )
        self.assertEqual(
            runner._coverage_cursor_id(0, source),
            runner._coverage_cursor_id(0, consume_only),
        )


class CatchUpCursorRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "state").mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.saved_source = runner.SOURCES["gchat"]
        self.saved_analyze = llm.analyze
        llm.analyze = lambda routine, prompt: "A compact summary."
        self.addCleanup(runner.SOURCES.__setitem__, "gchat", self.saved_source)
        self.addCleanup(setattr, llm, "analyze", self.saved_analyze)

    def routine(self, memory=False):
        routine = {
            "id": "sweep",
            "enabled": True,
            "source": {
                "kind": "gchat",
                "all_spaces": True,
                "hours": 26,
                "max_results": 0,
                "max_per_space": 0,
                "batch_messages": "daily",
                "batch_messages_after": "2026-07-28T06:00:00Z",
                "catch_up": True,
                "catch_up_overlap": "1h",
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep durable decisions.",
            },
        }
        if memory:
            routine["memory"] = {
                "store": str(self.base / "memory"),
                "type": "note",
            }
        else:
            routine["output"] = {
                "vault_dir": str(self.base / "vault"),
                "slug_prefix": "sweep",
            }
        return routine

    def test_success_uses_bootstrap_then_last_success_with_overlap(self):
        observed = []

        def candidates(source):
            observed.append(dict(source))
            return []

        runner.SOURCES["gchat"] = (candidates, self.saved_source[1])
        routine = self.routine()
        cursor_id = runner._catch_up_cursor_id(routine["source"])

        with mock.patch.object(
            runner, "utc_now_iso", return_value="2026-07-28T10:00:00Z",
        ):
            first = runner.run(self.base, [routine])
        with mock.patch.object(
            runner, "utc_now_iso", return_value="2026-07-28T12:00:00Z",
        ):
            second = runner.run(self.base, [routine])

        self.assertEqual(first["errors"], 0)
        self.assertEqual(second["errors"], 0)
        self.assertEqual(observed[0]["_since"], "2026-07-28T06:00:00Z")
        self.assertEqual(observed[1]["_since"], "2026-07-28T09:00:00Z")
        self.assertEqual(
            state.CursorStore(self.base).checkpoint(
                "sweep", cursor_id, "gchat"
            ),
            "2026-07-28T12:00:00Z",
        )

    def test_slack_uses_its_own_bootstrap_and_cursor_namespace(self):
        saved_slack = runner.SOURCES["slack"]
        self.addCleanup(runner.SOURCES.__setitem__, "slack", saved_slack)
        observed = []

        def candidates(source):
            observed.append(dict(source))
            return []

        runner.SOURCES["slack"] = (candidates, saved_slack[1])
        routine = {
            "id": "slack-sweep",
            "enabled": True,
            "source": {
                "kind": "slack",
                "direct_channels": ["CEXAMPLE"],
                "include_mentions": True,
                "hours": 26,
                "max_results": 0,
                "catch_up": True,
                "catch_up_overlap": "1h",
                "catch_up_after": "2026-07-28T08:00:00Z",
                "reply_roots_after": "2026-06-28T08:00:00Z",
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep durable decisions.",
            },
            "output": {
                "vault_dir": str(self.base / "slack-vault"),
                "slug_prefix": "slack",
            },
        }

        with mock.patch.object(
            runner, "utc_now_iso", return_value="2026-07-28T10:00:00Z",
        ):
            first = runner.run(self.base, [routine])
        with mock.patch.object(
            runner, "utc_now_iso", return_value="2026-07-28T12:00:00Z",
        ):
            second = runner.run(self.base, [routine])

        self.assertEqual(first["errors"], 0)
        self.assertEqual(second["errors"], 0)
        self.assertEqual(observed[0]["_since"], "2026-07-28T08:00:00Z")
        self.assertEqual(
            observed[0]["_catch_up_boundary"],
            "2026-07-28T08:00:00Z",
        )
        self.assertEqual(observed[1]["_since"], "2026-07-28T09:00:00Z")
        cursor_id = runner._catch_up_cursor_id(routine["source"])
        self.assertEqual(
            state.CursorStore(self.base).checkpoint(
                "slack-sweep", cursor_id, "slack"
            ),
            "2026-07-28T12:00:00Z",
        )

        # Expanding the configured channel set gets a new cursor namespace.
        # It must bootstrap from the declared boundary, not the old scope's
        # 12:00 checkpoint.
        expanded = {
            **routine,
            "source": {
                **routine["source"],
                "direct_channels": ["CEXAMPLE", "CNEW"],
            },
        }
        with mock.patch.object(
            runner, "utc_now_iso", return_value="2026-07-28T14:00:00Z",
        ):
            third = runner.run(self.base, [expanded])

        self.assertEqual(third["errors"], 0)
        self.assertEqual(observed[2]["_since"], "2026-07-28T08:00:00Z")
        self.assertNotEqual(
            runner._catch_up_cursor_id(routine["source"]),
            runner._catch_up_cursor_id(expanded["source"]),
        )

        # A candidate/enrichment schema upgrade also gets a fresh cursor so
        # older daily entries outside the live overlap are revisited.
        original_id = runner._catch_up_cursor_id(routine["source"])
        with mock.patch.object(
            slack_source, "CATCH_UP_SCHEMA",
            slack_source.CATCH_UP_SCHEMA + 1,
        ):
            upgraded_id = runner._catch_up_cursor_id(routine["source"])
        self.assertNotEqual(original_id, upgraded_id)

    def test_inactive_slack_owner_gets_routing_cursor_without_advancing_it(self):
        saved_slack = runner.SOURCES["slack"]
        self.addCleanup(runner.SOURCES.__setitem__, "slack", saved_slack)
        observed = {}

        def candidates(source):
            observed[source["direct_channels"][0]] = dict(source)
            return []

        runner.SOURCES["slack"] = (candidates, saved_slack[1])
        active = {
            "id": "active",
            "enabled": True,
            "source": {
                "kind": "slack",
                "direct_channels": ["CACTIVE"],
                "hours": 26,
                "max_results": 0,
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep durable decisions.",
            },
            "output": {
                "vault_dir": str(self.base / "active-vault"),
                "slug_prefix": "active",
            },
        }
        owner = {
            "id": "owner",
            "enabled": True,
            "source": {
                "kind": "slack",
                "direct_channels": ["COWNER"],
                "hours": 26,
                "max_results": 0,
                "catch_up": True,
                "catch_up_overlap": "1h",
                "catch_up_after": "2026-07-28T08:00:00Z",
                "reply_roots_after": "2026-06-28T08:00:00Z",
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep durable decisions.",
            },
            "output": {
                "vault_dir": str(self.base / "owner-vault"),
                "slug_prefix": "owner",
            },
        }

        with mock.patch.object(
            runner, "utc_now_iso", return_value="2026-07-28T10:00:00Z",
        ):
            totals = runner.run(
                self.base, [active, owner], active_ids={"active"}
            )

        self.assertEqual(totals["errors"], 0)
        self.assertEqual(
            observed["COWNER"]["_since"], "2026-07-28T08:00:00Z"
        )
        self.assertEqual(
            observed["COWNER"]["_catch_up_boundary"],
            "2026-07-28T08:00:00Z",
        )
        cursor_id = runner._catch_up_cursor_id(owner["source"])
        self.assertIsNone(
            state.CursorStore(self.base).checkpoint(
                "owner", cursor_id, "slack"
            )
        )

    def test_slack_cached_snapshot_gap_fails_and_holds_both_cursors(self):
        census_path = self.base / "state" / "census.json"

        def write_census(cutoff, until):
            census_path.write_text(json.dumps({
                "version": 1,
                "cutoff_epoch": (
                    time_utils.rfc3339_key(cutoff)[0].timestamp()
                ),
                "cutoff_at": cutoff,
                "until_epoch": (
                    time_utils.rfc3339_key(until)[0].timestamp()
                ),
                "until_at": until,
                "inventory": [],
                "next_index": 0,
                "active": [],
                "errors": [],
                "completed_at": until,
            }))

        routine = {
            "id": "slack-general",
            "enabled": True,
            "source": {
                "kind": "slack",
                "active_conversations": {
                    "checkpoint": str(census_path),
                    "hours": 48,
                    "refresh_every": "1d",
                    "requests_per_minute": 40,
                },
                "max_results": 0,
                "catch_up": True,
                "catch_up_overlap": "1h",
                "catch_up_after": "2026-07-28T08:00:00Z",
                "reply_roots_after": "2026-07-28T08:00:00Z",
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction_from_connector": "slack",
                "connector_sweep": True,
            },
            "output": {
                "vault_dir": str(self.base / "slack-vault"),
                "slug_prefix": "slack",
            },
            "memory": {
                "store": str(self.base / "memory"),
                "type": "note",
            },
        }

        with mock.patch.object(
            config, "validate", return_value=[]
        ), mock.patch.object(
            memory_sink, "mark_connector_pulled", return_value=True
        ) as mark:
            # Run A at 08:00 legally reuses a snapshot ending 23 hours earlier.
            write_census(
                "2026-07-28T09:00:00Z",
                "2026-07-30T09:00:00Z",
            )
            with mock.patch.object(
                runner, "utc_now_iso",
                return_value="2026-07-31T08:00:00Z",
            ), mock.patch.object(
                slack_source, "utc_now_iso",
                return_value="2026-07-31T08:00:00Z",
            ):
                first = runner.run(self.base, [routine])

            mark.assert_called_once_with(
                routine, "2026-07-30T09:00:00Z", dry_run=False
            )
            mark.reset_mock()

            # The next 48-hour snapshot starts 23 hours after the boundary
            # Run A actually consumed. The content cursor alone looks
            # continuous, but the discovery cursor proves it is unsafe.
            write_census(
                "2026-07-31T08:00:00Z",
                "2026-08-02T08:00:00Z",
            )
            consume_only_routine = {
                **routine,
                "source": {
                    **routine["source"],
                    "active_conversations": {
                        **routine["source"]["active_conversations"],
                        "refresh_if_stale": False,
                    },
                },
            }
            with mock.patch.object(
                runner, "utc_now_iso",
                return_value="2026-08-02T08:00:00Z",
            ), mock.patch.object(
                slack_source, "utc_now_iso",
                return_value="2026-08-02T08:00:00Z",
            ):
                second = runner.run(self.base, [consume_only_routine])

            mark.assert_not_called()

        cursor_id = runner._catch_up_cursor_id(routine["source"])
        discovery_id = runner._active_conversation_cursor_id(routine["source"])
        coverage_id = runner._coverage_cursor_id(0, routine["source"])
        cursors = state.CursorStore(self.base)
        self.assertEqual(first["errors"], 0)
        self.assertEqual(
            cursors.checkpoint("slack-general", cursor_id, "slack"),
            "2026-07-31T08:00:00Z",
        )
        self.assertEqual(
            cursors.checkpoint("slack-general", discovery_id, "slack"),
            "2026-07-30T09:00:00Z",
        )
        self.assertEqual(
            cursors.checkpoint("slack-general", coverage_id, "slack"),
            "2026-07-30T09:00:00Z",
        )

        self.assertEqual(second["errors"], 1)
        held = state.CursorStore(self.base)
        self.assertEqual(
            held.checkpoint(
                "slack-general", cursor_id, "slack"
            ),
            "2026-07-31T08:00:00Z",
        )
        self.assertEqual(
            held.checkpoint(
                "slack-general", discovery_id, "slack"
            ),
            "2026-07-30T09:00:00Z",
        )
        self.assertEqual(
            held.checkpoint(
                "slack-general", coverage_id, "slack"
            ),
            "2026-07-30T09:00:00Z",
        )

    def test_listing_failure_holds_prior_cursor(self):
        def candidates(source):
            raise RuntimeError("source unavailable")

        runner.SOURCES["gchat"] = (candidates, self.saved_source[1])
        with mock.patch.object(
            runner, "utc_now_iso", return_value="2026-07-28T10:00:00Z",
        ):
            totals = runner.run(self.base, [self.routine()])

        self.assertEqual(totals["errors"], 1)
        self.assertIsNone(
            state.CursorStore(self.base).checkpoint(
                "sweep", "gchat:all-spaces", "gchat"
            )
        )

    def test_connector_state_failure_holds_prior_cursor(self):
        def candidates(source):
            return []

        runner.SOURCES["gchat"] = (candidates, self.saved_source[1])
        routine = self.routine(memory=True)
        routine["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gchat",
            "connector_sweep": True,
        }
        with mock.patch.object(
            config, "validate", return_value=[]
        ), mock.patch.object(
            memory_sink, "mark_connector_pulled",
            side_effect=RuntimeError("state unavailable"),
        ):
            with mock.patch.object(
                runner, "utc_now_iso",
                return_value="2026-07-28T10:00:00Z",
            ):
                totals = runner.run(self.base, [routine])

        self.assertEqual(totals["errors"], 1)
        self.assertIsNone(
            state.CursorStore(self.base).checkpoint(
                "sweep", "gchat:all-spaces", "gchat"
            )
        )

    def test_successful_noop_marks_connector_pulled(self):
        def candidates(source):
            return []

        runner.SOURCES["gchat"] = (candidates, self.saved_source[1])
        routine = self.routine(memory=True)
        routine["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gchat",
            "connector_sweep": True,
        }
        with mock.patch.object(
            config, "validate", return_value=[]
        ), mock.patch.object(
            memory_sink, "mark_connector_pulled", return_value=True
        ) as mark:
            with mock.patch.object(
                runner, "utc_now_iso",
                return_value="2026-07-28T10:00:00Z",
            ):
                totals = runner.run(self.base, [routine])

        self.assertEqual(totals["errors"], 0)
        mark.assert_called_once_with(
            routine, "2026-07-28T10:00:00Z", dry_run=False
        )

    def test_connector_prompt_alone_does_not_claim_full_coverage(self):
        def candidates(source):
            return []

        runner.SOURCES["gchat"] = (candidates, self.saved_source[1])
        routine = self.routine(memory=True)
        routine["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gchat",
        }
        with mock.patch.object(
            config, "validate", return_value=[]
        ), mock.patch.object(
            memory_sink, "mark_connector_pulled"
        ) as mark, mock.patch.object(
            runner, "utc_now_iso",
            return_value="2026-07-28T10:00:00Z",
        ):
            totals = runner.run(self.base, [routine])

        self.assertEqual(totals["errors"], 0)
        mark.assert_not_called()

    def test_unrelated_targeted_source_does_not_publish_connector_health(self):
        sweep = self.routine(memory=True)
        sweep["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gchat",
            "connector_sweep": True,
        }
        local = {
            "id": "local",
            "enabled": True,
            "source": {
                "kind": "mila",
                "recordings_file": "/tmp/recordings.json",
                "max_results": 0,
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep meeting decisions.",
            },
            "memory": {
                "store": str(self.base / "memory"),
                "type": "note",
            },
        }
        saved_mila = runner.SOURCES["mila"]
        self.addCleanup(runner.SOURCES.__setitem__, "mila", saved_mila)
        runner.SOURCES["mila"] = (lambda _source: [], saved_mila[1])

        with mock.patch.object(
            memory_sink, "mark_connector_pulled"
        ) as mark:
            totals = runner.run(
                self.base, [sweep, local], active_ids={"local"}
            )

        self.assertEqual(totals["errors"], 0)
        mark.assert_not_called()

    def test_inactive_owner_holds_watermark_until_its_successful_run(self):
        def candidates(source):
            return []

        runner.SOURCES["gchat"] = (candidates, self.saved_source[1])
        sweep = self.routine(memory=True)
        sweep["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gchat",
            "connector_sweep": True,
        }
        owner = {
            "id": "domain",
            "enabled": True,
            "source": {
                "kind": "gchat",
                "spaces": ["spaces/OWNED"],
                "hours": 168,
                "max_results": 0,
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep domain decisions.",
            },
            "output": {
                "vault_dir": str(self.base / "domain-vault"),
                "slug_prefix": "domain",
            },
        }

        with mock.patch.object(
            config, "validate", return_value=[]
        ), mock.patch.object(
            memory_sink, "mark_connector_pulled", return_value=True
        ) as mark:
            with mock.patch.object(
                runner, "utc_now_iso",
                return_value="2026-07-28T10:00:00Z",
            ):
                first = runner.run(
                    self.base, [owner, sweep], active_ids={"sweep"}
                )
            mark.assert_not_called()

            with mock.patch.object(
                runner, "utc_now_iso",
                return_value="2026-07-28T12:00:00Z",
            ):
                second = runner.run(
                    self.base, [owner, sweep], active_ids={"domain"}
                )

        self.assertEqual(first["errors"], 0)
        self.assertEqual(second["errors"], 0)
        mark.assert_called_once_with(
            sweep, "2026-07-28T10:00:00Z", dry_run=False
        )

    def test_fixed_window_owner_gap_holds_connector_watermark(self):
        def candidates(source):
            return []

        runner.SOURCES["gchat"] = (candidates, self.saved_source[1])
        sweep = self.routine(memory=True)
        sweep["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gchat",
            "connector_sweep": True,
        }
        owner = {
            "id": "domain",
            "enabled": True,
            "source": {
                "kind": "gchat",
                "spaces": ["spaces/OWNED"],
                "hours": 168,
                "max_results": 0,
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep domain decisions.",
            },
            "output": {
                "vault_dir": str(self.base / "domain-vault"),
                "slug_prefix": "domain",
            },
        }

        with mock.patch.object(
            config, "validate", return_value=[]
        ), mock.patch.object(
            memory_sink, "mark_connector_pulled", return_value=True
        ) as mark:
            with mock.patch.object(
                runner, "utc_now_iso",
                return_value="2026-07-28T10:00:00Z",
            ):
                seeded = runner.run(self.base, [owner, sweep])
            self.assertEqual(seeded["errors"], 0)
            mark.assert_called_once()
            mark.reset_mock()

            with mock.patch.object(
                runner, "utc_now_iso",
                return_value="2026-08-10T10:00:00Z",
            ):
                after_gap = runner.run(
                    self.base, [owner, sweep], active_ids={"domain"}
                )

        self.assertEqual(after_gap["errors"], 0)
        mark.assert_not_called()

    def test_disabled_owner_prevents_connector_wide_checkpoint(self):
        def candidates(source):
            return []

        runner.SOURCES["gchat"] = (candidates, self.saved_source[1])
        sweep = self.routine(memory=True)
        sweep["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gchat",
            "connector_sweep": True,
        }
        disabled = {
            "id": "parked-domain",
            "enabled": False,
            "source": {
                "kind": "gchat",
                "spaces": ["spaces/PARKED"],
                "max_results": 0,
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep domain decisions.",
            },
            "output": {
                "vault_dir": str(self.base / "parked-vault"),
                "slug_prefix": "parked",
            },
        }
        with mock.patch.object(
            config, "validate", return_value=[]
        ), mock.patch.object(
            memory_sink, "mark_connector_pulled"
        ) as mark, mock.patch.object(
            runner, "utc_now_iso",
            return_value="2026-07-28T10:00:00Z",
        ):
            totals = runner.run(self.base, [disabled, sweep])

        self.assertEqual(totals["errors"], 0)
        mark.assert_not_called()

    def test_duplicate_connector_sweep_publishers_fail_closed(self):
        def candidates(source):
            return []

        runner.SOURCES["gchat"] = (candidates, self.saved_source[1])
        first = self.routine(memory=True)
        first["analyze"] = {
            "provider": "gemini",
            "model": "m",
            "instruction_from_connector": "gchat",
            "connector_sweep": True,
        }
        second = {
            **first,
            "id": "second-sweep",
            "source": dict(first["source"]),
            "analyze": dict(first["analyze"]),
            "memory": dict(first["memory"]),
        }
        with mock.patch.object(
            config, "validate", return_value=[]
        ), mock.patch.object(
            memory_sink, "mark_connector_pulled"
        ) as mark, mock.patch.object(
            runner, "utc_now_iso",
            return_value="2026-07-28T10:00:00Z",
        ):
            totals = runner.run(self.base, [first, second])

        self.assertEqual(totals["errors"], 1)
        mark.assert_not_called()

    def test_memory_error_is_retried_before_cursor_advances(self):
        candidate = {
            "id": "gchat:EXAMPLE:daily:2026-07-28@2026-07-28T09:00:00Z",
            "title": "durable update",
            "raw": {"source_id": "gchat:EXAMPLE:daily:2026-07-28"},
        }

        def candidates(source):
            return [candidate]

        def fetch(routine, source, value):
            return {
                "id": value["id"],
                "source_id": value["raw"]["source_id"],
                "source_kind": "gchat",
                "title": value["title"],
                "date": "2026-07-28",
                "body": "Complete daily context.",
                "frontmatter": {},
            }

        runner.SOURCES["gchat"] = (candidates, fetch)
        routine = self.routine(memory=True)
        cursor_id = runner._catch_up_cursor_id(routine["source"])
        # Exceptions are allowed to have no message. Presence of the ledger key,
        # not the truthiness of its text, must trigger the retry.
        outcomes = [RuntimeError(), {"memory": "created"}]

        with mock.patch.object(memory_sink, "capture", side_effect=outcomes) as capture:
            with mock.patch.object(
                runner, "utc_now_iso", return_value="2026-07-28T10:00:00Z",
            ):
                first = runner.run(self.base, [routine])
            self.assertEqual(first["errors"], 1)
            self.assertIsNone(
                state.CursorStore(self.base).checkpoint(
                    "sweep", cursor_id, "gchat"
                )
            )

            with mock.patch.object(
                runner, "utc_now_iso", return_value="2026-07-28T12:00:00Z",
            ):
                second = runner.run(self.base, [routine])

        self.assertEqual(second["errors"], 0)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(
            state.CursorStore(self.base).checkpoint(
                "sweep", cursor_id, "gchat"
            ),
            "2026-07-28T12:00:00Z",
        )
        record = state.load(self.base)[candidate["id"]]
        self.assertNotIn("memory_error", record)

    def test_successful_candidate_is_not_reprocessed_on_overlap(self):
        candidate = {
            "id": "gchat:EXAMPLE:daily:2026-07-28@2026-07-28T09:00:00Z",
            "title": "durable update",
            "raw": {"source_id": "gchat:EXAMPLE:daily:2026-07-28"},
        }

        def candidates(source):
            return [candidate]

        def fetch(routine, source, value):
            return {
                "id": value["id"],
                "source_id": value["raw"]["source_id"],
                "source_kind": "gchat",
                "title": value["title"],
                "date": "2026-07-28",
                "body": "Complete daily context.",
                "frontmatter": {},
            }

        runner.SOURCES["gchat"] = (candidates, fetch)
        with mock.patch.object(
            memory_sink, "capture", return_value={"memory": "created"},
        ) as capture:
            with mock.patch.object(
                runner, "utc_now_iso", return_value="2026-07-28T10:00:00Z",
            ):
                first = runner.run(self.base, [self.routine(memory=True)])
            with mock.patch.object(
                runner, "utc_now_iso", return_value="2026-07-28T12:00:00Z",
            ):
                second = runner.run(self.base, [self.routine(memory=True)])

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(capture.call_count, 1)


class RunCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.routine = multi_routine(self.base / "vault")
        self.routine["enabled"] = False

    def test_include_disabled_runs_only_the_named_parked_routine(self):
        args = SimpleNamespace(
            routine="domain",
            include_disabled=True,
            dry_run=True,
            refresh_labels=False,
        )
        totals = {
            "processed": 1, "skipped": 0, "errors": 0,
            "matched": 1, "fallbacks": 0, "pending_actions": 0,
            "ambiguous": 0,
        }
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(
                 daemon_cli.config, "discover", return_value=[self.routine]
             ), \
             mock.patch.object(
                 daemon_cli.config, "validate", return_value=[]
             ), \
             mock.patch.object(
                 daemon_cli.runner, "run", return_value=totals
             ) as run:
            self.assertEqual(daemon_cli.cmd_run(args), 0)

        selected = run.call_args.args[1]
        self.assertTrue(selected[0]["enabled"])
        self.assertFalse(self.routine["enabled"])
        self.assertEqual(run.call_args.kwargs["active_ids"], {"domain"})
        self.assertTrue(run.call_args.kwargs["dry_run"])

    def test_include_disabled_requires_one_named_routine(self):
        args = SimpleNamespace(
            routine=None,
            include_disabled=True,
            dry_run=True,
            refresh_labels=False,
        )
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(
                 daemon_cli.config, "discover", return_value=[self.routine]
             ):
            with self.assertRaisesRegex(
                config.RoutineError, "requires --routine"
            ):
                daemon_cli.cmd_run(args)

    def test_manual_all_uses_the_scheduler_lock_for_slack_census(self):
        self.routine["enabled"] = True
        census = {
            "id": "census",
            "enabled": True,
            "role": "maintenance",
            "schedule": {"every": "1d"},
            "maintenance": {
                "kind": "slack_conversation_census",
                "checkpoint": "state/census.json",
            },
        }
        args = SimpleNamespace(
            routine=None,
            include_disabled=False,
            dry_run=False,
            refresh_labels=False,
        )
        totals = {
            "processed": 0, "skipped": 0, "errors": 0,
            "matched": 0, "fallbacks": 0, "pending_actions": 0,
            "ambiguous": 0,
        }
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(daemon_cli.config, "secure_routine_files"), \
             mock.patch.object(
                 daemon_cli.config, "discover", return_value=[self.routine, census]
             ), \
             mock.patch.object(daemon_cli.config, "validate", return_value=[]), \
             mock.patch.object(
                 daemon_cli.runner, "run", return_value=totals
             ) as run:
            self.assertEqual(daemon_cli.cmd_run(args), 0)

        self.assertEqual(run.call_count, 2)
        calls = {
            frozenset(call.kwargs["active_ids"]): call.kwargs["lock_name"]
            for call in run.call_args_list
        }
        self.assertEqual(calls[frozenset({"domain"})], "run")
        self.assertEqual(calls[frozenset({"census"})], "slack-census")

    def test_manual_named_slack_census_uses_its_scheduler_lock(self):
        census = {
            "id": "census",
            "enabled": True,
            "role": "maintenance",
            "schedule": {"every": "1d"},
            "maintenance": {
                "kind": "slack_conversation_census",
                "checkpoint": "state/census.json",
            },
        }
        args = SimpleNamespace(
            routine="census",
            include_disabled=False,
            dry_run=False,
            refresh_labels=False,
        )
        totals = {
            "processed": 0, "skipped": 0, "errors": 0,
            "matched": 0, "fallbacks": 0, "pending_actions": 0,
            "ambiguous": 0,
        }
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(daemon_cli.config, "secure_routine_files"), \
             mock.patch.object(
                 daemon_cli.config, "discover", return_value=[census]
             ), \
             mock.patch.object(daemon_cli.config, "validate", return_value=[]), \
             mock.patch.object(
                 daemon_cli.runner, "run", return_value=totals
             ) as run:
            self.assertEqual(daemon_cli.cmd_run(args), 0)

        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["active_ids"], {"census"})
        self.assertEqual(run.call_args.kwargs["lock_name"], "slack-census")


class TickCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.routine = multi_routine(self.base / "vault")
        self.args = SimpleNamespace(dry_run=False, refresh_labels=False)

    def test_tick_runs_due_ids_and_persists_attempt(self):
        totals = {
            "processed": 0, "skipped": 0, "errors": 0,
            "matched": 0, "fallbacks": 0, "pending_actions": 0,
            "ambiguous": 0,
        }
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(
                 daemon_cli.uuid, "uuid4",
                 return_value=SimpleNamespace(hex="abc123456789ffff"),
             ), \
             mock.patch.object(daemon_cli.config, "discover", return_value=[self.routine]), \
             mock.patch.object(daemon_cli.runner, "run", return_value=totals) as run, \
             mock.patch.object(daemon_cli, "log") as log:
            self.assertEqual(daemon_cli.cmd_tick(self.args), 0)
        self.assertEqual(run.call_args.kwargs["active_ids"], {"domain"})
        self.assertIn("domain", state.ScheduleStore(self.base).entries)
        self.assertEqual(
            log.call_args_list,
            [
                mock.call("tick[abc123456789]: due=domain"),
                mock.call(
                    "tick[abc123456789] done: 0 processed, "
                    "0 already-seen, 0 error(s)"
                ),
            ],
        )

    def test_grouped_tick_identifies_its_scheduler_stream(self):
        args = SimpleNamespace(
            dry_run=False,
            refresh_labels=False,
            group="capture",
        )
        totals = {
            "processed": 0, "skipped": 0, "errors": 0,
            "matched": 0, "fallbacks": 0, "pending_actions": 0,
            "ambiguous": 0,
        }
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(
                 daemon_cli.uuid, "uuid4",
                 return_value=SimpleNamespace(hex="capture12345ffff"),
             ), \
             mock.patch.object(
                 daemon_cli.config, "discover", return_value=[self.routine]
             ), \
             mock.patch.object(
                 daemon_cli.runner, "run", return_value=totals
             ), \
             mock.patch.object(daemon_cli, "log") as log:
            self.assertEqual(daemon_cli.cmd_tick(args), 0)

        self.assertEqual(
            log.call_args_list,
            [
                mock.call("tick[capture12345](capture): due=domain"),
                mock.call(
                    "tick[capture12345](capture) done: 0 processed, "
                    "0 already-seen, 0 error(s)"
                ),
            ],
        )

    def test_tick_runs_capture_before_maintenance_and_marks_it_immediately(self):
        maintenance_routine = {
            "id": "census",
            "enabled": True,
            "role": "maintenance",
            "schedule": {"every": "1d"},
            "maintenance": {
                "kind": "slack_conversation_census",
                "checkpoint": "state/census.json",
            },
        }
        totals = {
            "processed": 0, "skipped": 0, "errors": 0,
            "matched": 0, "fallbacks": 0, "pending_actions": 0,
            "ambiguous": 0,
        }
        order = []

        def run(_base, _routines, **kwargs):
            active = kwargs["active_ids"]
            order.append(active)
            if active == {"census"}:
                self.assertIn("domain", state.ScheduleStore(self.base).entries)
            return totals

        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(
                 daemon_cli, "current_epoch", side_effect=[1000.0, 2000.0]
             ), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(daemon_cli.config, "secure_routine_files"), \
             mock.patch.object(
                 daemon_cli.config, "discover",
                 return_value=[self.routine, maintenance_routine],
             ), mock.patch.object(daemon_cli.config, "validate", return_value=[]), \
             mock.patch.object(daemon_cli.runner, "run", side_effect=run), \
             mock.patch.object(daemon_cli, "log"):
            self.assertEqual(daemon_cli.cmd_tick(self.args), 0)

        self.assertEqual(order, [{"domain"}, {"census"}])
        self.assertEqual(
            set(state.ScheduleStore(self.base).entries), {"domain", "census"}
        )
        attempts = state.ScheduleStore(self.base).entries
        self.assertEqual(attempts["domain"]["last_attempted_epoch"], 1000.0)
        self.assertEqual(attempts["census"]["last_attempted_epoch"], 2000.0)

    def test_second_tick_inside_interval_does_nothing(self):
        state.ScheduleStore(self.base).mark_attempted({"domain"})
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(daemon_cli.config, "discover", return_value=[self.routine]), \
             mock.patch.object(daemon_cli.runner, "run") as run:
            self.assertEqual(daemon_cli.cmd_tick(self.args), 0)
        run.assert_not_called()

    def test_dry_run_noop_is_identified_in_the_operational_log(self):
        state.ScheduleStore(self.base).mark_attempted({"domain"})
        args = SimpleNamespace(dry_run=True, refresh_labels=False)
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(
                 daemon_cli.uuid, "uuid4",
                 return_value=SimpleNamespace(hex="dry123456789ffff"),
             ), \
             mock.patch.object(
                 daemon_cli.config, "discover", return_value=[self.routine]
             ), \
             mock.patch.object(daemon_cli, "log") as log:
            self.assertEqual(daemon_cli.cmd_tick(args), 0)
        log.assert_called_once_with(
            "tick[dry123456789]: no routines due (dry-run)"
        )


if __name__ == "__main__":
    unittest.main()
