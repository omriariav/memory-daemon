"""Behavioral tests for the marketplace administration helper."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
ADMIN = (
    PROJECT
    / "plugins"
    / "memory-daemon-manager"
    / "scripts"
    / "memory_daemon_admin.py"
)


def routine_yaml(rid, instruction="Keep durable decisions and commitments."):
    return f"""\
id: {rid}
enabled: true
schedule:
  every: 4h
source:
  kind: gmail
  query: 'is:unread'
  actions: []
analyze:
  provider: gemini
  model: example-model
  instruction: {instruction}
output:
  vault_dir: /tmp/example-vault
  slug_prefix: {rid}
"""


class PluginAdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "daemon"
        self.store = self.root / "memory-store"
        self.repo.mkdir()
        self.store.mkdir()
        shutil.copy2(PROJECT / "daemon.py", self.repo / "daemon.py")
        shutil.copytree(PROJECT / "workspace_daemon", self.repo / "workspace_daemon")
        (self.repo / "routines").mkdir()
        (self.repo / "state").mkdir()
        (self.store / "connectors").mkdir()

    def run_admin(self, *args, expect=0):
        result = subprocess.run(
            [sys.executable, str(ADMIN), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            result.returncode,
            expect,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    @staticmethod
    def token(output):
        match = re.search(r"^plan-token: ([a-f0-9]{64})$", output, re.M)
        if not match:
            raise AssertionError(f"no plan token in:\n{output}")
        return match.group(1)

    def candidate(self, name, content):
        path = self.root / name
        path.write_text(content)
        return path

    def test_routine_add_edit_remove_is_planned_validated_and_scoped(self):
        candidate = self.candidate("candidate.yaml", routine_yaml("example-routine"))
        plan = self.run_admin(
            "routine", "plan", "--repo", self.repo,
            "--operation", "add", "--id", "example-routine",
            "--candidate", candidate,
        )
        self.assertIn("+++ b/routines/example-routine.yaml", plan.stdout)
        target = self.repo / "routines" / "example-routine.yaml"
        self.assertFalse(target.exists())

        applied = self.run_admin(
            "routine", "apply", "--repo", self.repo,
            "--operation", "add", "--id", "example-routine",
            "--candidate", candidate, "--token", self.token(plan.stdout),
        )
        self.assertTrue(target.exists())
        self.assertEqual(json.loads(applied.stdout)["validation"], "ok")

        edited = self.candidate(
            "edited.yaml",
            "# user comment stays\n" + routine_yaml(
                "example-routine", "Keep durable decisions, owners, dates, and commitments."
            ),
        )
        edit_plan = self.run_admin(
            "routine", "plan", "--repo", self.repo,
            "--operation", "edit", "--id", "example-routine",
            "--candidate", edited,
        )
        self.run_admin(
            "routine", "apply", "--repo", self.repo,
            "--operation", "edit", "--id", "example-routine",
            "--candidate", edited, "--token", self.token(edit_plan.stdout),
        )
        self.assertTrue(target.read_text().startswith("# user comment stays"))

        ledger = self.repo / "state" / "processed.json"
        ledger.write_text('{"keep-me": {"rule_id": "example-routine"}}\n')
        remove_plan = self.run_admin(
            "routine", "plan", "--repo", self.repo,
            "--operation", "remove", "--id", "example-routine",
        )
        self.run_admin(
            "routine", "apply", "--repo", self.repo,
            "--operation", "remove", "--id", "example-routine",
            "--token", self.token(remove_plan.stdout),
            expect=2,
        )
        self.assertTrue(target.exists())
        self.run_admin(
            "routine", "apply", "--repo", self.repo,
            "--operation", "remove", "--id", "example-routine",
            "--token", self.token(remove_plan.stdout),
            "--confirm-target", "example-routine",
        )
        self.assertFalse(target.exists())
        self.assertIn("keep-me", ledger.read_text())

    def test_stale_plan_token_cannot_overwrite_a_newer_routine(self):
        target = self.repo / "routines" / "example.yaml"
        target.write_text(routine_yaml("example"))
        candidate = self.candidate(
            "candidate.yaml",
            routine_yaml("example", "Keep durable decisions, dates, and explicit owners."),
        )
        plan = self.run_admin(
            "routine", "plan", "--repo", self.repo,
            "--operation", "edit", "--id", "example", "--candidate", candidate,
        )
        target.write_text("# concurrent edit\n" + routine_yaml("example"))
        self.run_admin(
            "routine", "apply", "--repo", self.repo,
            "--operation", "edit", "--id", "example", "--candidate", candidate,
            "--token", self.token(plan.stdout),
            expect=2,
        )
        self.assertTrue(target.read_text().startswith("# concurrent edit"))

    def test_invalid_candidate_is_rejected_before_write(self):
        invalid = self.candidate(
            "invalid.yaml",
            """\
id: invalid
sources:
  - kind: slack
    channels: [C0123EXAMPLE]
    actions: [archive]
analyze:
  provider: gemini
  model: example-model
  instruction: Keep durable decisions and commitments.
memory:
  store: /tmp/example-memory
