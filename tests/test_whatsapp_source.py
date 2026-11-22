"""WhatsApp forwarder spool -> chat-day memories.

Every fixture is synthetic: invented names (Hebrew and English), invented
chat labels and fake JIDs, laid out exactly as the forwarder's spool format
version 1 describes (batch folders ``b<seq>-<UTC stamp>/batch.json`` plus
``media/wa-<row key>.<ext>``, folders 0700 and files 0600).
"""
import datetime
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import (
    config, llm, memory_sink, runner, state, triviality, whatsapp_source,
)

ZONE = datetime.timezone(datetime.timedelta(hours=2))
GROUP_JID = "120363000000000001@g.us"
DIRECT_JID = "15550000001@s.whatsapp.net"
MEMBER_JID = "100000000000001@lid"
GROUP_LABEL = "Example class group"
DIRECT_LABEL = "Example Contact"
PARENT = "פלוני אלמוני"
OTHER_PARENT = "Avi Example"
HEBREW_TEXT = "מחר יום ספורט, מצורף לוח הזמנים\nשורה שנייה"
DAY = "2026-11-22"


def iso(hour, minute=0, day=22):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=ZONE)


def message_id(seed):
    return "wa1-" + hashlib.sha256(str(seed).encode()).hexdigest()[:40]


def record(seed, when, text="", *, chat_key=1265, chat_jid=GROUP_JID,
           label=GROUP_LABEL, chat_type="group", sender=PARENT,
           sender_jid=MEMBER_JID, from_me=False, kind="text", media=None,
           update=None, row_key=None):
    return {
        "id": message_id(seed),
        "id_source": "stanza",
        "update": update,
        "backfill": False,
        "row_key": row_key or 150000 + int(
            hashlib.sha1(str(seed).encode()).hexdigest()[:3], 16),
        "stanza_id": "3EB0" + hashlib.sha1(str(seed).encode()).hexdigest()[:16].upper(),
        "chat_key": chat_key,
        "chat_jid": chat_jid,
        "chat_label": label,
        "chat_type": chat_type,
        "entry": 1,
        "sender": "Me" if from_me else sender,
        "sender_jid": None if from_me else sender_jid,
        "from_me": from_me,
        "timestamp": when.isoformat(),
        "epoch": int(when.timestamp()),
        "type": kind,
        "text": text,
        "media": media,
    }


