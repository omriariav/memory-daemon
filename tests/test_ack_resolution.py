"""Terse Chat acknowledgements resolve prior captures instead of vanishing.

Covers the pipeline pieces that let a "done" reply (plus its reaction) close a
previously captured request: the related-memory store scan, the runner's
prompt enrichment, the prompt-header rendering, and the status telemetry that
distinguishes "seen but judged non-durable" from "never fetched".
"""
import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import llm, memory_sink, runner, state, status


def write_entry(store, entry_id, source_ids, date, etype="todo",
                title="Provide bullets"):
    entries = Path(store) / "memory" / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    sources = "\n".join(f"  - {value}" for value in source_ids)
    (entries / f"{entry_id}.md").write_text(
        f"---\nid: {entry_id}\ntype: {etype}\ntitle: {title}\n"
        f"date: {date}\nsource_ids:\n{sources}\n---\nbody\n",
        encoding="utf-8",
    )


class RecentEntriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = self.tmp.name
        self.today = datetime.date.today()

    def test_matches_prefix_and_excludes_current_source(self):
        recent = (self.today - datetime.timedelta(days=1)).isoformat()
        write_entry(self.store, "request-entry",
                    [f"gchat:AAA:daily:{recent}"], recent)
        write_entry(self.store, "other-space",
                    [f"gchat:BBB:daily:{recent}"], recent)
        write_entry(self.store, "current-digest",
                    [f"gchat:AAA:daily:{self.today.isoformat()}"],
                    self.today.isoformat())

        out = memory_sink.recent_entries_for_prefix(
            self.store, "gchat:AAA:",
            exclude_source_id=f"gchat:AAA:daily:{self.today.isoformat()}",
        )

        self.assertEqual(
            [entry["id"] for entry in out], ["request-entry"]
        )
        self.assertEqual(out[0]["type"], "todo")
        self.assertEqual(out[0]["title"], "Provide bullets")

    def test_old_entries_fall_outside_the_context_window(self):
        stale = (self.today - datetime.timedelta(days=30)).isoformat()
        write_entry(self.store, "stale-entry",
                    [f"gchat:AAA:daily:{stale}"], stale)

        self.assertEqual(
            memory_sink.recent_entries_for_prefix(self.store, "gchat:AAA:"),
            [],
        )

    def test_missing_store_returns_empty(self):
        self.assertEqual(
            memory_sink.recent_entries_for_prefix(
                f"{self.store}/nope", "gchat:AAA:"
            ),
            [],
        )

    def test_result_is_capped_and_newest_first(self):
        for index in range(memory_sink.RELATED_CONTEXT_LIMIT + 3):
            day = (self.today - datetime.timedelta(days=index)).isoformat()
            write_entry(self.store, f"entry-{index}",
                        [f"gchat:AAA:daily:{day}"], day)

        out = memory_sink.recent_entries_for_prefix(self.store, "gchat:AAA:")

        self.assertEqual(len(out), memory_sink.RELATED_CONTEXT_LIMIT)
        self.assertEqual(out[0]["id"], "entry-0")


class HeaderRenderingTest(unittest.TestCase):
    def test_related_memories_render_with_ack_guidance(self):
        item = {
            "source_kind": "gchat",
            "frontmatter": {
                "gchat_space": "spaces/AAA",
                "related_memory_entries": [{
                    "id": "2026-08-03-provide-bullets",
                    "type": "todo",
                    "date": "2026-08-03",
                    "title": "Provide privacy bullets",
                }],
            },
        }
        lines = "\n".join(llm.source_header_lines(item))
        self.assertIn("Durable memories already captured", lines)
        self.assertIn(
            "- 2026-08-03-provide-bullets (todo, 2026-08-03): "
            "Provide privacy bullets",
            lines,
        )
        self.assertIn("terse acknowledgement", lines)

    def test_reaction_count_is_surfaced_as_a_signal(self):
        item = {
            "source_kind": "gchat",
            "frontmatter": {
                "gchat_space": "spaces/AAA",
                "reaction_count": 1,
            },
        }
        lines = "\n".join(llm.source_header_lines(item))
        self.assertIn("Emoji reactions on supplied messages: 1", lines)

    def test_no_related_entries_renders_nothing_extra(self):
        item = {"source_kind": "gchat",
                "frontmatter": {"gchat_space": "spaces/AAA"}}
        lines = "\n".join(llm.source_header_lines(item))
        self.assertNotIn("Durable memories", lines)


class AttachRelatedMemoriesTest(unittest.TestCase):
    ROUTINE = {"id": "r", "memory": {"store": "/store", "type": "note"}}

    def test_gchat_item_gains_related_entries(self):
        related = [{"id": "e1", "type": "todo", "date": "2026-08-03",
                    "title": "T"}]
        item = {
            "id": "gchat:AAA:daily:2026-08-04@v",
            "source_id": "gchat:AAA:daily:2026-08-04",
            "frontmatter": {"gchat_space": "spaces/AAA"},
        }
        with mock.patch.object(
            memory_sink, "recent_entries_for_prefix", return_value=related
        ) as lookup, mock.patch.object(runner, "log"):
            runner._attach_related_memories(
                self.ROUTINE, {"kind": "gchat"}, item
            )
        lookup.assert_called_once_with(
            "/store", "gchat:AAA:",
            exclude_source_id="gchat:AAA:daily:2026-08-04",
        )
        self.assertEqual(
            item["frontmatter"]["related_memory_entries"], related
        )

    def test_non_gchat_sources_are_untouched(self):
        item = {"frontmatter": {}}
        with mock.patch.object(
            memory_sink, "recent_entries_for_prefix"
        ) as lookup:
            runner._attach_related_memories(
                self.ROUTINE, {"kind": "gmail"}, item
            )
        lookup.assert_not_called()
        self.assertNotIn("related_memory_entries", item["frontmatter"])

    def test_lookup_failure_is_enrichment_only(self):
        item = {
            "id": "gchat:AAA:daily:2026-08-04@v",
            "frontmatter": {"gchat_space": "spaces/AAA"},
        }
        with mock.patch.object(
            memory_sink, "recent_entries_for_prefix",
            side_effect=RuntimeError("store offline"),
        ), mock.patch.object(runner, "log") as log:
            runner._attach_related_memories(
                self.ROUTINE, {"kind": "gchat"}, item
            )
        self.assertNotIn("related_memory_entries", item["frontmatter"])
        self.assertIn("related-memory lookup failed", log.call_args.args[0])


class SkipTelemetryTest(unittest.TestCase):
    def test_status_counts_seen_but_not_worthy_items(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state.save(tmp.name, {
            "gchat:AAA:daily:2026-08-04@v": {
                "rule_id": "sweep",
                "processed_at": "2026-08-04T07:35:56Z",
                "memory": "skipped_not_worthy",
            },
            "gchat:AAA:daily:2026-08-03@v": {
                "rule_id": "sweep",
                "processed_at": "2026-08-03T13:45:00Z",
                "memory": "created",
            },
        })
        routines = [{
            "id": "sweep",
            "enabled": True,
            "schedule": {"every": "1h"},
            "source": {"kind": "gchat", "all_spaces": True},
        }]
        rows = status.routine_rows(
            tmp.name, routines, {"routines": {}}, now=5000
        )
        self.assertEqual(rows[0]["skipped_not_worthy"], "1")
        # Judged-non-durable is an outcome, not a fault.
        self.assertEqual(rows[0]["issues"], "-")


if __name__ == "__main__":
    unittest.main()
