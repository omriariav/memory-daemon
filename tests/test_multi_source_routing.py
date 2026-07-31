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
                "sweep", "gchat:all-spaces", "gchat"
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
                "instruction": "Keep durable decisions.",
            },
            "output": {
                "vault_dir": str(self.base / "slack-vault"),
                "slug_prefix": "slack",
            },
        }

        # Run A at 08:00 legally reuses a snapshot ending 23 hours earlier.
        write_census(
            "2026-07-28T09:00:00Z",
            "2026-07-30T09:00:00Z",
        )
        with mock.patch.object(
            runner, "utc_now_iso", return_value="2026-07-31T08:00:00Z",
        ), mock.patch.object(
            slack_source, "utc_now_iso",
            return_value="2026-07-31T08:00:00Z",
        ):
            first = runner.run(self.base, [routine])

        cursor_id = runner._catch_up_cursor_id(routine["source"])
        discovery_id = runner._active_conversation_cursor_id(routine["source"])
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

        # The next 48-hour snapshot starts 23 hours after the boundary that
        # Run A actually consumed. The content cursor alone looks continuous,
        # but the durable discovery cursor proves this snapshot is unsafe.
        write_census(
            "2026-07-31T08:00:00Z",
            "2026-08-02T08:00:00Z",
        )
        with mock.patch.object(
            runner, "utc_now_iso", return_value="2026-08-02T08:00:00Z",
        ), mock.patch.object(
            slack_source, "utc_now_iso",
            return_value="2026-08-02T08:00:00Z",
        ):
            second = runner.run(self.base, [routine])

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
        # Exceptions are allowed to have no message. Presence of the ledger key,
        # not the truthiness of its text, must trigger the retry.
        outcomes = [RuntimeError(), {"memory": "created"}]

        with mock.patch.object(memory_sink, "capture", side_effect=outcomes) as capture:
            with mock.patch.object(
                runner, "utc_now_iso", return_value="2026-07-28T10:00:00Z",
            ):
                first = runner.run(self.base, [self.routine(memory=True)])
            self.assertEqual(first["errors"], 1)
            self.assertIsNone(
                state.CursorStore(self.base).checkpoint(
                    "sweep", "gchat:all-spaces", "gchat"
                )
            )

            with mock.patch.object(
                runner, "utc_now_iso", return_value="2026-07-28T12:00:00Z",
            ):
                second = runner.run(self.base, [self.routine(memory=True)])

        self.assertEqual(second["errors"], 0)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(
            state.CursorStore(self.base).checkpoint(
                "sweep", "gchat:all-spaces", "gchat"
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