class Spool:
    """Writes batches the way the forwarder does."""

    def __init__(self, root):
        self.dir = Path(root) / "spool"
        self.dir.mkdir(mode=0o700)
        os.chmod(self.dir, 0o700)
        self.seq = 40

    def batch(self, created, messages, files=None):
        self.seq += 1
        stamp = created.astimezone(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        name = f"b{self.seq:010d}-{stamp}"
        folder = self.dir / name
        folder.mkdir(mode=0o700)
        (folder / "media").mkdir(mode=0o700)
        for message in messages:
            media = message.get("media")
            if media and media.get("path") == "AUTO":
                media["path"] = f"{name}/media/wa-{message['row_key']}{media.pop('ext')}"
        for filename, data in (files or {}).items():
            path = folder / "media" / filename
            path.write_bytes(data)
            os.chmod(path, 0o600)
        payload = {
            "format": "whatsapp-forwarder-spool",
            "version": 1,
            "batch": name,
            "seq": self.seq,
            "created": created.isoformat(),
            "created_epoch": int(created.timestamp()),
            "timezone": "Asia/Jerusalem",
            "messages": messages,
        }
        target = folder / "batch.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                          encoding="utf-8")
        os.chmod(target, 0o600)
        return name

    def media_message(self, seed, when, kind, data, ext, *, status="copied",
                      original_name=None, mime=None, text="", **kwargs):
        row = 160000 + int(hashlib.sha1(str(seed).encode()).hexdigest()[:4], 16)
        media = {
            "kind": kind, "status": status,
            "path": "AUTO" if status == "copied" else None,
            "ext": ext, "original_name": original_name, "mime": mime,
            "size": len(data) if data is not None else None,
        }
        if status != "copied":
            media.pop("ext")
        message = record(seed, when, text, kind=kind, media=media,
                         row_key=row, **kwargs)
        files = {f"wa-{row}{ext}": data} if status == "copied" else {}
        return message, files


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spool = Spool(self.root)
        self.archive = self.root / "archive"
        self.now = iso(23, 0).timestamp()
        patcher = mock.patch.object(
            whatsapp_source, "_now", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        log_patch = mock.patch.object(
            whatsapp_source, "log", side_effect=self.logs.append)
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def source(self, **overrides):
        value = {
            "kind": "whatsapp",
            "spool_dir": str(self.spool.dir),
            "archive_dir": str(self.archive),
            "quiet_minutes": 30,
            "max_wait_hours": 6,
            "max_results": 0,
        }
        value.update(overrides)
        return value

    def listing(self, processed=None, **overrides):
        source = self.source(**overrides)
        if processed is not None:
            source["_processed"] = processed
        return whatsapp_source.candidates(source)

    def ready(self, found):
        return [c for c in found if not c["raw"].get("_deferred")]


class GroupingAndIdentityTest(Base):
    def test_one_candidate_per_chat_and_local_day_with_opaque_identity(self):
        self.spool.batch(iso(9, 0), [
            record("a", iso(8, 12), HEBREW_TEXT),
            record("b", iso(8, 15), "Thanks!", from_me=True),
            record("c", iso(8, 30), "See you", chat_key=2001,
                   chat_jid=DIRECT_JID, label=DIRECT_LABEL, chat_type="direct",
                   sender=DIRECT_LABEL, sender_jid=DIRECT_JID),
            # 00:30 local on the 23rd is still the 22nd in UTC: the local
            # day decides.
            record("d", iso(0, 30, day=23), "late night"),
        ])
        found = self.listing()
        ids = sorted(c["raw"]["source_id"] for c in found)
        self.assertEqual(ids, [
            "whatsapp:chat-1265:2026-11-22",
            "whatsapp:chat-1265:2026-11-23",
            "whatsapp:chat-2001:2026-11-22",
        ])
        for candidate in found:
            self.assertRegex(
                candidate["id"],
                r"^whatsapp:chat-\d+:\d{4}-\d{2}-\d{2}@[0-9a-f]{16}$")
            self.assertNotIn(GROUP_LABEL, candidate["title"])
        # No phone number or JID anywhere in the archive, names or content.
        for path in self.archive.rglob("*"):
            for jid in (GROUP_JID, DIRECT_JID, MEMBER_JID):
                self.assertNotIn(jid.split("@")[0], str(path))
                if path.is_file():
                    self.assertNotIn(jid.split("@")[0], path.read_text())

    def test_later_batch_updates_same_identity_with_new_candidate_id(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 12), "first")])
        first = self.listing()
        self.spool.batch(iso(10, 0), [record("b", iso(9, 50), "second")])
        second = self.listing()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]["raw"]["source_id"],
                         second[0]["raw"]["source_id"])
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(len(second[0]["raw"]["_day"]["messages"]), 2)

    def test_cursor_resumes_after_last_finished_batch(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 12), "first")])
        self.listing()
        self.logs.clear()
        self.listing()
        self.assertFalse(any("batches=" in line for line in self.logs))
        cursor = next(self.archive.glob("spool-cursor-*.json"))
        self.assertTrue(json.loads(cursor.read_text())["last_batch"]
                        .startswith("b0000000041-"))

    def test_staging_and_foreign_names_are_ignored(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 12), "first")])
        staging = self.spool.dir / ".staging-0000000099-abc"
        staging.mkdir()
        (staging / "batch.json").write_text("{not json")
        (self.spool.dir / "notes.txt").write_text("x")
        self.assertEqual(len(self.listing()), 1)

    def test_filters_by_chat_key_and_label(self):
        self.spool.batch(iso(9, 0), [
            record("a", iso(8, 12), "group"),
            record("c", iso(8, 30), "direct", chat_key=2001,
                   chat_jid=DIRECT_JID, label=DIRECT_LABEL, chat_type="direct"),
        ])
        found = self.listing(exclude_chat_labels=[DIRECT_LABEL])
        self.assertEqual([c["raw"]["chat_key"] for c in found], [1265])
        self.assertFalse((self.archive / "chats" / "chat-2001").exists())

    def test_unsupported_version_fails_loudly_and_keeps_position(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 12), "ok")])
        name = self.spool.batch(iso(10, 0), [record("b", iso(9, 12), "new")])
        path = self.spool.dir / name / "batch.json"
        payload = json.loads(path.read_text())
        payload["version"] = 2
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(RuntimeError, "format version 2"):
            self.listing()
        cursor = json.loads(
            next(self.archive.glob("spool-cursor-*.json")).read_text())
        # The good first batch is kept; the bad one is read again next run.
        self.assertTrue(cursor["last_batch"].startswith("b0000000041-"))

    def test_invalid_records_are_counted_not_ingested(self):
        bad_id = record("x", iso(8, 0), "bad id")
        bad_id["id"] = "../../etc"
        no_epoch = record("y", iso(8, 1), "no epoch")
        no_epoch["epoch"] = "soon"
        self.spool.batch(iso(9, 0), [bad_id, no_epoch,
                                     record("a", iso(8, 12), "ok")])
        found = self.listing()
        self.assertEqual(len(found[0]["raw"]["_day"]["messages"]), 1)
        self.assertTrue(any("invalid=2" in line for line in self.logs))


