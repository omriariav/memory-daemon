"""Slack watch file: loader, routine expansion, and the shared thread item."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import config, slack_source, slack_watch

WATCH = """\
channels:
  - C0EXAMPLE01
  - {id: C0EXAMPLE02, name: example-team}
  - D0EXAMPLE03
people: [U0EXAMPLEA, {id: U0EXAMPLEB, name: Example B}]
include_mentions: true
owner_action_channels: [C0EXAMPLE01]
bootstrap: 12h
"""

ROUTINE = """\
id: watch-test
analyze: {{provider: gemini, model: example-model, instruction: x}}
output: {{vault_dir: {vault}, slug_prefix: example}}
source:
  kind: {kind}
  watch: {watch}
{extra}"""


def write(directory, name, text):
    path = Path(directory) / name
    path.write_text(text)
    return path


class LoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def load(self, text):
        return slack_watch.load(write(self.tmp.name, "watch.yaml", text))

    def test_plain_and_alias_entries_load_as_ids(self):
        loaded = self.load(WATCH)
        self.assertEqual(
            loaded["channels"], ["C0EXAMPLE01", "C0EXAMPLE02", "D0EXAMPLE03"]
        )
        self.assertEqual(loaded["people"], ["U0EXAMPLEA", "U0EXAMPLEB"])
        self.assertTrue(loaded["include_mentions"])
        self.assertEqual(loaded["owner_action_channels"], ["C0EXAMPLE01"])
        self.assertEqual(loaded["bootstrap"], "12h")

    def test_defaults_and_duplicates(self):
        loaded = self.load("channels: [C0EXAMPLE01, C0EXAMPLE01]\n")
        self.assertEqual(loaded["channels"], ["C0EXAMPLE01"])
        self.assertEqual(loaded["people"], [])
        self.assertFalse(loaded["include_mentions"])
        self.assertEqual(loaded["bootstrap"], "3d")

    def test_bad_files_fail_clearly(self):
        for text, fragment in (
            ("chanels: [C0EXAMPLE01]\n", "unknown key"),
            ("channels: [not-an-id]\n", "not a channel id"),
            ("channels: [U0EXAMPLEA]\n", "not a channel id"),
            ("people: [C0EXAMPLE01]\n", "not a user id"),
            ("channels: C0EXAMPLE01\n", "must be a list"),
            ("channels: [{id: C0EXAMPLE01, extra: 1}]\n", "mapping entries"),
            ("include_mentions: yes please\n", "true or false"),
            ("bootstrap: soon\n", "bootstrap"),
            ("- a\n- b\n", "mapping"),
            ("channels: [\n", "valid YAML"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(slack_watch.WatchError) as caught:
                    self.load(text)
                self.assertIn(fragment, str(caught.exception))

    def test_relative_and_missing_paths_fail(self):
        with self.assertRaises(slack_watch.WatchError):
            slack_watch.load("watch.yaml")
        with self.assertRaises(slack_watch.WatchError):
            slack_watch.load(Path(self.tmp.name) / "absent.yaml")


class RoutineExpansionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "routines").mkdir()
        self.watch = write(self.base, "watch.yaml", WATCH)

    def routine(self, kind="slack", extra="", watch=None):
        write(self.base / "routines", "watch-test.yaml", ROUTINE.format(
            vault=self.base, kind=kind, watch=watch or self.watch, extra=extra,
        ))
        return config.discover(self.base)[0]

    def test_watch_expands_into_legacy_keys(self):
        source = self.routine(extra="  ignore_people: true\n")["source"]
        self.assertEqual(
            source["channels"], ["C0EXAMPLE01", "C0EXAMPLE02", "D0EXAMPLE03"]
        )
        self.assertTrue(source["include_mentions"])
        self.assertEqual(source["owner_action_channels"], ["C0EXAMPLE01"])
        self.assertNotIn("bootstrap", source)
        self.assertEqual(config.validate(self.routine(
            extra="  ignore_people: true\n"
        )), [])

    def test_kind_slack_rejects_people_unless_ignored(self):
        problems = config.validate(self.routine())
        self.assertTrue(any("ignore_people" in p for p in problems), problems)

    def test_missing_unknown_and_clashing_watch_files_fail_validation(self):
        problems = config.validate(
            self.routine(watch=self.base / "absent.yaml")
        )
        self.assertTrue(any("cannot read watch file" in p for p in problems))
        write(self.base, "bad.yaml", "chanels: []\n")
        problems = config.validate(self.routine(watch=self.base / "bad.yaml"))
        self.assertTrue(any("unknown key" in p for p in problems), problems)
        problems = config.validate(self.routine(
            extra="  ignore_people: true\n  channels: [C0EXAMPLE09]\n"
        ))
        self.assertTrue(any("cannot be combined" in p for p in problems))

    def test_watch_on_other_kinds_is_rejected(self):
        problems = config.validate(self.routine(kind="gchat"))
        self.assertTrue(
            any("watch is supported only for slack" in p for p in problems)
        )

    def test_watch_candidates_match_inline_keys(self):
        watched = self.routine(extra="  ignore_people: true\n")["source"]
        inline = {
            "kind": "slack",
            "channels": ["C0EXAMPLE01", "C0EXAMPLE02", "D0EXAMPLE03"],
            "include_mentions": True,
            "owner_action_channels": ["C0EXAMPLE01"],
        }
        history = {"ok": True, "messages": [{
            "source_id": "slack:C0EXAMPLE01:1784700404.0",
            "ts": "1784700404.0", "text": "root", "reply_count": 0,
        }]}

        def cli(args, timeout=60):
            return {"ok": True, "mentions": []} if args[0] == "mentions" \
                else history

        results = []
        for source in (watched, inline):
            with mock.patch.object(slack_source, "_cli", side_effect=cli):
                results.append(slack_source.candidates(dict(source)))
        self.assertEqual(results[0], results[1])
        self.assertTrue(results[0])


class ThreadItemTest(unittest.TestCase):
    def test_item_uses_given_self_id_without_auth_lookup(self):
        candidate = {
            "id": "slack:C0EXAMPLE01:1784700404.0@1784700410.0",
            "title": "root",
            "raw": {
                "channel": "C0EXAMPLE01", "anchor": "1784700404.0",
                "source_id": "slack:C0EXAMPLE01:1784700404.0",
            },
        }
        messages = [
            {"ts": "1784700404.0", "user": "U0EXAMPLEA", "text": "root"},
            {"ts": "1784700410.0", "user": "U0EXAMPLEB",
             "text": "ping <@U0EXAMPLEA|Example A>"},
        ]
        with mock.patch.object(slack_source, "_cli") as cli:
            item = slack_source.thread_item(
                candidate, messages, {"U0EXAMPLEA": "Example A"},
                {"owner_action_channels": ["C0EXAMPLE01"]},
                self_id="U0EXAMPLEA",
            )
        cli.assert_not_called()
        self.assertEqual(
            item["frontmatter"]["slack_owner_evidence"],
            ["channel-rule", "authored", "mentioned"],
        )
        self.assertEqual(item["source_id"], "slack:C0EXAMPLE01:1784700404.0")
        self.assertEqual(item["frontmatter"]["source_people"], [])
        self.assertEqual(
            item["frontmatter"]["slack_participants"], ["Example A"]
        )
        self.assertIn("ping", item["body"])


if __name__ == "__main__":
    unittest.main()