""",
        )
        self.run_admin(
            "routine", "plan", "--repo", self.repo,
            "--operation", "add", "--id", "invalid", "--candidate", invalid,
            expect=2,
        )
        self.assertFalse((self.repo / "routines" / "invalid.yaml").exists())

    def test_add_rejects_existing_filename_with_a_different_declared_id(self):
        occupied = self.repo / "routines" / "foo.yaml"
        occupied.write_text(routine_yaml("bar"))
        candidate = self.candidate("candidate.yaml", routine_yaml("foo"))
        self.run_admin(
            "routine", "plan", "--repo", self.repo,
            "--operation", "add", "--id", "foo", "--candidate", candidate,
            expect=2,
        )
        self.assertEqual(occupied.read_text(), routine_yaml("bar"))

    def test_numeric_routine_id_is_rejected_and_list_does_not_crash(self):
        numeric = routine_yaml("123")
        candidate = self.candidate("numeric.yaml", numeric)
        self.run_admin(
            "routine", "plan", "--repo", self.repo,
            "--operation", "add", "--id", "numeric", "--candidate", candidate,
            expect=2,
        )
        (self.repo / "routines" / "numeric.yaml").write_text(numeric)
        validation = subprocess.run(
            [sys.executable, str(self.repo / "daemon.py"), "validate"],
            cwd=self.repo, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(validation.returncode, 1)
        self.assertIn("id must use lowercase", validation.stdout)
        listing = subprocess.run(
            [sys.executable, str(self.repo / "daemon.py"), "list"],
            cwd=self.repo, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("123", listing.stdout)

    def test_failed_full_validation_rolls_back_change(self):
        (self.repo / "routines" / "already-broken.yaml").write_text(
            "id: already-broken\nsource: {}\n"
        )
        candidate = self.candidate("candidate.yaml", routine_yaml("new-routine"))
        plan = self.run_admin(
            "routine", "plan", "--repo", self.repo,
            "--operation", "add", "--id", "new-routine", "--candidate", candidate,
        )
        self.run_admin(
            "routine", "apply", "--repo", self.repo,
            "--operation", "add", "--id", "new-routine", "--candidate", candidate,
            "--token", self.token(plan.stdout),
            expect=2,
        )
        self.assertFalse((self.repo / "routines" / "new-routine.yaml").exists())

    def test_failed_prompt_remove_restores_private_permissions(self):
        (self.repo / "routines" / "already-broken.yaml").write_text(
            "id: already-broken\nsource: {}\n"
        )
        override = self.store / "memory" / "connectors" / "slack.md"
        override.parent.mkdir(parents=True)
        override.write_text(
            "Keep durable decisions, commitments, incidents, and material facts.\n"
        )
        os.chmod(override, 0o600)
        plan = self.run_admin(
            "prompt", "plan", "--repo", self.repo, "--store", self.store,
            "--operation", "remove", "--name", "slack",
        )
        self.run_admin(
            "prompt", "apply", "--repo", self.repo, "--store", self.store,
            "--operation", "remove", "--name", "slack",
            "--token", self.token(plan.stdout), "--confirm-target", "slack",
            expect=2,
        )
        self.assertTrue(override.exists())
        self.assertEqual(override.stat().st_mode & 0o777, 0o600)

    def test_failed_validation_preserves_concurrent_edit_and_conflict_copy(self):
        target = self.repo / "routines" / "example.yaml"
        original = routine_yaml("example")
        target.write_text(original)
        candidate = self.candidate(
            "candidate.yaml",
            routine_yaml("example", "Keep durable decisions, named owners, and dates."),
        )
        plan = self.run_admin(
            "routine", "plan", "--repo", self.repo,
            "--operation", "edit", "--id", "example", "--candidate", candidate,
        )
        (self.repo / "daemon.py").write_text(
            """\
#!/usr/bin/env python3
import sys
import time
from pathlib import Path
Path("validator-started").write_text("started")
time.sleep(0.5)
print("forced validation failure", file=sys.stderr)
raise SystemExit(1)
"""
        )
        process = subprocess.Popen(
            [
                sys.executable, str(ADMIN), "routine", "apply",
                "--repo", str(self.repo), "--operation", "edit",
                "--id", "example", "--candidate", str(candidate),
                "--token", self.token(plan.stdout),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        marker = self.repo / "validator-started"
        deadline = time.time() + 5
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists(), "validator did not start")
        concurrent = "# concurrent editor\n" + routine_yaml("example")
        target.write_text(concurrent)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 2, f"{stdout}\n{stderr}")
        self.assertEqual(target.read_text(), concurrent)
        copies = list(target.parent.glob(".example.yaml.rollback-conflict-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), original)
        self.assertEqual(copies[0].stat().st_mode & 0o777, 0o600)

    def test_routine_list_redacts_queries_and_source_identifiers(self):
        secret_query = "from:private.person@example.test"
        channel = "CSECRET123"
        (self.repo / "routines" / "redacted.yaml").write_text(
            f"""\
id: redacted
sources:
  - kind: gmail
    query: '{secret_query}'
    actions: []
  - kind: slack
    channels: [{channel}]