class QuietPeriodTest(Base):
    def test_recent_change_waits_for_quiet_period(self):
        self.spool.batch(iso(22, 50), [record("a", iso(22, 45), "still talking")])
        found = self.listing()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["raw"]["_deferred"])
        self.now = iso(23, 21).timestamp()
        self.assertEqual(len(self.ready(self.listing())), 1)

    def test_max_wait_releases_an_active_chat(self):
        for hour in range(16, 23):
            self.spool.batch(iso(hour, 50), [
                record(f"m{hour}", iso(hour, 45), f"message {hour}")])
        # Last change at 22:50: not quiet, but the first uncaptured change
        # (16:50) has waited more than six hours.
        self.assertEqual(len(self.ready(self.listing())), 1)

    def test_max_wait_counts_from_the_last_capture(self):
        self.spool.batch(iso(10, 0), [record("a", iso(9, 55), "morning")])
        first = self.listing()
        processed = {first[0]["id"]: {
            "rule_id": "r", "source_kind": "whatsapp",
            "source_id": first[0]["raw"]["source_id"],
            "processed_at": "2026-11-22T09:00:00Z",  # 11:00 local
        }}
        for minute in (0, 20, 40):
            self.spool.batch(iso(22, minute + 5), [
                record(f"e{minute}", iso(22, minute), "evening")])
        # Changes since the capture start at 22:05: neither quiet (last at
        # 22:45) nor six hours old.
        found = self.listing(processed=processed)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["raw"]["_deferred"])

    def test_settled_chat_day_is_not_listed_again(self):
        self.spool.batch(iso(10, 0), [record("a", iso(9, 55), "morning")])
        first = self.listing()
        processed = {first[0]["id"]: {
            "source_kind": "whatsapp", "source_id": first[0]["raw"]["source_id"],
            "processed_at": "2026-11-22T20:00:00Z", "memory": "created",
        }}
        self.assertEqual(self.listing(processed=processed), [])
        processed[first[0]["id"]]["memory_error"] = "store down"
        again = self.listing(processed=processed)
        self.assertEqual(len(self.ready(again)), 1)

    def test_max_results_defers_the_rest_without_dropping_them(self):
        self.spool.batch(iso(9, 0), [
            record("a", iso(8, 0), "a"),
            record("b", iso(8, 0), "b", chat_key=2001, chat_jid=DIRECT_JID,
                   label=DIRECT_LABEL, chat_type="direct"),
        ])
        found = self.listing(max_results=1)
        self.assertEqual(len(found), 2)
        self.assertEqual(len(self.ready(found)), 1)


