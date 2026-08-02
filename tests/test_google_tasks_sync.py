"""Bidirectional Google Tasks sync behavior."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from workspace_daemon import google_tasks_sync


LIST_ID = "list-example"
TASK_ID = "task-example"


def task(title="Google task", status="needsAction", **extra):
    return {
        "id": TASK_ID,
        "tasklist_id": LIST_ID,
        "title": title,
        "notes": "Concrete task notes.",
        "status": status,
        "updated": "2026-08-02T08:00:00.000Z",
        **extra,
    }


def entry_text(
    entry_id="2026-08-02-local-task",
    title="Local task",
    body="Do the local work.",
    date="2026-08-02",
    entry_type="todo",
    tags=None,
    source_ids=None,
    follows=None,
):
    source_ids = source_ids or []
    follows = follows or []
    tags = tags or ["work"]
    lines = [
        "---",
        f"id: {entry_id}",
        f"date: '{date}'",
        f"type: {entry_type}",
        f"title: {json.dumps(title)}",
        "people: []",
        "teams: []",
        f"tags: {json.dumps(tags)}",
    ]
    if source_ids:
        lines.append("source_ids:")
        lines.extend(f"  - '{value}'" for value in source_ids)
    if follows:
        lines.append("follows:")
        lines.extend(f"  - {value}" for value in follows)
    lines.extend(["---", "", body, ""])
    return "\n".join(lines)


class GoogleTasksSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "store"
        self.entries = self.store / "memory" / "entries" / "2026" / "08"
        self.entries.mkdir(parents=True)
        self.checkpoint = Path(self.tmp.name) / "sync.json"
        self.cfg = {
            "store": str(self.store),
            "tasklists": "all",
            "outbound_tasklist": LIST_ID,
            "outbound_since": "2026-08-02",
            "exclude_tags": ["no-google-tasks"],
        }
        self.tasklists = {
            LIST_ID: {"id": LIST_ID, "title": "Incoming"},
        }

    def write_entry(self, text, name="entry.md"):
        (self.entries / name).write_text(text)

    def run_dry(self, tasks):
        checkpoint_before = (
            self.checkpoint.read_text() if self.checkpoint.exists() else None
        )
        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync, "_open_tasks", return_value=tasks
        ), mock.patch.object(
            google_tasks_sync, "_memory_cli"
        ) as memory_cli, mock.patch.object(
            google_tasks_sync, "_gws"
        ) as gws:
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=True,
            )
        memory_cli.assert_not_called()
        gws.assert_not_called()
        if checkpoint_before is None:
            self.assertFalse(self.checkpoint.exists())
        else:
            self.assertEqual(self.checkpoint.read_text(), checkpoint_before)
        return report

    def test_dry_run_plans_new_google_import_and_new_memory_export(self):
        self.write_entry(entry_text())
        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": task()})

        self.assertEqual(report["create_memory"], 1)
        self.assertEqual(report["create_google"], 1)
        self.assertEqual(report["conflicts"], 0)
        import_plan = next(
            row for row in report["planned"]
            if row["action"] == "create_memory"
        )
        self.assertEqual(import_plan["memory_date"], "2026-08-02")
        self.assertEqual(
            import_plan["source_updated"],
            "2026-08-02T08:00:00.000Z",
        )
        self.assertTrue(report["ok"])

    def test_exact_title_links_instead_of_duplicating(self):
        self.write_entry(entry_text(
            title="Google task",
            body="Concrete task notes.",
        ))
        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": task()})

        self.assertEqual(report["link_memory"], 1)
        self.assertNotIn("create_memory", report)
        self.assertNotIn("create_google", report)
        self.assertEqual(report["planned"][0]["memory_date"], "2026-08-02")
        self.assertEqual(
            report["planned"][0]["source_updated"],
            "2026-08-02T08:00:00.000Z",
        )

    def test_exact_title_with_different_content_is_a_conflict(self):
        self.write_entry(entry_text(title="Google task"))
        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": task()})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("different content", report["planned"][0]["reason"])
        self.assertFalse(report["ok"])

    def test_two_google_tasks_never_claim_the_same_exact_title_memory_todo(self):
        self.write_entry(entry_text(
            title="Google task",
            body="Concrete task notes.",
        ))
        second_id = "task-second"
        report = self.run_dry({
            f"{LIST_ID}:{TASK_ID}": task(),
            f"{LIST_ID}:{second_id}": task(id=second_id),
        })

        self.assertEqual(report["link_memory"], 1)
        self.assertEqual(report["create_memory"], 1)
        linked = [
            row for row in report["planned"]
            if row["action"] == "link_memory"
        ]
        self.assertEqual(linked[0]["memory_id"], "2026-08-02-local-task")
        self.assertEqual(report["conflicts"], 0)
        self.assertTrue(report["ok"])

    def test_excluded_todo_is_never_claimed_by_exact_title_matching(self):
        self.write_entry(entry_text(
            title="Google task",
            body="Concrete task notes.",
            tags=["work", "no-google-tasks"],
        ))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": task()})

        self.assertNotIn("link_memory", report)
        self.assertEqual(report["create_memory"], 1)
        self.assertNotIn("create_google", report)
        self.assertEqual(report["conflicts"], 0)
        self.assertTrue(report["ok"])

    def test_source_linked_non_todo_is_a_conflict_not_reclassified(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        self.write_entry(entry_text(
            title="Google task",
            body="Concrete task notes.",
            entry_type="note",
            source_ids=[source_id],
        ))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": task()})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("not a todo", report["planned"][0]["reason"])
        self.assertNotIn("update_memory", report)
        self.assertFalse(report["ok"])

    def test_outbound_since_prevents_historical_flood(self):
        self.write_entry(entry_text(date="2026-08-01"))
        report = self.run_dry({})

        self.assertNotIn("create_google", report)
        self.assertEqual(report["planned"], [])

    def test_google_completion_plans_following_memory_entry(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        open_task = task()
        self.write_entry(entry_text(
            title="Google task",
            body=google_tasks_sync._memory_body(open_task, "Incoming"),
            source_ids=[source_id],
        ))
        memory_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": memory_entry["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(open_task),
                    "memory_hash": google_tasks_sync._memory_hash(memory_entry),
                    "terminal": False,
                },
            },
        }))
        completed = task(
            status="completed",
            completed="2026-08-02T09:00:00.000Z",
            updated="2026-08-02T09:00:00.000Z",
        )
        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": completed})

        self.assertEqual(report["complete_memory"], 1)
        self.assertTrue(report["ok"])

    def test_google_update_dry_plan_shows_source_and_preserved_memory_dates(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        baseline_task = task(notes="Old notes.")
        self.write_entry(entry_text(
            title=baseline_task["title"],
            body=google_tasks_sync._memory_body(baseline_task, "Incoming"),
            source_ids=[source_id],
        ))
        entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": entry["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(baseline_task),
                    "memory_hash": google_tasks_sync._memory_hash(entry),
                    "terminal": False,
                },
            },
        }))
        changed = task(
            notes="New Google notes.",
            updated="2026-08-03T09:30:00.000Z",
        )

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": changed})

        self.assertEqual(report["update_memory"], 1)
        plan = report["planned"][0]
        self.assertEqual(plan["source_updated"], "2026-08-03T09:30:00.000Z")
        self.assertEqual(plan["memory_date"], "2026-08-02")
        self.assertTrue(report["ok"])

    def test_both_sides_changed_is_fail_closed_conflict(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        self.write_entry(entry_text(title="Locally changed", source_ids=[source_id]))
        memory_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": memory_entry["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(task(title="Old")),
                    "memory_hash": "old-memory-hash",
                    "terminal": False,
                },
            },
        }))
        report = self.run_dry({
            f"{LIST_ID}:{TASK_ID}": task(title="Google changed"),
        })

        self.assertEqual(report["conflicts"], 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["planned"][0]["action"], "conflict")

    def test_both_hashes_changed_but_aligned_recovers_interrupted_checkpoint(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        current_task = task(title="Aligned task", due="2026-08-10T00:00:00.000Z")
        body = google_tasks_sync._memory_body(current_task, "Incoming")
        self.write_entry(entry_text(
            title="Aligned task",
            body=body,
            source_ids=[source_id],
        ))
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": "2026-08-02-local-task",
                    "source_id": source_id,
                    "google_hash": "pre-write-google-hash",
                    "memory_hash": "pre-write-memory-hash",
                    "terminal": False,
                },
            },
        }))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": current_task})

        self.assertEqual(report["recover_checkpoint"], 1)
        self.assertEqual(report["conflicts"], 0)
        self.assertTrue(report["ok"])

    def test_checkpoint_recovery_always_commits_prior_durable_memory_write(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        current_task = task(title="Aligned task")
        body = google_tasks_sync._memory_body(current_task, "Incoming")
        self.write_entry(entry_text(
            title="Aligned task",
            body=body,
            source_ids=[source_id],
        ))
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": "2026-08-02-local-task",
                    "source_id": source_id,
                    "google_hash": "pre-write-google-hash",
                    "memory_hash": "pre-write-memory-hash",
                    "terminal": False,
                },
            },
        }))

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": current_task},
        ), mock.patch.object(
            google_tasks_sync, "_commit_store"
        ) as commit:
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=False,
            )

        self.assertEqual(report["recover_checkpoint"], 1)
        commit.assert_called_once_with(
            str(self.store),
            "memory: sync Google Tasks",
        )
        saved = json.loads(self.checkpoint.read_text())
        self.assertNotEqual(
            saved["mappings"][f"{LIST_ID}:{TASK_ID}"]["google_hash"],
            "pre-write-google-hash",
        )

    def test_removing_existing_google_due_date_fails_closed(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        self.write_entry(entry_text(title="Google task", source_ids=[source_id]))
        current_task = task(due="2026-08-10T00:00:00.000Z")
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": "2026-08-02-local-task",
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(current_task),
                    "memory_hash": "before-local-edit",
                    "terminal": False,
                },
            },
        }))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": current_task})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("clearing", report["planned"][0]["reason"])
        self.assertFalse(report["ok"])

    def test_terminal_mapping_does_not_refetch_completed_task_each_run(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        self.write_entry(entry_text(title="Google task", source_ids=[source_id]))
        self.write_entry(entry_text(
            entry_id="2026-08-02-completed-google-task",
            title="Completed: Google task",
            body="Google Tasks marked this task completed.",
            entry_type="note",
            follows=["2026-08-02-local-task"],
        ), name="completion.md")
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": "2026-08-02-local-task",
                    "source_id": source_id,
                    "google_hash": "completed-google",
                    "memory_hash": "resolved-memory",
                    "terminal": True,
                },
            },
        }))

        report = self.run_dry({})

        self.assertEqual(report["planned"], [])
        self.assertTrue(report["ok"])

    def test_reopening_memory_side_of_terminal_mapping_is_a_conflict(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        completed = task(
            status="completed",
            completed="2026-08-02T09:00:00.000Z",
            updated="2026-08-02T09:00:00.000Z",
        )
        body = google_tasks_sync._memory_body(completed, "Incoming")
        self.write_entry(entry_text(
            title="Google task",
            body=body,
            source_ids=[source_id],
        ))
        reopened = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        previously_resolved = dict(reopened, resolved=True)
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": reopened["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(completed),
                    "memory_hash": google_tasks_sync._memory_hash(
                        previously_resolved
                    ),
                    "terminal": True,
                },
            },
        }))
        checkpoint_before = self.checkpoint.read_text()

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync, "_open_tasks", return_value={}
        ), mock.patch.object(
            google_tasks_sync, "_gws", return_value=completed
        ) as gws, mock.patch.object(
            google_tasks_sync, "_memory_cli"
        ) as memory_cli:
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=True,
            )

        gws.assert_called_once_with(["get", LIST_ID, TASK_ID])
        memory_cli.assert_not_called()
        self.assertEqual(report["conflicts"], 1)
        self.assertIn("reopened", report["planned"][0]["reason"])
        self.assertEqual(self.checkpoint.read_text(), checkpoint_before)

    def test_reopening_google_side_of_terminal_mapping_is_a_conflict(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        completed = task(
            status="completed",
            completed="2026-08-02T09:00:00.000Z",
            updated="2026-08-02T09:00:00.000Z",
        )
        body = google_tasks_sync._memory_body(completed, "Incoming")
        self.write_entry(entry_text(
            title="Google task",
            body=body,
            source_ids=[source_id],
        ))
        self.write_entry(entry_text(
            entry_id="2026-08-02-completed-google-task",
            title="Completed: Google task",
            body="Google Tasks marked this task completed.",
            entry_type="note",
            follows=["2026-08-02-local-task"],
        ), name="completion.md")
        resolved = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.assertTrue(resolved["resolved"])
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": resolved["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(completed),
                    "memory_hash": google_tasks_sync._memory_hash(resolved),
                    "terminal": True,
                },
            },
        }))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": task()})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("reopened", report["planned"][0]["reason"])
        self.assertFalse(report["ok"])

    def test_google_content_edit_and_completion_together_fail_closed(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        old_task = task(notes="Old notes.")
        old_body = google_tasks_sync._memory_body(old_task, "Incoming")
        self.write_entry(entry_text(
            title="Google task",
            body=old_body,
            source_ids=[source_id],
        ))
        memory_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": memory_entry["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(old_task),
                    "memory_hash": google_tasks_sync._memory_hash(memory_entry),
                    "terminal": False,
                },
            },
        }))
        checkpoint_before = self.checkpoint.read_text()
        changed_and_completed = task(
            notes="New notes at completion.",
            status="completed",
            completed="2026-08-02T09:00:00.000Z",
            updated="2026-08-02T09:00:00.000Z",
        )

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": changed_and_completed},
        ), mock.patch.object(
            google_tasks_sync, "_memory_cli"
        ) as memory_cli, mock.patch.object(
            google_tasks_sync, "_commit_store"
        ) as commit:
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=False,
            )

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("content and completion", report["planned"][0]["reason"])
        self.assertFalse(report["ok"])
        memory_cli.assert_not_called()
        commit.assert_not_called()
        self.assertEqual(self.checkpoint.read_text(), checkpoint_before)
        self.assertNotIn(
            "last_successful_sync_at",
            json.loads(self.checkpoint.read_text()),
        )

    def test_memory_content_edit_and_resolution_together_fail_closed(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        google = task(notes="Old notes.")
        old_body = google_tasks_sync._memory_body(google, "Incoming")
        changed_body = google_tasks_sync._memory_body(
            task(notes="Locally edited notes."),
            "Incoming",
        )
        self.write_entry(entry_text(
            title="Google task",
            body=changed_body,
            source_ids=[source_id],
        ))
        self.write_entry(entry_text(
            entry_id="2026-08-02-completed-google-task",
            title="Completed: Google task",
            body="Completed locally.",
            entry_type="note",
            follows=["2026-08-02-local-task"],
        ), name="completion.md")
        current = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        baseline = dict(current, body=old_body, resolved=False)
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": current["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(google),
                    "memory_hash": google_tasks_sync._memory_hash(baseline),
                    "terminal": False,
                },
            },
        }))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": google})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("content and completion", report["planned"][0]["reason"])
        self.assertNotIn("complete_google", report)
        self.assertFalse(report["ok"])

    def test_clean_noop_updates_health_without_committing_memory(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        current_task = task()
        body = google_tasks_sync._memory_body(current_task, "Incoming")
        self.write_entry(entry_text(
            title="Google task",
            body=body,
            source_ids=[source_id],
        ))
        memory_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": memory_entry["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(current_task),
                    "memory_hash": google_tasks_sync._memory_hash(memory_entry),
                    "terminal": False,
                },
            },
        }))

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": current_task},
        ), mock.patch.object(
            google_tasks_sync, "_commit_store"
        ) as commit:
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=False,
            )

        self.assertEqual(report["planned"], [])
        self.assertTrue(report["ok"])
        commit.assert_not_called()
        self.assertIn(
            "last_successful_sync_at",
            json.loads(self.checkpoint.read_text()),
        )

    def test_real_import_writes_mapping_and_commits_memory(self):
        current_task = task()
        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": current_task},
        ), mock.patch.object(
            google_tasks_sync,
            "_memory_upsert",
            return_value="2026-08-02-google-task",
        ) as upsert, mock.patch.object(
            google_tasks_sync, "_assert_google_unchanged"
        ), mock.patch.object(
            google_tasks_sync, "_commit_store"
        ) as commit:
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=False,
            )

        self.assertEqual(report["create_memory"], 1)
        upsert.assert_called_once()
        commit.assert_called_once()
        saved = json.loads(self.checkpoint.read_text())
        mapping = saved["mappings"][f"{LIST_ID}:{TASK_ID}"]
        self.assertEqual(mapping["memory_id"], "2026-08-02-google-task")
        self.assertFalse(mapping["terminal"])

    def test_real_export_links_created_google_task_back_to_memory(self):
        self.write_entry(entry_text())
        created = {
            **task(title="Local task"),
            "notes": (
                "Do the local work.\n\n"
                "Synced from personal memory: 2026-08-02-local-task"
            ),
        }
        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync, "_open_tasks", return_value={}
        ), mock.patch.object(
            google_tasks_sync, "_create_google", return_value=created
        ) as create, mock.patch.object(
            google_tasks_sync,
            "_memory_upsert",
            return_value="2026-08-02-local-task",
        ) as upsert, mock.patch.object(
            google_tasks_sync, "_assert_google_unchanged"
        ), mock.patch.object(
            google_tasks_sync, "_commit_store"
        ) as commit:
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=False,
            )

        self.assertEqual(report["create_google"], 1)
        create.assert_called_once()
        self.assertEqual(upsert.call_args.args[-1], "2026-08-02-local-task")
        commit.assert_called_once()
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual(
            saved["mappings"][f"{LIST_ID}:{TASK_ID}"]["memory_id"],
            "2026-08-02-local-task",
        )
        self.assertFalse(
            saved["mappings"][f"{LIST_ID}:{TASK_ID}"]["pending_link"]
        )

    def test_outbound_post_create_race_keeps_identity_and_never_duplicates(self):
        self.write_entry(entry_text())
        created = {
            **task(title="Local task"),
            "notes": (
                "Do the local work.\n\n"
                "Synced from personal memory: 2026-08-02-local-task"
            ),
        }

        def create_then_concurrent_edit(_tasklist, _entry):
            self.write_entry(entry_text(
                title="Renamed concurrently",
                body="Changed while Google create was in flight.",
            ))
            return created

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync, "_open_tasks", return_value={}
        ), mock.patch.object(
            google_tasks_sync,
            "_create_google",
            side_effect=create_then_concurrent_edit,
        ), mock.patch.object(
            google_tasks_sync, "_memory_upsert"
        ) as upsert, mock.patch.object(
            google_tasks_sync, "_commit_store"
        ) as commit:
            with self.assertRaisesRegex(RuntimeError, "concurrent memory change"):
                google_tasks_sync.run(
                    self.cfg,
                    checkpoint_path=self.checkpoint,
                    dry_run=False,
                )

        upsert.assert_not_called()
        commit.assert_not_called()
        saved = json.loads(self.checkpoint.read_text())
        pending = saved["mappings"][f"{LIST_ID}:{TASK_ID}"]
        self.assertEqual(pending["memory_id"], "2026-08-02-local-task")
        self.assertTrue(pending["pending_link"])

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": created})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("identity", report["planned"][0]["reason"])
        self.assertNotIn("create_memory", report)
        self.assertNotIn("create_google", report)

    def test_origin_marker_recovers_pre_checkpoint_google_create(self):
        self.write_entry(entry_text())
        created = {
            **task(title="Renamed after remote create"),
            "notes": (
                "Do the local work.\n\n"
                "Synced from personal memory: 2026-08-02-local-task"
            ),
        }

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": created})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("recovered outbound origin", report["planned"][0]["reason"])
        self.assertNotIn("create_memory", report)
        self.assertNotIn("create_google", report)
        self.assertFalse(report["ok"])

    def test_aligned_origin_marker_recovers_by_linking_memory(self):
        self.write_entry(entry_text())
        created = {
            **task(title="Local task"),
            "notes": (
                "Do the local work.\n\n"
                "Synced from personal memory: 2026-08-02-local-task"
            ),
        }

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": created})

        self.assertEqual(report["link_memory"], 1)
        self.assertNotIn("create_memory", report)
        self.assertNotIn("create_google", report)
        self.assertEqual(report["planned"][0]["memory_date"], "2026-08-02")
        self.assertEqual(
            report["planned"][0]["source_updated"],
            "2026-08-02T08:00:00.000Z",
        )
        self.assertTrue(report["ok"])

    def test_two_origin_markers_cannot_claim_the_same_memory_todo(self):
        self.write_entry(entry_text())
        notes = (
            "Do the local work.\n\n"
            "Synced from personal memory: 2026-08-02-local-task"
        )
        second_id = "task-second"

        report = self.run_dry({
            f"{LIST_ID}:{TASK_ID}": task(
                title="Local task",
                notes=notes,
            ),
            f"{LIST_ID}:{second_id}": task(
                id=second_id,
                title="Local task",
                notes=notes,
            ),
        })

        self.assertEqual(report["link_memory"], 1)
        self.assertEqual(report["conflicts"], 1)
        self.assertIn("already claimed", report["planned"][1]["reason"])
        self.assertNotIn("create_google", report)
        self.assertFalse(report["ok"])

    def test_spoofed_origin_never_reclassifies_non_todo_memory(self):
        self.write_entry(entry_text(
            entry_type="note",
            title="Local task",
            body="Do the local work.",
        ))
        created = {
            **task(title="Local task"),
            "notes": (
                "Do the local work.\n\n"
                "Synced from personal memory: 2026-08-02-local-task"
            ),
        }

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": created})

        self.assertEqual(report["conflicts"], 1)
        self.assertNotIn("link_memory", report)
        self.assertFalse(report["ok"])

    def test_memory_body_round_trips_notes_list_and_due(self):
        google = task(due="2026-08-10T00:00:00.000Z")
        body = google_tasks_sync._memory_body(google, "Incoming")
        fields = google_tasks_sync._memory_to_google({
            "id": "2026-08-02-task",
            "title": google["title"],
            "body": body,
            "source_ids": [google_tasks_sync._source_id(LIST_ID, TASK_ID)],
        })

        self.assertEqual(fields["due"], "2026-08-10")
        self.assertIn("Concrete task notes.", fields["notes"])
        self.assertNotIn("[Google Tasks]", fields["notes"])
        self.assertIn("Synced from personal memory", fields["notes"])

    def test_blank_google_notes_round_trip_without_leaking_metadata(self):
        google = task(notes="")
        body = google_tasks_sync._memory_body(google, "Incoming")
        entry = {
            "id": "2026-08-02-task",
            "title": google["title"],
            "body": body,
            "resolved": False,
            "source_ids": [google_tasks_sync._source_id(LIST_ID, TASK_ID)],
        }

        fields = google_tasks_sync._memory_to_google(entry)

        self.assertEqual(
            body,
            "[Google Tasks]\n"
            "List: Incoming\n"
            "Initial Google updated: 2026-08-02T08:00:00.000Z",
        )
        self.assertEqual(
            fields["notes"],
            "Synced from personal memory: 2026-08-02-task",
        )
        self.assertTrue(google_tasks_sync._content_aligned(google, entry))

    def test_initial_import_uses_and_records_google_updated_timestamp(self):
        google = task(updated="2026-05-26T10:52:05.411Z")

        with mock.patch.object(
            google_tasks_sync,
            "_memory_cli",
            return_value=SimpleNamespace(
                returncode=0,
                stdout="created 2026-05-26-google-task",
                stderr="",
            ),
        ) as memory_cli:
            entry_id = google_tasks_sync._memory_upsert(
                self.store,
                google,
                self.tasklists[LIST_ID],
                google_tasks_sync._source_id(LIST_ID, TASK_ID),
            )

        self.assertEqual(entry_id, "2026-05-26-google-task")
        args = memory_cli.call_args.args[1]
        self.assertEqual(args[args.index("--date") + 1], "2026-05-26")
        body = args[args.index("--body") + 1]
        self.assertIn(
            "Initial Google updated: 2026-05-26T10:52:05.411Z",
            body,
        )

    def test_unlinked_lookalike_metadata_cannot_spoof_initial_timestamp(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        lookalike_body = (
            "[Google Tasks]\n"
            "List: User-authored\n"
            "Initial Google updated: 2020-01-01T00:00:00.000Z"
        )
        existing = {
            "id": "2026-08-02-local-task",
            "date": "2026-08-02",
            "type": "todo",
            "title": "Local task",
            "body": lookalike_body,
            "tags": ["work"],
            "source_ids": [],
            "follows": [],
            "resolved": False,
        }
        created = task(
            title="Local task",
            notes=lookalike_body,
            updated="2026-08-02T08:00:00.000Z",
        )

        with mock.patch.object(
            google_tasks_sync,
            "_memory_cli",
            return_value=SimpleNamespace(
                returncode=0,
                stdout="updated 2026-08-02-local-task",
                stderr="",
            ),
        ) as memory_cli:
            google_tasks_sync._memory_upsert(
                self.store,
                created,
                self.tasklists[LIST_ID],
                source_id,
                existing["id"],
                existing_entry=existing,
            )

        args = memory_cli.call_args.args[1]
        body = args[args.index("--body") + 1]
        self.assertEqual(body.count("Initial Google updated:"), 2)
        self.assertTrue(body.startswith(lookalike_body))
        self.assertTrue(body.endswith(
            "Initial Google updated: 2026-08-02T08:00:00.000Z"
        ))

    def test_later_google_update_preserves_initial_timestamp_and_memory_date(self):
        initial = task(updated="2026-05-26T10:52:05.411Z")
        existing = {
            "id": "2026-05-26-google-task",
            "date": "2026-05-26",
            "type": "todo",
            "title": initial["title"],
            "body": google_tasks_sync._memory_body(initial, "Incoming"),
            "tags": ["google-tasks"],
            "source_ids": [google_tasks_sync._source_id(LIST_ID, TASK_ID)],
            "follows": [],
            "resolved": False,
        }
        changed = task(
            notes="Changed later.",
            updated="2026-08-02T10:40:20.253Z",
        )

        with mock.patch.object(
            google_tasks_sync,
            "_memory_cli",
            return_value=SimpleNamespace(
                returncode=0,
                stdout="updated 2026-05-26-google-task",
                stderr="",
            ),
        ) as memory_cli:
            google_tasks_sync._memory_upsert(
                self.store,
                changed,
                self.tasklists[LIST_ID],
                google_tasks_sync._source_id(LIST_ID, TASK_ID),
                existing["id"],
                existing_entry=existing,
            )

        args = memory_cli.call_args.args[1]
        self.assertEqual(args[args.index("--date") + 1], "2026-05-26")
        body = args[args.index("--body") + 1]
        self.assertIn(
            "Initial Google updated: 2026-05-26T10:52:05.411Z",
            body,
        )
        self.assertNotIn("Initial Google updated: 2026-08-02", body)

    def test_user_notes_starting_with_marker_round_trip_unchanged(self):
        google = task(notes="[Google Tasks]\nUser-authored details")
        body = google_tasks_sync._memory_body(google, "Incoming")
        entry = {
            "id": "2026-08-02-task",
            "title": google["title"],
            "body": body,
            "resolved": False,
            "source_ids": [google_tasks_sync._source_id(LIST_ID, TASK_ID)],
        }

        fields = google_tasks_sync._memory_to_google(entry)

        self.assertTrue(fields["notes"].startswith(
            "[Google Tasks]\nUser-authored details\n\n"
        ))
        self.assertTrue(google_tasks_sync._content_aligned(google, entry))

    def test_raw_outbound_marker_text_is_preserved_as_user_notes(self):
        entry = {
            "id": "2026-08-02-task",
            "title": "Local task",
            "body": "[Google Tasks]\nUser-authored details",
            "source_ids": [],
        }

        fields = google_tasks_sync._memory_to_google(entry)

        self.assertTrue(fields["notes"].startswith(
            "[Google Tasks]\nUser-authored details\n\n"
        ))
        self.assertIn(
            "Synced from personal memory: 2026-08-02-task",
            fields["notes"],
        )

    def test_linked_marker_shaped_user_edit_is_sent_to_google_verbatim(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        google = task(notes="Old notes.")
        self.write_entry(entry_text(
            title=google["title"],
            body=google_tasks_sync._memory_body(google, "Incoming"),
            source_ids=[source_id],
        ))
        baseline = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": baseline["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(google),
                    "memory_hash": google_tasks_sync._memory_hash(baseline),
                    "terminal": False,
                },
            },
        }))
        self.write_entry(entry_text(
            title=google["title"],
            body="[Google Tasks]\nUser-authored details",
            source_ids=[source_id],
        ))

        def update(_list_id, _task_id, fields):
            self.assertTrue(fields["notes"].startswith(
                "[Google Tasks]\nUser-authored details\n\n"
            ))
            return task(title=fields["title"], notes=fields["notes"])

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": google},
        ), mock.patch.object(
            google_tasks_sync, "_assert_pair_unchanged"
        ), mock.patch.object(
            google_tasks_sync, "_update_google", side_effect=update
        ) as update_google:
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=False,
            )

        self.assertEqual(report["update_google"], 1)
        update_google.assert_called_once()
        self.assertTrue(report["ok"])

    def test_post_link_exclusion_skips_changes_on_both_sides(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        baseline_task = task()
        baseline_body = google_tasks_sync._memory_body(baseline_task, "Incoming")
        self.write_entry(entry_text(
            title=baseline_task["title"],
            body=baseline_body,
            tags=["work", "no-google-tasks"],
            source_ids=[source_id],
        ))
        baseline_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": baseline_entry["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(baseline_task),
                    "memory_hash": google_tasks_sync._memory_hash(baseline_entry),
                    "terminal": False,
                },
            },
        }))

        report = self.run_dry({
            f"{LIST_ID}:{TASK_ID}": task(title="Changed in Google"),
        })

        self.assertEqual(report["skip_excluded"], 1)
        self.assertNotIn("update_memory", report)
        self.assertNotIn("update_google", report)
        self.assertNotIn("complete_memory", report)
        self.assertNotIn("complete_google", report)
        self.assertTrue(report["ok"])

    def test_real_blank_note_import_establishes_mapping_without_conflict(self):
        current_task = task(notes="")
        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": current_task},
        ), mock.patch.object(
            google_tasks_sync,
            "_memory_upsert",
            return_value="2026-08-02-google-task",
        ), mock.patch.object(
            google_tasks_sync, "_assert_google_unchanged"
        ), mock.patch.object(
            google_tasks_sync, "_commit_store"
        ):
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=False,
            )

        self.assertEqual(report["create_memory"], 1)
        self.assertEqual(report["conflicts"], 0)
        self.assertTrue(report["ok"])
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual(
            saved["mappings"][f"{LIST_ID}:{TASK_ID}"]["memory_id"],
            "2026-08-02-google-task",
        )

    def test_failed_memory_update_does_not_advance_checkpoint(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        old_task = task(notes="Old notes.")
        old_body = google_tasks_sync._memory_body(old_task, "Incoming")
        self.write_entry(entry_text(
            title="Google task",
            body=old_body,
            source_ids=[source_id],
        ))
        memory_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": memory_entry["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(old_task),
                    "memory_hash": google_tasks_sync._memory_hash(memory_entry),
                    "terminal": False,
                },
            },
        }))
        checkpoint_before = self.checkpoint.read_text()
        changed_task = task(notes="New Google notes.")

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": changed_task},
        ), mock.patch.object(
            google_tasks_sync,
            "_memory_cli",
            return_value=SimpleNamespace(
                returncode=2,
                stdout="",
                stderr="reported error before write",
            ),
        ), mock.patch.object(
            google_tasks_sync, "_assert_pair_unchanged"
        ), mock.patch.object(google_tasks_sync, "_commit_store"):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                google_tasks_sync.run(
                    self.cfg,
                    checkpoint_path=self.checkpoint,
                    dry_run=False,
                )

        self.assertEqual(self.checkpoint.read_text(), checkpoint_before)
        unchanged = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.assertEqual(unchanged["body"], old_body)

    def test_concurrent_memory_edit_aborts_inbound_update_before_write(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        old_task = task(notes="Old notes.")
        old_body = google_tasks_sync._memory_body(old_task, "Incoming")
        self.write_entry(entry_text(
            title="Google task",
            body=old_body,
            source_ids=[source_id],
        ))
        memory_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": memory_entry["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(old_task),
                    "memory_hash": google_tasks_sync._memory_hash(memory_entry),
                    "terminal": False,
                },
            },
        }))
        checkpoint_before = self.checkpoint.read_text()
        changed_task = task(notes="Changed in Google.")

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": changed_task},
        ), mock.patch.object(
            google_tasks_sync,
            "_assert_pair_unchanged",
            side_effect=RuntimeError("concurrent memory change detected"),
        ), mock.patch.object(
            google_tasks_sync, "_memory_upsert"
        ) as upsert:
            with self.assertRaisesRegex(RuntimeError, "concurrent memory"):
                google_tasks_sync.run(
                    self.cfg,
                    checkpoint_path=self.checkpoint,
                    dry_run=False,
                )

        upsert.assert_not_called()
        self.assertEqual(self.checkpoint.read_text(), checkpoint_before)

    def test_concurrent_google_edit_aborts_local_update_before_write(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        google = task(notes="Old notes.")
        changed_body = google_tasks_sync._memory_body(
            task(notes="Changed in memory."),
            "Incoming",
        )
        self.write_entry(entry_text(
            title="Google task",
            body=changed_body,
            source_ids=[source_id],
        ))
        current = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        baseline = dict(
            current,
            body=google_tasks_sync._memory_body(google, "Incoming"),
        )
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": current["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(google),
                    "memory_hash": google_tasks_sync._memory_hash(baseline),
                    "terminal": False,
                },
            },
        }))
        checkpoint_before = self.checkpoint.read_text()

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": google},
        ), mock.patch.object(
            google_tasks_sync,
            "_assert_pair_unchanged",
            side_effect=RuntimeError("concurrent Google Tasks change detected"),
        ), mock.patch.object(
            google_tasks_sync, "_update_google"
        ) as update:
            with self.assertRaisesRegex(RuntimeError, "concurrent Google"):
                google_tasks_sync.run(
                    self.cfg,
                    checkpoint_path=self.checkpoint,
                    dry_run=False,
                )

        update.assert_not_called()
        self.assertEqual(self.checkpoint.read_text(), checkpoint_before)

    def test_memory_guard_detects_concurrent_tag_and_source_identity_changes(self):
        self.write_entry(entry_text())
        observed = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        cases = [
            {"tags": ["work", "no-google-tasks"]},
            {
                "source_ids": [
                    google_tasks_sync._source_id("other-list", "other-task")
                ]
            },
        ]
        for index, changes in enumerate(cases):
            with self.subTest(index=index):
                self.write_entry(entry_text(**changes))
                with self.assertRaisesRegex(RuntimeError, "concurrent memory"):
                    google_tasks_sync._assert_memory_unchanged(
                        self.store,
                        observed,
                    )
                self.write_entry(entry_text())

    def test_outbound_rechecks_exclusion_before_create(self):
        self.write_entry(entry_text())

        def now_excluded(_store, observed):
            return dict(observed, tags=[*observed["tags"], "no-google-tasks"])

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync, "_open_tasks", return_value={}
        ), mock.patch.object(
            google_tasks_sync,
            "_assert_memory_unchanged",
            side_effect=now_excluded,
        ), mock.patch.object(
            google_tasks_sync, "_create_google"
        ) as create:
            with self.assertRaisesRegex(RuntimeError, "became excluded"):
                google_tasks_sync.run(
                    self.cfg,
                    checkpoint_path=self.checkpoint,
                    dry_run=False,
                )

        create.assert_not_called()
        self.assertFalse(self.checkpoint.exists())

    def test_stranded_pending_identity_reserves_memory_and_fails_closed(self):
        self.write_entry(entry_text())
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                "removed-list:created-task": {
                    "memory_id": "2026-08-02-local-task",
                    "source_id": google_tasks_sync._source_id(
                        "removed-list",
                        "created-task",
                    ),
                    "google_hash": "created-google-hash",
                    "memory_hash": "pre-link-memory-hash",
                    "terminal": False,
                    "pending_link": True,
                },
            },
        }))

        report = self.run_dry({})

        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("no longer selected", report["errors"][0]["error"])
        self.assertNotIn("create_google", report)
        self.assertFalse(report["ok"])

    def test_pending_link_dry_plan_shows_source_and_preserved_memory_dates(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        self.write_entry(entry_text())
        entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        google = task(
            title="Local task",
            notes=(
                "Do the local work.\n\n"
                "Synced from personal memory: 2026-08-02-local-task"
            ),
        )
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": entry["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(google),
                    "memory_hash": google_tasks_sync._memory_hash(entry),
                    "terminal": False,
                    "pending_link": True,
                },
            },
        }))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": google})

        self.assertEqual(report["link_memory"], 1)
        plan = report["planned"][0]
        self.assertEqual(plan["source_updated"], "2026-08-02T08:00:00.000Z")
        self.assertEqual(plan["memory_date"], "2026-08-02")
        self.assertTrue(report["ok"])

    def test_missing_updated_new_import_fails_before_any_write(self):
        google = task()
        google.pop("updated")

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": google},
        ), mock.patch.object(
            google_tasks_sync, "_memory_cli"
        ) as memory_cli, mock.patch.object(
            google_tasks_sync, "_gws"
        ) as gws:
            with self.assertRaisesRegex(RuntimeError, "valid updated timestamp"):
                google_tasks_sync.run(
                    self.cfg,
                    checkpoint_path=self.checkpoint,
                    dry_run=False,
                )

        memory_cli.assert_not_called()
        gws.assert_not_called()
        self.assertFalse(self.checkpoint.exists())

    def test_invalid_updated_exact_link_fails_before_any_write(self):
        self.write_entry(entry_text(
            title="Google task",
            body="Concrete task notes.",
        ))
        google = task(updated="not-a-timestamp")

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync,
            "_open_tasks",
            return_value={f"{LIST_ID}:{TASK_ID}": google},
        ), mock.patch.object(
            google_tasks_sync, "_memory_cli"
        ) as memory_cli, mock.patch.object(
            google_tasks_sync, "_gws"
        ) as gws:
            with self.assertRaisesRegex(RuntimeError, "valid updated timestamp"):
                google_tasks_sync.run(
                    self.cfg,
                    checkpoint_path=self.checkpoint,
                    dry_run=True,
                )

        memory_cli.assert_not_called()
        gws.assert_not_called()
        self.assertFalse(self.checkpoint.exists())

    def test_outbound_create_without_updated_never_records_checkpoint(self):
        self.write_entry(entry_text())
        created = task(title="Local task")
        created.pop("updated")

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync, "_open_tasks", return_value={}
        ), mock.patch.object(
            google_tasks_sync, "_create_google", return_value=created
        ) as create, mock.patch.object(
            google_tasks_sync, "_memory_cli"
        ) as memory_cli:
            with self.assertRaisesRegex(RuntimeError, "valid updated timestamp"):
                google_tasks_sync.run(
                    self.cfg,
                    checkpoint_path=self.checkpoint,
                    dry_run=False,
                )

        create.assert_called_once()
        memory_cli.assert_not_called()
        self.assertFalse(self.checkpoint.exists())

    def test_normal_mapping_without_canonical_memory_source_fails_closed(self):
        google = task(title="Local task", notes="Do the local work.")
        self.write_entry(entry_text())
        memory_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": memory_entry["id"],
                    "source_id": google_tasks_sync._source_id(LIST_ID, TASK_ID),
                    "google_hash": google_tasks_sync._google_hash(google),
                    "memory_hash": google_tasks_sync._memory_hash(memory_entry),
                    "terminal": False,
                },
            },
        }))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": google})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("identity was removed", report["planned"][0]["reason"])
        self.assertNotIn("link_memory", report)
        self.assertNotIn("create_google", report)
        self.assertFalse(report["ok"])

    def test_inaccessible_mapped_task_cannot_be_reexported(self):
        google = task(title="Local task", notes="Do the local work.")
        self.write_entry(entry_text())
        memory_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": memory_entry["id"],
                    "source_id": google_tasks_sync._source_id(LIST_ID, TASK_ID),
                    "google_hash": google_tasks_sync._google_hash(google),
                    "memory_hash": google_tasks_sync._memory_hash(memory_entry),
                    "terminal": False,
                },
            },
        }))

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync, "_open_tasks", return_value={}
        ), mock.patch.object(
            google_tasks_sync,
            "_gws",
            side_effect=RuntimeError("task unavailable"),
        ) as gws, mock.patch.object(
            google_tasks_sync, "_create_google"
        ) as create:
            report = google_tasks_sync.run(
                self.cfg,
                checkpoint_path=self.checkpoint,
                dry_run=False,
            )

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("identity was removed", report["planned"][0]["reason"])
        gws.assert_not_called()
        create.assert_not_called()
        self.assertFalse(report["ok"])

    def test_memory_entry_with_two_google_identities_fails_before_mutation(self):
        first_source = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        second_source = google_tasks_sync._source_id(LIST_ID, "task-second")
        self.write_entry(entry_text(source_ids=[first_source, second_source]))

        with mock.patch.object(
            google_tasks_sync, "_tasklists", return_value=self.tasklists
        ), mock.patch.object(
            google_tasks_sync, "_open_tasks", return_value={}
        ), mock.patch.object(
            google_tasks_sync, "_create_google"
        ) as create, mock.patch.object(
            google_tasks_sync, "_memory_cli"
        ) as memory_cli:
            with self.assertRaisesRegex(RuntimeError, "multiple Google Tasks"):
                google_tasks_sync.run(
                    self.cfg,
                    checkpoint_path=self.checkpoint,
                    dry_run=False,
                )

        create.assert_not_called()
        memory_cli.assert_not_called()

    def test_pending_mapping_and_canonical_source_cannot_claim_different_entries(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        google = task(title="Second task", notes="Second body.")
        self.write_entry(entry_text(), name="first.md")
        self.write_entry(entry_text(
            entry_id="2026-08-02-second-task",
            title="Second task",
            body=google_tasks_sync._memory_body(google, "Incoming"),
            source_ids=[source_id],
        ), name="second.md")
        first = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": first["id"],
                    "source_id": source_id,
                    "google_hash": google_tasks_sync._google_hash(google),
                    "memory_hash": google_tasks_sync._memory_hash(first),
                    "terminal": False,
                    "pending_link": True,
                },
            },
        }))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": google})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("different memory entries", report["planned"][0]["reason"])
        self.assertNotIn("link_memory", report)
        self.assertFalse(report["ok"])

    def test_two_pending_mappings_cannot_claim_the_same_memory_todo(self):
        self.write_entry(entry_text())
        first_source = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        second_id = "task-second"
        second_source = google_tasks_sync._source_id(LIST_ID, second_id)
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": "2026-08-02-local-task",
                    "source_id": first_source,
                    "google_hash": "first-google",
                    "memory_hash": "local",
                    "terminal": False,
                    "pending_link": True,
                },
                f"{LIST_ID}:{second_id}": {
                    "memory_id": "2026-08-02-local-task",
                    "source_id": second_source,
                    "google_hash": "second-google",
                    "memory_hash": "local",
                    "terminal": False,
                    "pending_link": True,
                },
            },
        }))

        report = self.run_dry({
            f"{LIST_ID}:{TASK_ID}": task(
                title="Local task",
                notes="Do the local work.",
            ),
            f"{LIST_ID}:{second_id}": task(
                id=second_id,
                title="Local task",
                notes="Do the local work.",
            ),
        })

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("same memory entry", report["planned"][0]["reason"])
        self.assertNotIn("link_memory", report)
        self.assertNotIn("create_google", report)
        self.assertFalse(report["ok"])

    def test_canonical_source_and_origin_marker_must_identify_same_entry(self):
        source_id = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        self.write_entry(entry_text(), name="origin.md")
        self.write_entry(entry_text(
            entry_id="2026-08-02-canonical-task",
            title="Canonical task",
            body="Canonical body.",
            source_ids=[source_id],
        ), name="canonical.md")
        google = task(
            title="Canonical task",
            notes=(
                "Canonical body.\n\n"
                "Synced from personal memory: 2026-08-02-local-task"
            ),
        )

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": google})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("canonical and origin", report["planned"][0]["reason"])
        self.assertNotIn("update_memory", report)
        self.assertNotIn("create_google", report)
        self.assertFalse(report["ok"])

    def test_pending_link_rejects_a_different_google_identity(self):
        pending_source = google_tasks_sync._source_id(LIST_ID, TASK_ID)
        other_source = google_tasks_sync._source_id("other-list", "other-task")
        google = task(title="Local task", notes="Do the local work.")
        self.write_entry(entry_text(source_ids=[other_source]))
        memory_entry = google_tasks_sync._load_memory_entries(self.store)[
            "2026-08-02-local-task"
        ]
        self.checkpoint.write_text(json.dumps({
            "version": 1,
            "mappings": {
                f"{LIST_ID}:{TASK_ID}": {
                    "memory_id": memory_entry["id"],
                    "source_id": pending_source,
                    "google_hash": google_tasks_sync._google_hash(google),
                    "memory_hash": google_tasks_sync._memory_hash(memory_entry),
                    "terminal": False,
                    "pending_link": True,
                },
            },
        }))

        report = self.run_dry({f"{LIST_ID}:{TASK_ID}": google})

        self.assertEqual(report["conflicts"], 1)
        self.assertIn("different Google Tasks", report["planned"][0]["reason"])
        self.assertNotIn("link_memory", report)
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()
