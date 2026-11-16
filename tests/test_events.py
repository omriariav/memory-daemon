"""Capture event stream: schema, ids, rotation, append safety."""
import json
import os
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

    def test_calendar_details_are_kept_when_valid(self):
        event = make(details={"calendar_event_id": "e1", "attendee_count": 2})
        self.assertEqual(
            event["details"], {"calendar_event_id": "e1", "attendee_count": 2})
        two = make(details={"attendee_count": 2,
                            "other_attendee": "a@example.com"})
        self.assertEqual(two["details"]["other_attendee"], "a@example.com")
        zero = make(details={"attendee_count": 0})
        self.assertEqual(zero["details"], {"attendee_count": 0})

    def test_invalid_calendar_details_are_dropped(self):
        for bad in ({"attendee_count": -1}, {"attendee_count": "3"},
                    {"attendee_count": True}, {"attendee_count": 2.0},
                    {"calendar_event_id": 5}, {"calendar_event_id": ""},
                    {"calendar_event_id": "x" * 300},
                    {"other_attendee": 5}, {"other_attendee": "no-at-sign"},
                    {"other_attendee": "a@" + "x" * 260}):
            self.assertNotIn("details", make(details=bad), bad)
        mixed = make(details={"calendar_event_id": "e1", "attendee_count": -4})
        self.assertEqual(mixed["details"], {"calendar_event_id": "e1"})

    def test_gmail_details_are_kept_when_valid(self):
        event = make(details={"gmail_labels": ["Save", "Reports"],
                              "saved_request": True})
        self.assertEqual(event["details"], {
            "gmail_labels": ["Reports", "Save"], "saved_request": True})

    def test_invalid_gmail_details_are_dropped_or_capped(self):
        for bad in ({"gmail_labels": []}, {"gmail_labels": "Save"},
                    {"gmail_labels": [5, ""]}, {"saved_request": False},
                    {"saved_request": "yes"}):
            self.assertNotIn("details", make(details=bad), bad)
        many = make(details={"gmail_labels": [f"L{i:02d}" for i in range(40)]})
        self.assertEqual(len(many["details"]["gmail_labels"]), 20)
        self.assertNotIn("details", make(details={"gmail_labels": [" ", "\n"]}))

    def test_gmail_labels_are_cleaned_deduped_and_sorted(self):
        event = make(details={"gmail_labels": [
            "b  label", "a", "a", "  ", "x" * 150, "b label"]})
        self.assertEqual(event["details"]["gmail_labels"],
                         ["a", "b label", "x" * 100])

    def test_mail_display_details_are_kept_cleaned_and_capped(self):
        event = make(details={
            "mail_from": "Ann  Example <ann@example.com>",
            "mail_subject": "Plan\nfor  May " + "x" * 300,
            "mail_snippet": "Short\tsnippet " + "y" * 400,
            "mail_thread_id": "18c0ffee12345678"})
        d = event["details"]
        self.assertEqual(d["mail_from"], "Ann Example")
        self.assertNotIn("\n", d["mail_subject"])
        self.assertEqual((len(d["mail_subject"]), len(d["mail_snippet"])),
                         (200, 300))
        self.assertTrue(d["mail_snippet"].startswith("Short snippet yyy"))
        self.assertEqual(d["mail_thread_id"], "18c0ffee12345678")

    def test_mail_from_is_a_display_name_never_an_address(self):
        for raw, want in (("Ann Example <ann@example.com>", "Ann Example"),
                          ('"Ann, Example" <ann@example.com>', "Ann, Example"),
                          ("x" * 120, "x" * 80)):
            self.assertEqual(
                make(details={"mail_from": raw})["details"]["mail_from"], want)
        for bad in ("ann@example.com", "<ann@example.com>", "Ann ann@example.com",
                    "", "  ", 5, None, ["a"]):
            self.assertNotIn("details", make(details={"mail_from": bad}), bad)

    def test_bad_mail_fields_are_dropped(self):
        for bad in ({"mail_subject": ""}, {"mail_subject": 4},
                    {"mail_snippet": "\x00\x01"}, {"mail_thread_id": "a b"},
                    {"mail_thread_id": "x" * 101}, {"mail_thread_id": 7},
                    {"mail_thread_id": "bad\n"}):
            self.assertNotIn("details", make(details=bad), bad)

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

    def test_emit_reports_success_and_short_writes(self):
        self.assertTrue(events.emit(self.base, make()))
        real_write = os.write

        def short(fd, data):
            return real_write(fd, data[:3])

        with mock.patch.object(events.os, "write", side_effect=short), \
                mock.patch.object(events, "log"):
            self.assertFalse(events.emit(self.base, make(kind="update")))
        self.assertTrue(events.emit(self.base, make(kind="update")))
        rows = [json.loads(line) for line in self.lines()]
        self.assertEqual([r["kind"] for r in rows], ["new", "update"])

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