class MediaTest(Base):
    def file_mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_copied_media_is_archived_privately_by_date_and_message_id(self):
        message, files = self.spool.media_message(
            "img", iso(8, 12), "image", b"\xff\xd8jpeg-bytes", ".jpg",
            mime="image/jpeg", text="schedule")
        self.spool.batch(iso(9, 0), [message], files)
        found = self.listing()
        stored = self.archive / "media" / DAY / f"{message_id('img')}.jpg"
        self.assertEqual(stored.read_bytes(), b"\xff\xd8jpeg-bytes")
        self.assertEqual(self.file_mode(stored), 0o600)
        for folder in (self.archive, self.archive / "media",
                       self.archive / "media" / DAY, self.archive / "chats"):
            self.assertEqual(self.file_mode(folder), 0o700)
        for path in self.archive.rglob("*.json"):
            self.assertEqual(self.file_mode(path), 0o600)
        record_ = found[0]["raw"]["_day"]["messages"][message_id("img")]
        self.assertEqual(record_["stored_path"],
                         f"media/{DAY}/{message_id('img')}.jpg")

    def test_reingest_is_idempotent(self):
        message, files = self.spool.media_message(
            "img", iso(8, 12), "image", b"bytes", ".jpg")
        self.spool.batch(iso(9, 0), [message], files)
        first = self.listing()
        stored = self.archive / "media" / DAY / f"{message_id('img')}.jpg"
        before = os.stat(stored)
        for cursor in self.archive.glob("spool-cursor-*.json"):
            cursor.unlink()  # as if the cursor write had been lost
        self.logs.clear()
        second = self.listing()
        after = os.stat(stored)
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertTrue(any("duplicates=1" in line for line in self.logs))

    def test_update_media_record_adds_the_late_file_to_the_same_message(self):
        waiting, _ = self.spool.media_message(
            "voice", iso(8, 12), "audio", None, ".opus", status="not_downloaded",
            mime="audio/ogg")
        self.spool.batch(iso(9, 0), [waiting])
        first = self.listing()
        late, files = self.spool.media_message(
            "voice", iso(8, 12), "audio", b"OggS-audio", ".opus",
            mime="audio/ogg", update="media")
        self.spool.batch(iso(11, 0), [late], files)
        second = self.listing()
        self.assertEqual(first[0]["raw"]["source_id"],
                         second[0]["raw"]["source_id"])
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        messages = second[0]["raw"]["_day"]["messages"]
        self.assertEqual(list(messages), [message_id("voice")])
        self.assertEqual(messages[message_id("voice")]["media"]["status"],
                         "copied")
        self.assertTrue(messages[message_id("voice")]["stored_path"])

    def test_partial_update_media_record_keeps_the_original_text(self):
        waiting, _ = self.spool.media_message(
            "doc", iso(8, 12), "document", None, ".pdf",
            status="not_downloaded", text=HEBREW_TEXT)
        self.spool.batch(iso(9, 0), [waiting])
        self.listing()
        late, files = self.spool.media_message(
            "doc", iso(8, 12), "document", b"%PDF", ".pdf", update="media")
        late["text"] = ""
        late["sender"] = ""
        self.spool.batch(iso(11, 0), [late], files)
        found = self.listing()
        kept = found[0]["raw"]["_day"]["messages"][message_id("doc")]
        self.assertEqual(kept["text"], HEBREW_TEXT)
        self.assertEqual(kept["sender"], PARENT)
        self.assertTrue(kept["stored_path"])

    def test_resent_duplicate_does_not_restart_the_quiet_period(self):
        message, files = self.spool.media_message(
            "img", iso(8, 12), "image", b"bytes", ".jpg")
        self.spool.batch(iso(9, 0), [message], files)
        first = self.listing()
        again, files = self.spool.media_message(
            "img", iso(8, 12), "image", b"bytes", ".jpg")
        again["backfill"] = True
        self.spool.batch(iso(22, 55), [again], files)  # a --resend
        second = self.listing()
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertFalse(second[0]["raw"].get("_deferred"))

    def test_statuses_without_a_file_are_recorded_not_copied(self):
        messages = []
        for status in ("not_selected", "too_large", "not_downloaded",
                       "unreadable", "blocked_type"):
            message, _ = self.spool.media_message(
                status, iso(8, 0), "document", None, ".bin", status=status)
            messages.append(message)
        self.spool.batch(iso(9, 0), messages)
        found = self.listing()
        self.assertFalse((self.archive / "media").exists())
        item = whatsapp_source.fetch({}, self.source(), found[0])
        for status in ("not_selected", "too_large", "not_downloaded",
                       "unreadable", "blocked_type"):
            self.assertIn(f"not copied by the forwarder ({status})",
                          item["memory_appendix"])

    def test_file_and_total_size_limits(self):
        big, big_files = self.spool.media_message(
            "big", iso(8, 0), "video", b"v" * (1024 * 1024 + 1), ".mp4")
        small1, files1 = self.spool.media_message(
            "s1", iso(8, 1), "image", b"a" * 700 * 1024, ".jpg")
        small2, files2 = self.spool.media_message(
            "s2", iso(8, 2), "image", b"b" * 700 * 1024, ".jpg")
        self.spool.batch(iso(9, 0), [big, small1, small2],
                         {**big_files, **files1, **files2})
        found = self.listing(max_file_mb=1, max_total_mb=1)
        messages = found[0]["raw"]["_day"]["messages"]
        self.assertEqual(messages[message_id("big")]["store_note"], "too_large")
        self.assertTrue(messages[message_id("s1")]["stored_path"])
        self.assertEqual(messages[message_id("s2")]["store_note"],
                         "archive_full")
        self.assertTrue(any("too_large=1" in line and "archive_full=1" in line
                            for line in self.logs))

    def test_media_path_outside_the_batch_is_refused(self):
        message, files = self.spool.media_message(
            "img", iso(8, 12), "image", b"bytes", ".jpg")
        name = self.spool.batch(iso(9, 0), [message], files)
        payload_path = self.spool.dir / name / "batch.json"
        payload = json.loads(payload_path.read_text())
        payload["messages"][0]["media"]["path"] = "../secret.jpg"
        payload_path.write_text(json.dumps(payload))
        found = self.listing()
        record_ = found[0]["raw"]["_day"]["messages"][message_id("img")]
        self.assertEqual(record_["store_note"], "bad_path")
        self.assertFalse((self.archive / "media").exists())

    def test_failed_copy_is_retried_while_the_spool_has_the_file(self):
        message, files = self.spool.media_message(
            "img", iso(8, 12), "image", b"bytes", ".jpg")
        self.spool.batch(iso(9, 0), [message], files)
        real_copy = whatsapp_source._copy_file
        with mock.patch.object(whatsapp_source, "_copy_file",
                               side_effect=PermissionError("denied")):
            first = self.listing()
        record_ = first[0]["raw"]["_day"]["messages"][message_id("img")]
        self.assertEqual(record_["store_note"], "copy_failed:PermissionError")
        with mock.patch.object(whatsapp_source, "_copy_file",
                               side_effect=real_copy):
            second = self.listing()
        record_ = second[0]["raw"]["_day"]["messages"][message_id("img")]
        self.assertTrue(record_["stored_path"])

    def test_archive_keeps_messages_after_the_spool_prunes_their_batch(self):
        message, files = self.spool.media_message(
            "img", iso(8, 12), "image", b"bytes", ".jpg", text=HEBREW_TEXT)
        first_batch = self.spool.batch(iso(9, 0), [message], files)
        self.listing()
        # The forwarder prunes the first batch; a late record for the same
        # day arrives. The chat-day must not shrink to the late record only.
        import shutil
        shutil.rmtree(self.spool.dir / first_batch)
        self.spool.batch(iso(12, 0), [record("late", iso(11, 0), "later")])
        found = self.listing()
        messages = found[0]["raw"]["_day"]["messages"]
        self.assertEqual(set(messages), {message_id("img"), message_id("late")})
        item = whatsapp_source.fetch({}, self.source(), found[0])
        self.assertIn("  > מחר יום ספורט, מצורף לוח הזמנים", item["memory_appendix"])
        self.assertIn(str(self.archive / "media" / DAY), item["memory_appendix"])


