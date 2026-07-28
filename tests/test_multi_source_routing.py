"""Multi-source routine, ownership, and cadence regression tests."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import daemon as daemon_cli
from workspace_daemon import actions, config, llm, runner, state


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
             mock.patch.object(daemon_cli.config, "discover", return_value=[self.routine]), \
             mock.patch.object(daemon_cli.runner, "run", return_value=totals) as run:
            self.assertEqual(daemon_cli.cmd_tick(self.args), 0)
        self.assertEqual(run.call_args.kwargs["active_ids"], {"domain"})
        self.assertIn("domain", state.ScheduleStore(self.base).entries)

    def test_second_tick_inside_interval_does_nothing(self):
        state.ScheduleStore(self.base).mark_attempted({"domain"})
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "LOG_FILE", self.base / "run.log"), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(daemon_cli.config, "discover", return_value=[self.routine]), \
             mock.patch.object(daemon_cli.runner, "run") as run:
            self.assertEqual(daemon_cli.cmd_tick(self.args), 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