analyze:
  provider: gemini
  model: example-model
  instruction: Keep durable decisions and commitments.
memory:
  store: /tmp/example-memory
"""
        )
        result = self.run_admin("routine", "list", "--repo", self.repo)
        self.assertNotIn(secret_query, result.stdout)
        self.assertNotIn(channel, result.stdout)
        row = json.loads(result.stdout)["routines"][0]
        self.assertTrue(row["sources"][0]["query_configured"])
        self.assertEqual(row["sources"][1]["channel_count"], 1)

    def test_prompt_override_lifecycle_preserves_template_and_other_memory(self):
        template = self.store / "connectors" / "slack.md"
        template.write_text(
            "Keep durable decisions, commitments, incidents, and facts from this source.\n"
        )
        sentinel = self.store / "memory" / "keep.md"
        sentinel.parent.mkdir()
        sentinel.write_text("do not remove\n")
        candidate = self.candidate(
            "prompt.md",
            "Keep durable decisions, named owners, deadlines, and material constraints. "
            "Discard logistics and social chatter.\n",
        )

        listed = self.run_admin("prompt", "list", "--store", self.store)
        self.assertNotIn("named owners", listed.stdout)
        self.assertEqual(
            json.loads(listed.stdout)["prompts"][0]["resolved_origin"], "template"
        )

        plan = self.run_admin(
            "prompt", "plan", "--repo", self.repo, "--store", self.store,
            "--operation", "add", "--name", "slack", "--candidate", candidate,
        )
        override = self.store / "memory" / "connectors" / "slack.md"
        self.assertFalse(override.exists())
        self.run_admin(
            "prompt", "apply", "--repo", self.repo, "--store", self.store,
            "--operation", "add", "--name", "slack", "--candidate", candidate,
            "--token", self.token(plan.stdout),
        )
        self.assertTrue(override.exists())

        edited = self.candidate(
            "prompt-edited.md",
            "Keep durable decisions, named owners, deadlines, constraints, and incidents. "
            "Discard logistics, notifications, repetition, and social chatter.\n",
        )
        edit_plan = self.run_admin(
            "prompt", "plan", "--repo", self.repo, "--store", self.store,
            "--operation", "edit", "--name", "slack", "--candidate", edited,
        )
        self.run_admin(
            "prompt", "apply", "--repo", self.repo, "--store", self.store,
            "--operation", "edit", "--name", "slack", "--candidate", edited,
            "--token", self.token(edit_plan.stdout),
        )
        self.assertIn("incidents", override.read_text())

        remove_plan = self.run_admin(
            "prompt", "plan", "--repo", self.repo, "--store", self.store,
            "--operation", "remove", "--name", "slack",
        )
        self.run_admin(
            "prompt", "apply", "--repo", self.repo, "--store", self.store,
            "--operation", "remove", "--name", "slack",
            "--token", self.token(remove_plan.stdout),
            expect=2,
        )
        self.run_admin(
            "prompt", "apply", "--repo", self.repo, "--store", self.store,
            "--operation", "remove", "--name", "slack",
            "--token", self.token(remove_plan.stdout),
            "--confirm-target", "slack",
        )
        self.assertFalse(override.exists())
        self.assertTrue(template.exists())
        self.assertEqual(sentinel.read_text(), "do not remove\n")

    def test_short_prompt_is_rejected_before_write(self):
        candidate = self.candidate("short.md", "Summarize it.\n")
        self.run_admin(
            "prompt", "plan", "--repo", self.repo, "--store", self.store,
            "--operation", "add", "--name", "gmail", "--candidate", candidate,
            expect=2,
        )
        self.assertFalse((self.store / "memory" / "connectors" / "gmail.md").exists())


class PluginPackagingTest(unittest.TestCase):
    def test_manifests_and_skills_are_present(self):
        plugin = PROJECT / "plugins" / "memory-daemon-manager"
        codex = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
        claude = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
        codex_marketplace = json.loads(
            (PROJECT / ".agents" / "plugins" / "marketplace.json").read_text()
        )
        claude_marketplace = json.loads(
            (PROJECT / ".claude-plugin" / "marketplace.json").read_text()
        )
        self.assertEqual(codex["name"], "memory-daemon-manager")
        self.assertEqual(claude["name"], codex["name"])
        self.assertEqual(claude["version"], codex["version"])
        self.assertEqual(codex_marketplace["name"], "memory-daemon")
        self.assertEqual(
            codex_marketplace["plugins"][0]["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual(claude_marketplace["name"], "memory-daemon")
        self.assertEqual(
            claude_marketplace["plugins"][0]["source"],
            "./plugins/memory-daemon-manager",
        )
        self.assertTrue(ADMIN.stat().st_mode & 0o111)
        for name in (
            "manage-memory-daemon-routines",
            "manage-memory-connector-prompts",
        ):
            text = (plugin / "skills" / name / "SKILL.md").read_text()
            self.assertNotIn("[TODO:", text)
            self.assertIn("plan-token", text)


if __name__ == "__main__":
    unittest.main()