class MediaTextTest(Base):
    def setUp(self):
        super().setUp()
        self.calls = []

    def fake_run(self, outputs):
        def run(cmd, timeout=120, check=True):
            self.calls.append(cmd)
            binary = Path(cmd[0]).name
            if binary == "whisper-cli":
                out = Path(cmd[cmd.index("-of") + 1]).with_suffix(".txt")
                out.write_text(outputs.get("whisper", ""), encoding="utf-8")
            result = mock.Mock(stdout=outputs.get(binary, ""), stderr="")
            return result
        return run

    def tools(self, present=("ffmpeg", "whisper-cli", "pdftotext")):
        return mock.patch.object(
            whatsapp_source, "optional_bin",
            side_effect=lambda name, _env: (
                f"/opt/tools/{name}" if name in present else None))

    def voice_candidate(self, **overrides):
        message, files = self.spool.media_message(
            "voice", iso(8, 12), "audio", b"OggS", ".opus", mime="audio/ogg")
        self.spool.batch(iso(9, 0), [message], files)
        model = self.root / "ggml-model.bin"
        model.write_bytes(b"model")
        source = self.source(transcribe_audio=True,
                             whisper_model=str(model),
                             whisper_language="he", **overrides)
        return source, whatsapp_source.candidates(source)[0]

    def test_voice_note_is_transcribed_locally_and_cached(self):
        source, candidate = self.voice_candidate(whisper_threads=4)
        transcript = "שלום, נפגשים מחר בשמונה"
        with self.tools(), mock.patch.object(
                whatsapp_source, "run",
                side_effect=self.fake_run({"whisper": transcript})):
            item = whatsapp_source.fetch({}, source, candidate)
        ffmpeg, whisper = self.calls
        self.assertEqual(Path(ffmpeg[0]).name, "ffmpeg")
        self.assertIn("16000", ffmpeg)
        self.assertEqual(ffmpeg[ffmpeg.index("-ac") + 1], "1")
        self.assertEqual(whisper[whisper.index("-l") + 1], "he")
        self.assertEqual(whisper[whisper.index("-t") + 1], "4")
        self.assertIn(transcript, item["body"])
        self.assertIn(f"  | {transcript}", item["memory_appendix"])
        cached = next((self.archive / "media" / DAY).glob("*.transcript.txt"))
        self.assertEqual(stat.S_IMODE(os.stat(cached).st_mode), 0o600)
        self.assertEqual(list((self.archive / ".tmp").iterdir()), [])
        self.calls.clear()
        with self.tools(), mock.patch.object(whatsapp_source, "run") as run:
            again = whatsapp_source.fetch({}, source, candidate)
        run.assert_not_called()
        self.assertIn(transcript, again["body"])
        self.assertFalse(any(transcript in line for line in self.logs))

    def test_transcription_failure_degrades_and_is_remembered(self):
        source, candidate = self.voice_candidate()
        with self.tools(), mock.patch.object(
                whatsapp_source, "run",
                side_effect=RuntimeError("command failed: secret words")):
            item = whatsapp_source.fetch({}, source, candidate)
        self.assertIn("transcript: none (convert_failed:RuntimeError)",
                      item["body"])
        self.assertTrue(any("failed=1" in line for line in self.logs))
        self.assertFalse(any("secret words" in line for line in self.logs))
        with self.tools(), mock.patch.object(whatsapp_source, "run") as run:
            whatsapp_source.fetch({}, source, candidate)
        run.assert_not_called()

    def test_missing_tools_or_model_degrade_and_retry_later(self):
        source, candidate = self.voice_candidate()
        with self.tools(present=("ffmpeg",)), \
                mock.patch.object(whatsapp_source, "run") as run:
            item = whatsapp_source.fetch({}, source, candidate)
        run.assert_not_called()
        self.assertIn("transcript: none (tool_missing)", item["body"])
        self.assertEqual(list((self.archive / "media" / DAY).glob("*.failed")), [])
        Path(source["whisper_model"]).unlink()
        with self.tools(), mock.patch.object(whatsapp_source, "run") as run:
            item = whatsapp_source.fetch({}, source, candidate)
        self.assertIn("transcript: none (model_missing)", item["body"])
        run.assert_not_called()

    def test_pdf_excerpt_is_bounded(self):
        message, files = self.spool.media_message(
            "pdf", iso(8, 0), "document", b"%PDF-1.7", ".pdf",
            mime="application/pdf", original_name="schedule.pdf")
        self.spool.batch(iso(9, 0), [message], files)
        source = self.source(pdf_max_chars=200, pdf_max_pages=3)
        candidate = whatsapp_source.candidates(source)[0]
        text = "Line of the schedule. " * 100
        with self.tools(), mock.patch.object(
                whatsapp_source, "run",
                side_effect=self.fake_run({"pdftotext": text})):
            item = whatsapp_source.fetch({}, source, candidate)
        command = self.calls[0]
        self.assertEqual(command[command.index("-l") + 1], "3")
        self.assertIn("PDF text excerpt (truncated):", item["body"])
        self.assertIn('"schedule.pdf"', item["memory_appendix"])
        excerpt = item["body"].split("PDF text excerpt (truncated):")[1]
        self.assertLess(len(excerpt), 260)

    def test_images_get_no_text_extraction_yet(self):
        message, files = self.spool.media_message(
            "img", iso(8, 0), "image", b"jpg", ".jpg")
        self.spool.batch(iso(9, 0), [message], files)
        source = self.source(transcribe_audio=False)
        candidate = whatsapp_source.candidates(source)[0]
        with self.tools(), mock.patch.object(whatsapp_source, "run") as run:
            item = whatsapp_source.fetch({}, source, candidate)
        run.assert_not_called()
        self.assertIn("attachment: image", item["body"])


