"""Capture event stream: schema, ids, rotation, append safety."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import events

KEYS = [
    "v", "id", "ts", "source", "kind", "routine", "memory_id", "source_id",
    "title", "people",
]


def make(**over):
    args = dict(
        routine_id="slack-to-memory", kind="new", memory_id="2026-10-26-t",
        source_id="slack:C1:1.0", title="T", people=["jane-doe"],
        ts="2026-10-26T10:00:00Z",
    )
    args.update(over)
    return events.build(**args)


class BuildTest(unittest.TestCase):
    def test_schema_and_key_order(self):
        event = make()
        self.assertEqual(list(event), KEYS)
        self.assertEqual(event["v"], 1)
        self.assertEqual(event["ts"], "2026-10-26T10:00:00Z")
        self.assertEqual(event["routine"], "slack-to-memory")
        self.assertEqual(event["people"], ["jane-doe"])

    def test_default_ts_is_utc_iso(self):
        self.assertRegex(
            make(ts=None)["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
        )

    def test_id_is_stable_16_hex(self):
        self.assertEqual(make()["id"], make()["id"])
        self.assertRegex(make()["id"], r"^[0-9a-f]{16}$")

    def test_id_changes_with_inputs(self):
        base = make()["id"]
        self.assertNotEqual(base, make(kind="update")["id"])
        self.assertNotEqual(base, make(memory_id="other")["id"])
        self.assertNotEqual(base, make(source_id="slack:C1:2.0")["id"])
        self.assertNotEqual(base, make(ts="2026-10-26T10:00:01Z")["id"])

    def test_null_memory_id_is_kept(self):
        self.assertIsNone(make(memory_id=None)["memory_id"])

    def test_source_derivation(self):
        self.assertEqual(events.source_of("slack:C1:1.0"), "slack")
        self.assertEqual(events.source_of("gmail:abc"), "gmail")
        self.assertEqual(events.source_of("mila:2026-10-26:x"), "mila")
        self.assertEqual(events.source_of("noprefix"), "unknown")
        self.assertEqual(events.source_of(":x"), "unknown")
        self.assertEqual(events.source_of(""), "unknown")
        self.assertEqual(events.source_of(None), "unknown")


class EmitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.file = Path(self.base) / "state" / "events.jsonl"

    def lines(self, file=None):
        return (file or self.file).read_text().splitlines()

    def test_two_emits_make_two_valid_lines(self):
        events.emit(self.base, make())
        events.emit(self.base, make(kind="update", ts="2026-10-26T10:01:00Z"))
        rows = [json.loads(line) for line in self.lines()]
        self.assertEqual([r["kind"] for r in rows], ["new", "update"])
        self.assertEqual(list(rows[0]), KEYS)

    def test_files_are_private(self):
        events.emit(self.base, make())
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.file.parent / "events.lock").exists())

    def test_rotates_at_threshold_and_overwrites_previous(self):
        with mock.patch.object(events, "MAX_BYTES", 50):
            events.emit(self.base, make(title="first"))
            events.emit(self.base, make(title="second"))
            events.emit(self.base, make(title="third"))
        rotated = self.file.with_name("events.jsonl.1")
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(json.loads(self.lines()[0])["title"], "third")
        self.assertEqual(json.loads(self.lines(rotated)[0])["title"], "second")

    def test_no_rotation_below_threshold(self):
        events.emit(self.base, make())
        events.emit(self.base, make(kind="update"))
        self.assertFalse(self.file.with_name("events.jsonl.1").exists())

    def test_oserror_is_swallowed_and_logged(self):
        blocker = Path(self.base) / "file"
        blocker.write_text("x")  # state dir cannot be created under a file
        with mock.patch.object(events, "log") as log:
            events.emit(str(blocker), make())
        self.assertIn("WARN", log.call_args.args[0])

    def test_unserializable_event_is_swallowed(self):
        with mock.patch.object(events, "log") as log:
            events.emit(self.base, {"people": {1, 2}})
        log.assert_called_once()
        self.assertFalse(self.file.exists())


if __name__ == "__main__":
    unittest.main()