class BodyTest(Base):
    def test_body_and_verbatim_block_preserve_text_time_sender_and_ids(self):
        message, files = self.spool.media_message(
            "img", iso(8, 12), "image", b"jpg", ".jpg", mime="image/jpeg",
            text=HEBREW_TEXT)
        self.spool.batch(iso(9, 0), [
            message,
            record("b", iso(8, 15), "Thanks!", from_me=True),
            record("c", iso(8, 20), "סיסמה: Hunter2Secret",
                   sender=OTHER_PARENT),
        ], files)
        candidate = self.listing()[0]
        item = whatsapp_source.fetch({}, self.source(), candidate)
        body = item["body"]
        self.assertIn(f"[08:12] {PARENT}: מחר יום ספורט", body)
        self.assertIn("[08:15] Me [owner]: Thanks!", body)
        self.assertNotIn("Hunter2Secret", body)
        self.assertIn("[REDACTED]", body)
        appendix = item["memory_appendix"]
        self.assertIn(
            f"- 2026-11-22T08:12:00+02:00 · {PARENT} · {message_id('img')}",
            appendix)
        self.assertIn("  > מחר יום ספורט, מצורף לוח הזמנים\n  > שורה שנייה",
                      appendix)
        self.assertIn(str(self.archive / "media" / DAY / f"{message_id('img')}.jpg"),
                      appendix)
        self.assertNotIn("Hunter2Secret", appendix)
        self.assertIn(f"- 2026-11-22T08:15:00+02:00 · Me · {message_id('b')} · from_me",
                      appendix)
        self.assertIn("attachment: image · image/jpeg · 3 bytes · stored at", appendix)
        meta = item["frontmatter"]
        self.assertEqual(meta["whatsapp_message_ids"],
                         [message_id("img"), message_id("b"), message_id("c")])
        self.assertEqual(meta["whatsapp_participants"], [PARENT, OTHER_PARENT])
        self.assertEqual(meta["whatsapp_chat_ref"], "chat-1265")
        self.assertEqual(item["source_id"], "whatsapp:chat-1265:2026-11-22")
        self.assertEqual(item["date"], DAY)
        header = "\n".join(llm.source_header_lines(item))
        self.assertIn("Source: WhatsApp", header)
        self.assertIn(f"Participants in supplied messages: {PARENT}, {OTHER_PARENT}",
                      header)

    def test_verbatim_block_parses_back_to_the_original_text(self):
        text = "line one\n\n  indented · with dot\n[08:00] looks like a header: no"
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), text)])
        item = whatsapp_source.fetch({}, self.source(), self.listing()[0])
        lines = item["memory_appendix"].split("\n")
        start = next(i for i, line in enumerate(lines)
                     if line.endswith(message_id("a")))
        quoted = []
        for line in lines[start + 1:]:
            if not line.startswith("  >"):
                break
            quoted.append(line[4:] if line.startswith("  > ") else "")
        self.assertEqual("\n".join(quoted), text)

    def test_unknown_senders_are_not_offered_as_participants(self):
        self.spool.batch(iso(9, 0), [
            record("a", iso(8, 0), "hi", sender="Unknown-1234"),
            record("b", iso(8, 1), "hi", sender="Unknown"),
        ])
        item = whatsapp_source.fetch({}, self.source(), self.listing()[0])
        self.assertEqual(item["frontmatter"]["whatsapp_participants"], [])

    def test_long_day_keeps_newest_lines_in_the_prompt_and_all_in_the_entry(self):
        messages = [record(f"m{i}", iso(8, i % 60), f"message number {i:03d} "
                           + "x" * 80) for i in range(60)]
        self.spool.batch(iso(9, 0), messages)
        item = whatsapp_source.fetch({}, self.source(max_body_chars=2000),
                                     self.listing()[0])
        self.assertLessEqual(len(item["body"]), 2200)
        self.assertIn("earlier line(s) of this day are omitted", item["body"])
        self.assertIn("message number 000", item["memory_appendix"])
        self.assertIn("message number 059", item["memory_appendix"])


class ValidationTest(Base):
    def routine(self, **source):
        return {
            "id": "whatsapp-to-memory",
            "enabled": True,
            "source": self.source(**source),
            "analyze": {"provider": "gemini", "model": "gemini-test",
                        "instruction": "Keep durable facts."},
            "memory": {"store": str(self.root / "store"), "type": "note"},
        }

    def test_valid_source(self):
        self.assertEqual(config.validate(self.routine()), [])

    def test_spool_must_exist_and_be_private(self):
        os.chmod(self.spool.dir, 0o755)
        problems = config.validate(self.routine())
        self.assertTrue(any("accessible by group/others" in p for p in problems))
        problems = config.validate(self.routine(spool_dir=str(self.root / "none")))
        self.assertTrue(any("spool_dir does not exist" in p for p in problems))

    def test_archive_must_stay_out_of_git_store_and_spool(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        problems = config.validate(self.routine(archive_dir=str(repo / "wa")))
        self.assertTrue(any("inside a git work tree" in p for p in problems))
        problems = config.validate(
            self.routine(archive_dir=str(self.root / "store" / "attachments")))
        self.assertTrue(any("outside memory.store" in p for p in problems))
        problems = config.validate(
            self.routine(archive_dir=str(self.spool.dir / "archive")))
        self.assertTrue(any("must not contain each other" in p for p in problems))
        self.archive.mkdir(mode=0o755)
        os.chmod(self.archive, 0o755)
        problems = config.validate(self.routine())
        self.assertTrue(any("archive_dir is accessible" in p for p in problems))

    def test_disabled_routine_skips_filesystem_checks(self):
        routine = self.routine(spool_dir="/absolute/path/to/spool")
        routine["enabled"] = False
        self.assertEqual(config.validate(routine), [])

    def test_field_problems(self):
        problems = config.validate(self.routine(
            quiet_minutes=-1, max_wait_hours=100, transcribe_audio=True,
            whisper_language="Hebrew", chat_keys=["x"], max_file_mb=0,
            copy_media="yes", query="anything", unknown_key=1,
        ))
        text = "\n".join(problems)
        for needle in ("quiet_minutes", "max_wait_hours", "whisper_model",
                       "whisper_language", "chat_keys", "max_file_mb",
                       "copy_media", "query is not supported", "unknown_key"):
            self.assertIn(needle, text)
        problems = config.validate(self.routine(quiet_minutes=600,
                                                max_wait_hours=2))
        self.assertTrue(any("must not exceed max_wait_hours" in p
                            for p in problems))

    def test_two_sources_may_not_share_an_archive(self):
        first, second = self.routine(), self.routine()
        second["id"] = "second"
        problems = config.cross_problems([first, second])
        self.assertIn("whatsapp-to-memory", problems)
        self.assertTrue(any("archive_dir is shared" in p
                            for p in problems["second"]))

    def test_runtime_listing_refuses_an_open_spool(self):
        os.chmod(self.spool.dir, 0o750)
        with self.assertRaisesRegex(RuntimeError, "group/others"):
            self.listing()


class WiringTest(unittest.TestCase):
    def test_registered_everywhere_it_needs_to_be(self):
        self.assertIn("whatsapp", config.VALID_SOURCE_KINDS)
        self.assertIn("whatsapp", runner.SOURCES)
        self.assertEqual(runner._SOURCE_DEFAULT_LIMITS["whatsapp"], 0)
        self.assertIn("whatsapp", memory_sink.CAPTURE_SOURCE_ID_PREFIXES)
        self.assertTrue(memory_sink.OPERATOR_CONFIRMED_SOURCE_ID_RE.fullmatch(
            "whatsapp:chat-1265:2026-11-22"))
        self.assertTrue(triviality.is_chat_kind("whatsapp"))

    def test_small_talk_day_is_trivial_but_an_attachment_is_not(self):
        item = {"source_kind": "whatsapp",
                "body": "[08:15] Me [owner]: Thanks!\n[08:16] Avi Example: תודה"}
        self.assertTrue(triviality.assess(item)[0])
        item["body"] += "\n  attachment: image, image/jpeg, 1 KB, stored"
        self.assertFalse(triviality.assess(item)[0])


class RelatedMemoriesTest(unittest.TestCase):
    def test_earlier_days_of_the_same_chat_are_offered_as_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = Path(tmp) / "memory" / "entries"
            entries.mkdir(parents=True)
            today = datetime.date.today()
            for name, source_id in (
                ("same-chat", f"whatsapp:chat-1265:{today - datetime.timedelta(days=1)}"),
                ("this-day", f"whatsapp:chat-1265:{today}"),
                ("other-chat", f"whatsapp:chat-2001:{today}"),
            ):
                (entries / f"{name}.md").write_text(
                    f"---\nid: {name}\ndate: {today}\ntype: note\n"
                    f"title: {name}\nsource_ids: [{source_id}]\n---\nbody\n")
            routine = {"id": "r", "memory": {"store": tmp}}
            item = {"id": "x", "source_id": f"whatsapp:chat-1265:{today}",
                    "frontmatter": {"whatsapp_chat_ref": "chat-1265"}}
            with mock.patch.object(runner, "log"):
                runner._attach_related_memories(
                    routine, {"kind": "whatsapp"}, item)
            related = item["frontmatter"]["related_memory_entries"]
            self.assertEqual([entry["id"] for entry in related], ["same-chat"])


class FakeResult:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class EndToEndTest(Base):
    """Spool -> listing -> fetch -> prompt -> memory sink, LLM and CLI faked."""

    def setUp(self):
        super().setUp()
        self.base = self.root / "daemon"
        self.base.mkdir()
        self.store = self.root / "store"
        (self.store / "memory").mkdir(parents=True)
        memory_sink._slug_cache.clear()
        memory_sink._slug_cache[str(self.store)] = {"known-parent"}
        self.addCleanup(memory_sink._slug_cache.clear)
        self.routine = {
            "id": "whatsapp-to-memory",
            "enabled": True,
            "source": self.source(),
            "analyze": {"provider": "gemini", "model": "gemini-test",
                        "instruction": "Keep durable facts."},
            "memory": {"store": str(self.store), "type": "note",
                       "tags": ["whatsapp"]},
        }
        message, files = self.spool.media_message(
            "img", iso(8, 12), "image", b"jpg", ".jpg", mime="image/jpeg",
            text=HEBREW_TEXT)
        self.first_batch = self.spool.batch(iso(9, 0), [
            message, record("b", iso(8, 15), "Thanks!", from_me=True)], files)
        self.cli_calls = []
        self.prompts = []
        self.all_logs = []

    def fake_cli(self, store, args, stdin_text=None, timeout=120):
        self.cli_calls.append((args, stdin_text))
        return FakeResult("✓ created 2026-11-22-sports-day\n")

    def run_daemon(self, analyze=None, dry_run=False):
        identity = {
            "email": "owner@example.com",
            "person": {"email": "owner@example.com", "name": "Memory Owner",
                       "slug": "memory-owner"},
            "safe": True,
        }
        extraction = {
            "worthy": True, "owner_attention": False, "type": "event",
            "title": "Sports day tomorrow", "people": ["known-parent",
                                                        "invented-person"],
            "tags": ["school"], "follows": [],
            "body": "Sports day is tomorrow; the schedule was shared.",
        }

        def default_analyze(_routine, prompt, model=None):
            self.prompts.append(prompt)
            return "Sports day is tomorrow."

        def capture_log(message):
            self.all_logs.append(message)

        patches = [
            mock.patch.object(llm, "analyze", side_effect=analyze or default_analyze),
            mock.patch.object(memory_sink, "_cli", side_effect=self.fake_cli),
            mock.patch.object(memory_sink, "_commit_store"),
            mock.patch.object(memory_sink, "_authenticated_identity",
                              return_value=identity),
            mock.patch.object(memory_sink, "_extract", return_value=extraction),
            mock.patch.object(runner, "log", side_effect=capture_log),
            mock.patch.object(memory_sink, "log", side_effect=capture_log),
            mock.patch.object(whatsapp_source, "log", side_effect=capture_log),
        ]
        for patch in patches:
            patch.start()
        try:
            return runner.run(self.base, [self.routine], dry_run=dry_run)
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_chat_day_reaches_the_store_with_verbatim_block(self):
        totals = self.run_daemon()
        self.assertEqual(totals["errors"], 0)
        self.assertEqual(totals["processed"], 1)
        prompt = self.prompts[0]
        self.assertIn("Source: WhatsApp", prompt)
        self.assertIn(f"[08:12] {PARENT}: מחר יום ספורט", prompt)
        adds = [(a, s) for a, s in self.cli_calls if a[0] == "add"]
        self.assertEqual(len(adds), 1)
        args, stdin = adds[0]
        self.assertEqual(args[args.index("--source-ids") + 1],
                         "whatsapp:chat-1265:2026-11-22")
        self.assertEqual(args[args.index("--date") + 1], DAY)
        # Unknown people are dropped by slug validation, never minted.
        self.assertEqual(args[args.index("--people") + 1], "known-parent")
        self.assertIn("people-unmapped", args[args.index("--tags") + 1])
        self.assertTrue(stdin.startswith(
            "Sports day is tomorrow; the schedule was shared."))
        self.assertIn("## WhatsApp messages (verbatim)", stdin)
        self.assertIn(f"- 2026-11-22T08:12:00+02:00 · {PARENT} · "
                      f"{message_id('img')}", stdin)
        self.assertIn("  > מחר יום ספורט, מצורף לוח הזמנים", stdin)
        self.assertIn(str(self.archive / "media" / DAY / f"{message_id('img')}.jpg"),
                      stdin)
        ledger = state.load(self.base)
        (entry,) = ledger.values()
        self.assertEqual(entry["source_kind"], "whatsapp")
        self.assertEqual(entry["memory_source_id"],
                         "whatsapp:chat-1265:2026-11-22")
        # Settled: the next run lists nothing new for this day.
        self.cli_calls.clear()
        totals = self.run_daemon()
        self.assertEqual(totals["processed"], 0)
        self.assertEqual(self.cli_calls, [])

    def test_no_personal_data_in_logs(self):
        self.run_daemon()
        joined = "\n".join(self.all_logs)
        self.assertTrue(joined)
        for secret in (PARENT, "מחר", "Thanks!", GROUP_LABEL, GROUP_JID,
                       MEMBER_JID, "15550000001", "120363000000000001",
                       "100000000000001", "Sports day"):
            self.assertNotIn(secret, joined)

    def test_crash_between_copy_and_ledger_keeps_the_file(self):
        def broken(_routine, _prompt, model=None):
            raise RuntimeError("provider down")

        totals = self.run_daemon(analyze=broken)
        self.assertEqual(totals["errors"], 1)
        self.assertEqual(state.load(self.base), {})
        stored = self.archive / "media" / DAY / f"{message_id('img')}.jpg"
        self.assertTrue(stored.is_file())
        # The forwarder prunes the batch before the next successful run.
        import shutil
        shutil.rmtree(self.spool.dir / self.first_batch)
        totals = self.run_daemon()
        self.assertEqual(totals["processed"], 1)
        (args, stdin), = [c for c in self.cli_calls if c[0][0] == "add"]
        self.assertIn(str(stored), stdin)
        self.assertIn("  > מחר יום ספורט", stdin)

    def test_dry_run_writes_and_copies_nothing(self):
        totals = self.run_daemon(dry_run=True)
        self.assertEqual(totals["errors"], 0)
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.cli_calls, [])
        self.assertFalse((self.base / "state" / "processed.json").exists())


if __name__ == "__main__":
    unittest.main()
