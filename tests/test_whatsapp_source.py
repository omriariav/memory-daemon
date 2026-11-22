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
import shutil
import stat
import subprocess
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


def jid_for(key):
    """The invented chat JID behind a fixture chat key."""
    if key == 1265:
        return GROUP_JID
    if key == 2001:
        return DIRECT_JID
    return f"1203630000{key:08d}@g.us"


def local(key=1265, jid=None):
    """The archive chat id for a fixture chat (the daemon's keyed hash)."""
    ident = whatsapp_source._Identity(whatsapp_source.state_dir(), False)
    return ident.own_id(jid or jid_for(key))


def sid(date=DAY, **kwargs):
    return f"whatsapp:{local(**kwargs)}:{date}"


def chat_id(seed):
    return "wac1-" + hashlib.sha256(f"chat:{seed}".encode()).hexdigest()[:32]


def message_id(seed):
    return "wa1-" + hashlib.sha256(str(seed).encode()).hexdigest()[:40]


def record(seed, when, text="", *, chat_key=1265, chat_jid=None,
           label=GROUP_LABEL, chat_type="group", sender=PARENT,
           sender_jid=MEMBER_JID, from_me=False, kind="text", media=None,
           update=None, row_key=None, chat_id=None):
    return {
        "id": message_id(seed),
        "id_source": "stanza",
        "update": update,
        "backfill": False,
        "row_key": row_key or 150000 + int(
            hashlib.sha1(str(seed).encode()).hexdigest()[:3], 16),
        "stanza_id": "3EB0" + hashlib.sha1(str(seed).encode()).hexdigest()[:16].upper(),
        "chat_key": chat_key,
        "chat_jid": chat_jid or jid_for(chat_key),
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
        **({"chat_id": chat_id} if chat_id else {}),
    }


class Spool:
    """Writes batches the way the forwarder does."""

    def __init__(self, root):
        self.dir = Path(root) / "spool"
        self.dir.mkdir(mode=0o700)
        os.chmod(self.dir, 0o700)
        self.seq = 40

    def batch(self, created, messages, files=None, store_id=None):
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
        if store_id:
            payload["store_id"] = store_id
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
        whatsapp_source._extract_spent = 0.0
        whatsapp_source.configure(self.root / "daemon")
        self.addCleanup(whatsapp_source.configure, None)
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
        self.assertEqual(ids, sorted([
            sid(), sid("2026-11-23"), sid(key=2001),
        ]))
        for candidate in found:
            self.assertRegex(
                candidate["id"],
                r"^whatsapp:c[0-9a-f]{16}:\d{4}-\d{2}-\d{2}@[0-9a-f]{16}$")
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
        self.assertEqual([c["raw"]["chat"] for c in found], [local()])
        self.assertFalse((self.archive / "chats" / local(key=2001)).exists())
        for path in [*self.archive.rglob("*.json"),
                     *whatsapp_source.state_dir().glob("*.json")]:
            self.assertNotIn(DIRECT_LABEL, path.read_text())  # never recorded

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
    """Local text extraction runs at listing time, budgeted and cached."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.model = self.root / "ggml-model.bin"
        self.model.write_bytes(b"model")

    def fake_run(self, outputs, fail=None):
        def run(cmd, timeout=120, check=True):
            self.calls.append(cmd)
            binary = Path(cmd[0]).name
            if fail and binary in fail:
                problem = fail[binary]
                if isinstance(problem, BaseException):
                    raise problem
                return mock.Mock(returncode=problem, stdout="", stderr="")
            if binary == "whisper-cli":
                out = Path(cmd[cmd.index("-of") + 1]).with_suffix(".txt")
                out.write_text(outputs.get("whisper", ""), encoding="utf-8")
            return mock.Mock(returncode=0, stdout=outputs.get(binary, ""),
                             stderr="")
        return run

    def tools(self, present=("ffmpeg", "whisper-cli", "pdftotext")):
        return mock.patch.object(
            whatsapp_source, "optional_bin",
            side_effect=lambda name, _env: (
                f"/opt/tools/{name}" if name in present else None))

    def voice_source(self, **overrides):
        values = dict(transcribe_audio=True, whisper_model=str(self.model),
                      whisper_language="he")
        values.update(overrides)
        return self.source(**values)

    def add_voice(self, seed="voice", when=None):
        message, files = self.spool.media_message(
            seed, when or iso(8, 12), "audio", b"OggS", ".opus",
            mime="audio/ogg")
        self.spool.batch(iso(9, 0), [message], files)

    def listing_with(self, source, run_effect, present=None):
        tools = self.tools() if present is None else self.tools(present)
        with tools, mock.patch.object(whatsapp_source, "run",
                                      side_effect=run_effect):
            return whatsapp_source.candidates(source)

    def test_voice_note_is_transcribed_locally_and_cached(self):
        self.add_voice()
        source = self.voice_source(whisper_threads=4)
        transcript = "שלום, נפגשים מחר בשמונה"
        found = self.listing_with(source, self.fake_run({"whisper": transcript}))
        ffmpeg, whisper = self.calls
        self.assertEqual(Path(ffmpeg[0]).name, "ffmpeg")
        self.assertIn("16000", ffmpeg)
        self.assertEqual(ffmpeg[ffmpeg.index("-ac") + 1], "1")
        self.assertEqual(whisper[whisper.index("-l") + 1], "he")
        self.assertEqual(whisper[whisper.index("-t") + 1], "4")
        item = whatsapp_source.fetch({}, source, found[0])
        self.assertIn(transcript, item["body"])
        self.assertIn(f"  | {transcript}", item["memory_appendix"])
        cached = next((self.archive / "media" / DAY).glob("*.transcript.txt"))
        self.assertEqual(stat.S_IMODE(os.stat(cached).st_mode), 0o600)
        self.assertEqual(list((self.archive / ".tmp").iterdir()), [])
        self.calls.clear()
        with self.tools(), mock.patch.object(whatsapp_source, "run") as run:
            again = whatsapp_source.candidates(source)
            whatsapp_source.fetch({}, source, again[0])
        run.assert_not_called()
        self.assertFalse(any(transcript in line for line in self.logs))

    def test_nonzero_exit_is_deterministic_and_remembered(self):
        self.add_voice()
        source = self.voice_source()
        found = self.listing_with(
            source, self.fake_run({}, fail={"ffmpeg": 1}))
        item = whatsapp_source.fetch({}, source, found[0])
        self.assertIn("transcript: none (convert_failed)", item["body"])
        self.assertTrue(
            next((self.archive / "media" / DAY).glob("*.transcript.failed")))
        self.now += 10 * 86400
        with self.tools(), mock.patch.object(whatsapp_source, "run") as run:
            whatsapp_source.candidates(source)
        run.assert_not_called()

    def test_transient_failures_retry_with_backoff_then_give_up(self):
        self.add_voice()
        source = self.voice_source()
        timeout = subprocess.TimeoutExpired(["whisper-cli"], 300)
        for attempt, error in enumerate(
            (timeout, OSError(24, "Too many open files"), -9), start=1
        ):
            self.calls.clear()
            found = self.listing_with(
                source, self.fake_run({}, fail={"whisper-cli": error}))
            self.assertEqual(len(self.calls), 2, attempt)
            record = found[0]["raw"]["_day"]["messages"][message_id("voice")]
            if attempt < 3:
                self.assertEqual(record["extract"]["state"], "pending")
                # Not retried before the backoff has passed.
                self.calls.clear()
                self.listing_with(source, self.fake_run({}))
                self.assertEqual(self.calls, [])
                self.now += whatsapp_source.EXTRACT_BACKOFF_SECONDS * 2 ** attempt
            else:
                self.assertEqual(record["extract"]["state"], "failed")
                self.assertEqual(record["extract"]["reason"],
                                 "gave_up:transcribe_killed")
        self.assertFalse(
            list((self.archive / "media" / DAY).glob("*.attempts")))

    def test_transient_failure_then_success_updates_the_capture(self):
        self.add_voice()
        source = self.voice_source(max_wait_hours=24)
        timeout = subprocess.TimeoutExpired(["whisper-cli"], 300)
        first = self.listing_with(
            source, self.fake_run({}, fail={"whisper-cli": timeout}))
        # Pending media text holds the capture until max_wait.
        self.assertTrue(first[0]["raw"].get("_deferred"))
        self.now += whatsapp_source.EXTRACT_BACKOFF_SECONDS + 1
        second = self.listing_with(source, self.fake_run({"whisper": "ok now"}))
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        # The finished transcript is a change: the quiet period runs again.
        self.assertTrue(second[0]["raw"].get("_deferred"))
        self.now += 31 * 60
        second = self.listing_with(source, self.fake_run({}))
        self.assertFalse(second[0]["raw"].get("_deferred"))
        item = whatsapp_source.fetch({}, source, second[0])
        self.assertIn("ok now", item["body"])

    def test_extraction_budget_leaves_the_rest_for_the_next_run(self):
        self.add_voice("v1", iso(8, 0))
        self.add_voice("v2", iso(8, 5))
        source = self.voice_source()
        whatsapp_source._extract_spent = 10 ** 6  # used up by other files
        found = self.listing_with(source, self.fake_run({"whisper": "t"}))
        self.assertEqual(self.calls, [])
        states = {r["extract"]["reason"] for r in
                  found[0]["raw"]["_day"]["messages"].values()}
        self.assertEqual(states, {"budget"})
        self.assertTrue(any("deferred=2" in line for line in self.logs))
        whatsapp_source._extract_spent = 0.0
        found = self.listing_with(self.voice_source(),
                                  self.fake_run({"whisper": "t"}))
        states = {r["extract"]["state"] for r in
                  found[0]["raw"]["_day"]["messages"].values()}
        self.assertEqual(states, {"done"})

    def test_missing_tools_or_model_degrade_and_retry_once_installed(self):
        self.add_voice()
        source = self.voice_source()
        found = self.listing_with(source, self.fake_run({}),
                                  present=("ffmpeg",))
        self.assertEqual(self.calls, [])
        item = whatsapp_source.fetch({}, source, found[0])
        self.assertIn("transcript: none (tool_missing)", item["body"])
        self.assertFalse(found[0]["raw"].get("_deferred"))
        self.assertEqual(
            list((self.archive / "media" / DAY).glob("*.failed")), [])
        self.model.unlink()
        self.listing_with(source, self.fake_run({}))
        self.assertEqual(self.calls, [])
        self.model.write_bytes(b"model")
        found = self.listing_with(source, self.fake_run({"whisper": "now"}))
        self.assertEqual(len(self.calls), 2)
        record = found[0]["raw"]["_day"]["messages"][message_id("voice")]
        self.assertEqual(record["extract"]["state"], "done")

    def test_pdf_excerpt_is_bounded(self):
        message, files = self.spool.media_message(
            "pdf", iso(8, 0), "document", b"%PDF-1.7", ".pdf",
            mime="application/pdf", original_name="schedule.pdf")
        self.spool.batch(iso(9, 0), [message], files)
        source = self.source(pdf_max_chars=200, pdf_max_pages=3)
        text = "Line of the schedule. " * 100
        found = self.listing_with(source, self.fake_run({"pdftotext": text}))
        command = self.calls[0]
        self.assertEqual(command[command.index("-l") + 1], "3")
        item = whatsapp_source.fetch({}, source, found[0])
        self.assertIn("PDF text excerpt (truncated):", item["body"])
        self.assertIn('"schedule.pdf"', item["memory_appendix"])
        excerpt = item["body"].split("PDF text excerpt (truncated):")[1]
        self.assertLess(len(excerpt), 260)

    def test_images_get_no_text_extraction_yet(self):
        message, files = self.spool.media_message(
            "img", iso(8, 0), "image", b"jpg", ".jpg")
        self.spool.batch(iso(9, 0), [message], files)
        source = self.source(transcribe_audio=False)
        found = self.listing_with(source, self.fake_run({}))
        self.assertEqual(self.calls, [])
        item = whatsapp_source.fetch({}, source, found[0])
        self.assertIn("attachment: image", item["body"])

    def test_transcript_codes_are_redacted_in_both_views(self):
        self.add_voice()
        source = self.voice_source()
        found = self.listing_with(source, self.fake_run(
            {"whisper": "your verification code\n909090 thanks"}))
        item = whatsapp_source.fetch({}, source, found[0])
        self.assertNotIn("909090", item["body"])
        self.assertNotIn("909090", item["memory_appendix"])

    def test_default_timeout_suits_voice_notes(self):
        self.assertEqual(
            whatsapp_source.DEFAULTS["transcribe_timeout_seconds"], 300)
        self.assertLessEqual(whatsapp_source.PDF_TIMEOUT_SECONDS, 60)


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
        self.assertEqual(meta["whatsapp_chat_ref"], local())
        self.assertEqual(item["source_id"], sid())
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
            archive_keep_days=0, extract_budget_seconds=-1,
            recapture_after_rebuild="no", transcribe_timeout_seconds=7200,
        ))
        text = "\n".join(problems)
        for needle in ("quiet_minutes", "max_wait_hours", "whisper_model",
                       "whisper_language", "chat_keys", "max_file_mb",
                       "copy_media", "query is not supported", "unknown_key",
                       "archive_keep_days", "extract_budget_seconds",
                       "recapture_after_rebuild", "transcribe_timeout_seconds"):
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
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        whatsapp_source.configure(Path(tmp.name))
        self.addCleanup(whatsapp_source.configure, None)

    def test_registered_everywhere_it_needs_to_be(self):
        self.assertIn("whatsapp", config.VALID_SOURCE_KINDS)
        self.assertIn("whatsapp", runner.SOURCES)
        self.assertEqual(runner._SOURCE_DEFAULT_LIMITS["whatsapp"], 0)
        self.assertIn("whatsapp", memory_sink.CAPTURE_SOURCE_ID_PREFIXES)
        self.assertTrue(memory_sink.OPERATOR_CONFIRMED_SOURCE_ID_RE.fullmatch(
            sid()))
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
            whatsapp_source.configure(Path(tmp) / "daemon")
            self.addCleanup(whatsapp_source.configure, None)
            entries = Path(tmp) / "memory" / "entries"
            entries.mkdir(parents=True)
            today = datetime.date.today()
            for name, source_id in (
                ("same-chat", sid(today - datetime.timedelta(days=1))),
                ("this-day", sid(today)),
                ("other-chat", sid(today, key=2001)),
            ):
                (entries / f"{name}.md").write_text(
                    f"---\nid: {name}\ndate: {today}\ntype: note\n"
                    f"title: {name}\nsource_ids: [{source_id}]\n---\nbody\n")
            routine = {"id": "r", "memory": {"store": tmp}}
            item = {"id": "x", "source_id": sid(today),
                    "frontmatter": {"whatsapp_chat_ref": local()}}
            with mock.patch.object(runner, "log"):
                runner._attach_related_memories(
                    routine, {"kind": "whatsapp"}, item)
            related = item["frontmatter"]["related_memory_entries"]
            self.assertEqual([entry["id"] for entry in related], ["same-chat"])


class IndexCrashTest(Base):
    """Finding 1: the index can always be rebuilt from the day files."""

    def env(self, name):
        root = self.root / name
        root.mkdir()
        whatsapp_source.configure(root / "daemon")
        spool = Spool(root)
        return spool, self.source(spool_dir=str(spool.dir),
                                  archive_dir=str(root / "archive"))

    def second_batch(self, spool):
        spool.batch(iso(10, 0), [
            record("a", iso(8, 12), "first"),           # duplicate
            record("b", iso(9, 30), "second"),          # same day, new
            record("c", iso(9, 0, day=23), "other", chat_key=7),  # new day
        ])

    def ids(self, source):
        whatsapp_source.configure(Path(source["archive_dir"]).parent / "daemon")
        return {(c["raw"]["date"], tuple(sorted(c["raw"]["_day"]["messages"])),
                 c["id"].split("@")[1])
                for c in whatsapp_source.candidates(source)}

    def test_a_crash_at_any_write_of_save_never_hides_a_chat_day(self):
        spool, baseline = self.env("baseline")
        spool.batch(iso(9, 0), [record("a", iso(8, 12), "first")])
        self.ids(baseline)
        self.second_batch(spool)
        real = whatsapp_source._write_json
        calls = []
        with mock.patch.object(
                whatsapp_source, "_write_json",
                side_effect=lambda p, d: (calls.append(p.name), real(p, d))):
            expected = self.ids(baseline)
        self.assertEqual(len(expected), 2)
        self.assertGreaterEqual(len(calls), 4)  # days, index, cursor
        for step in range(1, len(calls) + 1):
            spool, source = self.env(f"crash{step}")
            spool.batch(iso(9, 0), [record("a", iso(8, 12), "first")])
            self.ids(source)
            self.second_batch(spool)
            counter = {"n": 0}

            def crashing(path, payload):
                counter["n"] += 1
                if counter["n"] == step:
                    raise OSError(28, "No space left on device")
                return real(path, payload)

            with mock.patch.object(whatsapp_source, "_write_json",
                                   side_effect=crashing):
                with self.assertRaises(OSError):
                    self.ids(source)
            self.ids(source)
            self.assertEqual(self.ids(source), expected, f"crash at {step}")

    def test_a_lost_index_is_rebuilt_from_the_day_files(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 12), "first")])
        before = {c["id"] for c in self.listing()}
        (self.archive / "chats" / "index.json").unlink()
        self.assertEqual({c["id"] for c in self.listing()}, before)


class RedactionTest(Base):
    """Finding 2: both views are rendered from the same redacted values."""

    def test_multiline_one_time_codes_are_redacted_in_both_views(self):
        self.spool.batch(iso(9, 0), [
            record("a", iso(8, 0), "Your login code:\n482913", sender="Bank"),
            record("b", iso(8, 1), "verification code\n551177", sender="Bank"),
        ])
        item = whatsapp_source.fetch({}, self.source(), self.listing()[0])
        for code in ("482913", "551177"):
            self.assertNotIn(code, item["body"])
            self.assertNotIn(code, item["memory_appendix"])
        self.assertEqual(item["body"].count("[REDACTED]"), 2)
        self.assertEqual(item["memory_appendix"].count("[REDACTED]"), 2)

    def test_file_names_are_redacted_once_for_both_views(self):
        message, files = self.spool.media_message(
            "doc", iso(8, 0), "document", b"x", ".bin",
            original_name="login code 667788.txt")
        self.spool.batch(iso(9, 0), [message], files)
        item = whatsapp_source.fetch({}, self.source(), self.listing()[0])
        self.assertNotIn("667788", item["body"])
        self.assertNotIn("667788", item["memory_appendix"])


class BatchErrorTest(Base):
    """Finding 4: one bad batch degrades the listing instead of stopping it."""

    def spoil(self, name, how):
        path = self.spool.dir / name / "batch.json"
        if how == "version":
            payload = json.loads(path.read_text())
            payload["version"] = 2
            path.write_text(json.dumps(payload))
        elif how == "json":
            path.write_text("{not json")
        elif how == "unreadable":
            os.chmod(path, 0)
            self.addCleanup(os.chmod, path, 0o600)

    def test_ready_days_still_flow_and_the_position_is_held(self):
        for how, code in (("version", "unsupported_version_2"),
                          ("json", "bad_json"),
                          ("unreadable", "unreadable:PermissionError")):
            with self.subTest(how=how):
                self.setUp()
                self.spool.batch(iso(9, 0), [record("a", iso(8, 12), "ok")])
                self.listing()
                bad = self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "x")])
                self.spoil(bad, how)
                self.spool.batch(iso(11, 0), [
                    record("c", iso(10, 0), "after", chat_key=7)])
                self.logs.clear()
                found = self.listing()
                self.assertEqual([c["raw"]["source_id"] for c in found], [sid()])
                self.assertEqual(found.degraded, f"batch {bad} not read ({code})")
                cursor = json.loads(
                    next(self.archive.glob("spool-cursor-*.json")).read_text())
                self.assertTrue(cursor["last_batch"].startswith("b0000000041-"))
                self.assertTrue(any(f"batch {bad} not read" in line
                                    for line in self.logs))
                self.assertFalse(any("ok" in line.split() for line in self.logs))

    def test_runner_reports_a_degraded_listing_as_an_error(self):
        listing = whatsapp_source.Listing()
        listing.degraded = "batch b0000000042-20261122T080000Z not read (bad_json)"
        base = self.root / "daemon"
        routine = {"id": "wa", "enabled": True, "source": self.source(),
                   "analyze": {"provider": "gemini", "model": "m",
                               "instruction": "x"}}
        lines = []
        with mock.patch.dict(runner.SOURCES,
                             {"whatsapp": (lambda _s: listing, None)}), \
                mock.patch.object(config, "validate", return_value=[]), \
                mock.patch.object(runner, "log", side_effect=lines.append):
            totals = runner.run(base, [routine])
        self.assertEqual(totals["errors"], 1)
        self.assertTrue(any(
            line.startswith("routine=wa source=whatsapp ERROR degraded:")
            for line in lines))


class SequenceTest(Base):
    """A batch sequence that went backwards degrades the listing."""

    def test_restarted_sequence_degrades_and_archived_days_keep_flowing(self):
        self.spool.seq = 499
        first = self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        self.listing()
        shutil.rmtree(self.spool.dir / first)
        self.spool.seq = 0
        self.spool.batch(iso(12, 0), [record("b", iso(11, 0), "b", chat_key=7)])
        found = self.listing()
        self.assertIn("went backwards", found.degraded)
        self.assertEqual([c["raw"]["source_id"] for c in found], [sid()])

    def test_older_name_created_later_degrades_while_the_cursor_exists(self):
        self.spool.seq = 499
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        self.listing()
        self.spool.seq = 9
        self.spool.batch(iso(12, 0), [record("b", iso(11, 0), "b")])
        self.assertIn("went backwards", self.listing().degraded)

    def test_a_clock_stepped_back_is_not_a_regression(self):
        self.spool.batch(iso(10, 0), [record("a", iso(9, 0), "a")])
        self.spool.batch(iso(9, 10), [record("b", iso(9, 5), "b")])  # clock back
        self.assertIsNone(self.listing().degraded)
        self.spool.batch(iso(9, 20), [record("c", iso(9, 15), "c")])
        found = self.listing()
        self.assertIsNone(found.degraded)
        self.assertEqual(len(found[0]["raw"]["_day"]["messages"]), 3)

    def test_pruned_cursor_batch_with_newer_batches_is_normal(self):
        first = self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        self.listing()
        shutil.rmtree(self.spool.dir / first)
        self.spool.batch(iso(12, 0), [record("b", iso(11, 0), "b")])
        found = self.listing()
        self.assertIsNone(found.degraded)
        self.assertEqual(len(found), 1)


STORE_A = "was1-" + "a" * 32
STORE_B = "was1-" + "b" * 32


class ChatIdentityTest(Base):
    """Identity is a keyed hash of the chat JID; chat_key only links."""

    def chats(self, found):
        return {c["raw"]["chat"]: sorted(c["raw"]["_day"]["messages"])
                for c in found}

    def test_reinstall_without_store_ids_never_merges_two_chats(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "family",
                                            chat_key=5, chat_jid=GROUP_JID)])
        self.listing()
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "doctor",
                                             chat_key=5, chat_jid=DIRECT_JID)])
        chats = self.chats(self.listing())
        self.assertEqual(set(chats), {local(jid=GROUP_JID), local(jid=DIRECT_JID)})

    def test_reinstall_with_a_new_store_never_merges_two_chats(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "family",
                                            chat_key=5, chat_jid=GROUP_JID)])
        self.listing()
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "doctor",
                                             chat_key=5, chat_jid=DIRECT_JID)],
                         store_id=STORE_B)
        self.assertEqual(len(self.chats(self.listing())), 2)

    def test_same_jid_after_a_reinstall_is_the_same_chat(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a", chat_key=5,
                                            chat_jid=GROUP_JID)],
                         store_id=STORE_A)
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "b", chat_key=900,
                                             chat_jid=GROUP_JID)],
                         store_id=STORE_B)
        self.assertEqual(self.chats(self.listing()),
                         {local(): sorted([message_id("a"), message_id("b")])})

    def test_jid_move_within_one_store_is_linked_by_its_key(self):
        lid = "100000000000077@lid"
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a", chat_key=5,
                                            chat_jid=DIRECT_JID)],
                         store_id=STORE_A)
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "b", chat_key=5,
                                             chat_jid=lid)], store_id=STORE_A)
        chats = self.chats(self.listing())
        self.assertEqual(list(chats), [local(jid=DIRECT_JID)])
        self.assertEqual(len(chats[local(jid=DIRECT_JID)]), 2)
        state = json.loads((whatsapp_source.state_dir() / "identity.json")
                           .read_text())
        self.assertEqual(state["aliases"],
                         {local(jid=lid): local(jid=DIRECT_JID)})
        text = json.dumps(state)
        for jid in (DIRECT_JID, lid, GROUP_JID):
            self.assertNotIn(jid.split("@")[0], text)

    def test_jid_move_without_a_store_id_is_not_linked(self):
        lid = "100000000000077@lid"
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a", chat_key=5,
                                            chat_jid=DIRECT_JID)])
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "b", chat_key=5,
                                             chat_jid=lid)])
        self.assertEqual(len(self.chats(self.listing())), 2)

    def test_two_known_chats_are_never_merged_by_a_key(self):
        self.spool.batch(iso(9, 0), [
            record("a", iso(8, 0), "a", chat_key=5, chat_jid=GROUP_JID),
            record("b", iso(8, 1), "b", chat_key=6, chat_jid=DIRECT_JID)],
            store_id=STORE_A)
        self.spool.batch(iso(10, 0), [
            record("c", iso(9, 0), "c", chat_key=5, chat_jid=DIRECT_JID)],
            store_id=STORE_A)
        found = self.listing()
        self.assertEqual(len(self.chats(found)), 2)
        self.assertTrue(any("identity_conflicts=1" in line for line in self.logs))

    def test_ids_do_not_depend_on_what_was_seen_first(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        first = {c["raw"]["source_id"] for c in self.listing()}
        rebuilt = {c["raw"]["source_id"] for c in self.listing(
            archive_dir=str(self.root / "rebuilt"))}
        self.assertEqual(first, rebuilt)
        key = whatsapp_source.state_dir() / "chat-id.key"
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)
        self.assertFalse(list(self.archive.rglob("chat-id.key")))
        self.assertFalse(any(key.read_text().strip() in line
                             for line in self.logs))

    def test_records_without_a_jid_are_skipped_and_counted(self):
        orphan = record("x", iso(8, 0), "no jid", chat_key=5)
        orphan["chat_jid"] = None
        self.spool.batch(iso(9, 0), [orphan, record("a", iso(8, 1), "a")])
        found = self.listing()
        self.assertEqual(len(found), 1)
        self.assertTrue(any("no_chat_jid=1" in line for line in self.logs))
        self.assertTrue(any("without a chat JID were not archived" in line
                            for line in self.logs))

    def test_key_filters_use_the_current_store_only(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a", chat_key=5,
                                            chat_jid=GROUP_JID)],
                         store_id=STORE_A)
        self.assertEqual(len(self.listing(chat_keys=[5, 900])), 1)
        # Reinstalled: the same chat now has key 900 and key 5 is nobody.
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "b", chat_key=900,
                                             chat_jid=GROUP_JID)],
                         store_id=STORE_B)
        self.listing(chat_keys=[5, 900])
        self.logs.clear()
        self.assertEqual(self.listing(chat_keys=[5]), [])
        self.assertTrue(any("chat_keys entry 5 has not appeared" in line
                            for line in self.logs))
        self.assertEqual(len(self.listing(chat_keys=[900])), 1)

    def test_without_store_ids_a_key_filter_matches_the_record_key(self):
        self.spool.batch(iso(9, 0), [
            record("a", iso(8, 0), "a", chat_key=5),
            record("b", iso(8, 1), "b", chat_key=6, chat_jid=DIRECT_JID)])
        found = self.listing(chat_keys=[5])
        self.assertEqual([c["raw"]["chat"] for c in found], [local(key=5)])


class RebuildGuardTest(Base):
    """A day may replace its capture only once it holds every message of it."""

    def capture(self, found, ledger):
        for candidate in found:
            whatsapp_source.fetch({}, self.source(), candidate)  # records ids
            ledger[candidate["id"]] = {
                "source_kind": "whatsapp",
                "source_id": candidate["raw"]["source_id"],
                "processed_at": "2026-11-22T20:00:00Z", "memory": "created"}

    def test_superset_day_in_a_rebuilt_archive_is_captured_again(self):
        ledger = {}
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        self.capture(self.listing(processed=ledger), ledger)
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "new")])
        found = self.listing(processed=ledger,
                             archive_dir=str(self.root / "rebuilt"))
        self.assertEqual([c["raw"]["source_id"] for c in found], [sid()])
        self.assertFalse(found[0]["raw"].get("_deferred"))

    def test_partial_day_is_held_and_gets_no_other_id(self):
        ledger = {}
        first = self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        self.capture(self.listing(processed=ledger), ledger)
        shutil.rmtree(self.spool.dir / first)
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "b")],
                         store_id=STORE_A)
        found = self.listing(processed=ledger,
                             archive_dir=str(self.root / "rebuilt"))
        self.assertEqual(found, [])
        self.assertTrue(any("fewer messages than their last capture" in line
                            for line in self.logs))
        days = list((self.root / "rebuilt" / "chats").glob("*/*.json"))
        self.assertEqual([p.parent.name for p in days], [local()])
        found = self.listing(processed=ledger,
                             archive_dir=str(self.root / "rebuilt"),
                             recapture_after_rebuild=True)
        self.assertEqual(len(found), 1)

    def test_normal_growth_is_never_held(self):
        ledger = {}
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        self.capture(self.listing(processed=ledger), ledger)
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "b")])
        self.assertEqual(len(self.ready(self.listing(processed=ledger))), 1)


class ReadinessTest(Base):
    """Extraction outcomes are changes with a time; nothing else re-arms a day."""

    def settle(self, found, ledger, at="2026-11-22T20:00:00Z"):
        for candidate in found:
            ledger[candidate["id"]] = {
                "source_kind": "whatsapp",
                "source_id": candidate["raw"]["source_id"],
                "processed_at": at, "memory": "created"}

    def test_tools_installed_later_recapture_once_after_quiet(self):
        model = self.root / "model.bin"
        model.write_bytes(b"m")
        message, files = self.spool.media_message(
            "v", iso(8, 0, day=19), "audio", b"OggS", ".opus")
        self.spool.batch(iso(9, 0, day=19), [message], files)
        source = dict(transcribe_audio=True, whisper_model=str(model),
                      quiet_minutes=60)
        ledger = {}
        missing = mock.patch.object(whatsapp_source, "optional_bin",
                                    return_value=None)
        with missing:
            found = self.listing(processed=ledger, **source)
        self.assertEqual(len(self.ready(found)), 1)
        self.settle(found, ledger)
        installed = mock.patch.object(
            whatsapp_source, "optional_bin",
            side_effect=lambda name, _env: f"/opt/tools/{name}")
        whatsapp_source._extract_spent = 10 ** 6  # budget used up
        with installed:
            self.assertEqual(self.listing(processed=ledger, **source), [])

        def run(cmd, timeout=120, check=True):
            if Path(cmd[0]).name == "whisper-cli":
                Path(cmd[cmd.index("-of") + 1]).with_suffix(".txt") \
                    .write_text("transcript", encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        whatsapp_source._extract_spent = 0.0
        with installed, mock.patch.object(whatsapp_source, "run",
                                          side_effect=run):
            found = self.listing(processed=ledger, **source)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["raw"].get("_deferred"))  # quiet period
        self.now += 3601
        with installed:
            found = self.listing(processed=ledger, **source)
        self.assertEqual(len(self.ready(found)), 1)
        self.settle(found, ledger, at="2026-11-23T00:00:00Z")
        with installed:
            self.assertEqual(self.listing(processed=ledger, **source), [])

    def test_text_finished_in_the_ingest_run_adds_no_extra_wait(self):
        model = self.root / "model.bin"
        model.write_bytes(b"m")
        message, files = self.spool.media_message(
            "v", iso(8, 0), "audio", b"OggS", ".opus")
        self.spool.batch(iso(9, 0), [message], files)  # quiet long since

        def run(cmd, timeout=120, check=True):
            if Path(cmd[0]).name == "whisper-cli":
                Path(cmd[cmd.index("-of") + 1]).with_suffix(".txt") \
                    .write_text("transcript", encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
                whatsapp_source, "optional_bin",
                side_effect=lambda name, _env: f"/opt/tools/{name}"), \
                mock.patch.object(whatsapp_source, "run", side_effect=run):
            found = self.listing(transcribe_audio=True,
                                 whisper_model=str(model), quiet_minutes=60)
        record = found[0]["raw"]["_day"]["messages"][message_id("v")]
        self.assertEqual(record["extract"]["state"], "done")
        self.assertEqual(len(self.ready(found)), 1)

    def test_budget_zero_turns_extraction_off(self):
        model = self.root / "model.bin"
        model.write_bytes(b"m")
        message, files = self.spool.media_message(
            "v", iso(8, 0), "audio", b"OggS", ".opus")
        self.spool.batch(iso(9, 0), [message], files)
        with mock.patch.object(whatsapp_source, "run") as run:
            found = self.listing(transcribe_audio=True,
                                 whisper_model=str(model),
                                 extract_budget_seconds=0)
        run.assert_not_called()
        self.assertEqual(len(self.ready(found)), 1)
        item = whatsapp_source.fetch({}, self.source(extract_budget_seconds=0),
                                     found[0])
        self.assertNotIn("transcript:", item["body"])


class StaleIndexTest(Base):
    def test_a_stale_index_entry_is_healed_even_when_the_batch_is_gone(self):
        ledger = {}
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        found = self.listing(processed=ledger)
        ledger[found[0]["id"]] = {"source_kind": "whatsapp",
                                  "source_id": sid(), "memory": "created",
                                  "processed_at": "2026-11-22T20:00:00Z"}
        later = self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "later")])
        real = whatsapp_source._write_json

        def crashing(path, payload):
            if path.name == "index.json":
                raise OSError(28, "No space left on device")
            return real(path, payload)

        with mock.patch.object(whatsapp_source, "_write_json",
                               side_effect=crashing):
            with self.assertRaises(OSError):
                self.listing(processed=ledger)
        shutil.rmtree(self.spool.dir / later)
        found = self.listing(processed=ledger)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]["raw"]["_day"]["messages"]), 2)


class FetchTest(Base):
    def test_last_chance_copy_never_writes_back_the_listing_snapshot(self):
        message, files = self.spool.media_message(
            "img", iso(8, 0), "image", b"jpg", ".jpg")
        self.spool.batch(iso(9, 0), [message], files)
        real = whatsapp_source._copy_file
        with mock.patch.object(whatsapp_source, "_copy_file",
                               side_effect=PermissionError("denied")):
            found = self.listing()
        # Meanwhile the archive gains a message the snapshot does not hold.
        self.spool.batch(iso(9, 30), [record("new", iso(9, 20), "newer")])
        self.listing()
        with mock.patch.object(whatsapp_source, "_copy_file",
                               side_effect=real):
            whatsapp_source.fetch({}, self.source(), found[0])
        day = json.loads(next(self.archive.glob("chats/*/2026-11-22.json"))
                         .read_text())
        self.assertIn(message_id("new"), day["messages"])

    def test_labels_and_sender_names_are_redacted_everywhere(self):
        self.spool.batch(iso(9, 0), [
            record("a", iso(8, 0), "hi", label="password: Hunter2Label",
                   sender="pin code: 9911")])
        item = whatsapp_source.fetch({}, self.source(), self.listing()[0])
        rendered = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("Hunter2Label", rendered)
        self.assertNotIn("9911", rendered)
        self.assertNotIn("Hunter2Label",
                         "\n".join(llm.source_header_lines(item)))


class MediaLifecycleTest(Base):
    def test_raised_limit_or_enabled_copies_happen_later(self):
        big, files = self.spool.media_message(
            "big", iso(8, 0), "video", b"v" * (1024 * 1024 + 10), ".mp4")
        img, img_files = self.spool.media_message(
            "img", iso(8, 1), "image", b"jpg", ".jpg")
        self.spool.batch(iso(9, 0), [big, img], {**files, **img_files})
        found = self.listing(max_file_mb=1, copy_media=False)
        messages = found[0]["raw"]["_day"]["messages"]
        self.assertFalse(any(m.get("stored_path") for m in messages.values()))
        found = self.listing(max_file_mb=1)
        messages = found[0]["raw"]["_day"]["messages"]
        self.assertTrue(messages[message_id("img")]["stored_path"])
        self.assertEqual(messages[message_id("big")]["store_note"], "too_large")
        found = self.listing(max_file_mb=2)
        self.assertTrue(found[0]["raw"]["_day"]["messages"]
                        [message_id("big")]["stored_path"])

    def index(self):
        return json.loads((self.archive / "chats" / "index.json").read_text())

    def test_media_bytes_is_recounted_and_replacements_are_not_double_counted(self):
        first, files = self.spool.media_message(
            "img", iso(8, 0), "image", b"a" * 100, ".jpg")
        self.spool.batch(iso(9, 0), [first], files)
        self.listing()
        self.assertEqual(self.index()["media_bytes"], 100)
        again, files = self.spool.media_message(
            "img", iso(8, 0), "image", b"b" * 150, ".jpg", update="media")
        self.spool.batch(iso(10, 0), [again], files)
        self.listing()
        self.assertEqual(self.index()["media_bytes"], 150)
        index = self.index()
        index["media_bytes"] = 999999
        (self.archive / "chats" / "index.json").write_text(json.dumps(index))
        self.now += whatsapp_source.MEDIA_RECOUNT_SECONDS + 1
        self.listing()
        self.assertEqual(self.index()["media_bytes"], 150)

    def test_stale_temp_files_and_workdirs_are_swept(self):
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        self.listing()
        old = self.now - whatsapp_source.STALE_TEMP_SECONDS - 10
        stale = self.archive / "media" / DAY / ".wa1-x.jpg.123.tmp"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"x")
        os.utime(stale, (old, old))
        workdir = self.archive / ".tmp" / "tmpstale"
        workdir.mkdir(parents=True)
        (workdir / "audio.wav").write_bytes(b"x")
        os.utime(workdir, (old, old))
        fresh = self.archive / ".tmp" / "tmpfresh"
        fresh.mkdir()
        os.utime(fresh, (self.now, self.now))
        self.listing()
        self.assertFalse(stale.exists())
        self.assertFalse(workdir.exists())
        self.assertTrue(fresh.exists())

    def test_sha256_is_verified_when_present(self):
        good, files = self.spool.media_message(
            "good", iso(8, 0), "image", b"right", ".jpg")
        good["media"]["sha256"] = hashlib.sha256(b"right").hexdigest()
        bad, bad_files = self.spool.media_message(
            "bad", iso(8, 1), "image", b"wrong", ".jpg")
        bad["media"]["sha256"] = hashlib.sha256(b"expected").hexdigest()
        self.spool.batch(iso(9, 0), [good, bad], {**files, **bad_files})
        messages = self.listing()[0]["raw"]["_day"]["messages"]
        self.assertTrue(messages[message_id("good")]["stored_path"])
        self.assertEqual(messages[message_id("bad")]["store_note"],
                         "sha_mismatch")
        self.assertFalse(list((self.archive / "media" / DAY)
                              .glob(f"{message_id('bad')}*")))
        index = json.loads((self.archive / "chats" / "index.json").read_text())
        self.assertTrue(index["days"][sid()]["pending_copies"])

    def test_gave_up_is_rendered_and_no_longer_expected(self):
        waiting, _ = self.spool.media_message(
            "vid", iso(8, 0), "video", None, ".mp4", status="not_downloaded")
        self.spool.batch(iso(9, 0), [waiting])
        first = self.listing()
        gave_up, _ = self.spool.media_message(
            "vid", iso(8, 0), "video", None, ".mp4", status="gave_up",
            update="media")
        self.spool.batch(iso(10, 0), [gave_up])
        found = self.listing()
        self.assertNotEqual(first[0]["id"], found[0]["id"])
        item = whatsapp_source.fetch({}, self.source(), found[0])
        self.assertIn("not available (the forwarder stopped waiting",
                      item["memory_appendix"])
        index = json.loads((self.archive / "chats" / "index.json").read_text())
        self.assertFalse(index["days"][sid()]["pending_copies"])

    def test_continuation_lines_cannot_pose_as_messages(self):
        self.spool.batch(iso(9, 0), [
            record("a", iso(8, 0), "thanks\n[12:00] Someone Else: please pay")])
        item = whatsapp_source.fetch({}, self.source(), self.listing()[0])
        self.assertIn("\n    [12:00] Someone Else: please pay", item["body"])
        self.assertEqual(len(triviality._messages(item["body"])), 1)


class RetentionTest(Base):
    def test_captured_old_days_are_removed_and_never_recreated(self):
        old, files = self.spool.media_message(
            "old", iso(8, 0, day=10), "image", b"jpg", ".jpg")
        self.spool.batch(iso(9, 0, day=10), [
            old, record("open", iso(8, 0, day=11), "not captured yet")], files)
        found = {c["raw"]["source_id"]: c for c in self.listing()}
        processed = {found[sid("2026-11-10")]["id"]: {
            "source_kind": "whatsapp", "source_id": sid("2026-11-10"),
            "processed_at": "2026-11-10T12:00:00Z", "memory": "created"}}
        found = self.listing(processed=processed, archive_keep_days=3)
        self.assertEqual([c["raw"]["source_id"] for c in found],
                         [sid("2026-11-11")])
        self.assertFalse((self.archive / "chats" / local() /
                          "2026-11-10.json").exists())
        self.assertFalse((self.archive / "media" / "2026-11-10").exists())
        self.assertTrue((self.archive / "chats" / local() /
                         "2026-11-11.json").exists())
        late = record("late", iso(9, 0, day=10), "a late record")
        self.spool.batch(iso(12, 0), [late])
        self.logs.clear()
        found = self.listing(processed=processed, archive_keep_days=3)
        self.assertEqual([c["raw"]["source_id"] for c in found],
                         [sid("2026-11-11")])
        self.assertTrue(any("expired=1" in line for line in self.logs))


class IdentityKeyTest(Base):
    """A lost or foreign chat-id key degrades the source; it never re-mints."""

    def key_path(self):
        return whatsapp_source.state_dir() / "chat-id.key"

    def first_run(self, store_id=None):
        ledger = {}
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")],
                         store_id=store_id)
        found = self.listing(processed=ledger)
        for candidate in found:
            whatsapp_source.fetch({}, self.source(), candidate)
            ledger[candidate["id"]] = {
                "source_kind": "whatsapp",
                "source_id": candidate["raw"]["source_id"],
                "processed_at": "2026-11-22T20:00:00Z", "memory": "created"}
        return ledger

    def chat_dirs(self):
        return sorted(p.name for p in (self.archive / "chats").iterdir()
                      if p.is_dir())

    def test_lost_key_degrades_instead_of_minting_new_ids(self):
        for store_id, lost in ((None, ["chat-id.key"]),
                               ("was1-" + "1" * 32,
                                ["chat-id.key", "identity.json"])):
            with self.subTest(store_id=store_id):
                self.setUp()
                ledger = self.first_run(store_id)
                before = self.chat_dirs()
                saved = self.key_path().read_bytes()
                for name in lost:
                    (whatsapp_source.state_dir() / name).unlink()
                self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "pm")],
                                 store_id=store_id)
                self.logs.clear()
                found = self.listing(processed=ledger)
                self.assertEqual(list(found), [])
                self.assertIn("chat-id key is missing", found.degraded)
                self.assertIn("restore it", found.degraded)
                self.assertFalse(self.key_path().exists())
                self.assertEqual(self.chat_dirs(), before)
                self.assertTrue(any(line.startswith("whatsapp ERROR:")
                                    for line in self.logs))
                # Restoring the key (and its identity file) heals it.
                self.key_path().write_bytes(saved)
                os.chmod(self.key_path(), 0o600)
                if "identity.json" in lost:
                    continue
                found = self.listing(processed=ledger)
                self.assertIsNone(found.degraded)
                self.assertEqual(self.chat_dirs(), before)
                self.assertEqual([c["raw"]["source_id"] for c in found], [sid()])

    def test_ledger_with_chats_also_forbids_minting(self):
        ledger = {"whatsapp:c0000000000000000:2026-11-20@0123456789abcdef": {
            "source_kind": "whatsapp", "memory": "created",
            "source_id": "whatsapp:c0000000000000000:2026-11-20",
            "processed_at": "2026-11-20T20:00:00Z"}}
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        found = self.listing(processed=ledger)
        self.assertIn("chat-id key is missing", found.degraded)
        self.assertFalse(self.key_path().exists())

    def test_a_different_key_is_refused_by_the_archive_fingerprint(self):
        ledger = self.first_run()
        self.key_path().write_text("ab" * 32 + "\n")  # a wrong backup
        found = self.listing(processed=ledger)
        self.assertEqual(list(found), [])
        self.assertIn("different chat-id key", found.degraded)
        fingerprint = json.loads(
            (self.archive / "identity-fingerprint.json").read_text())
        self.assertNotIn("ab" * 32, json.dumps(fingerprint))

    def test_a_second_checkout_with_its_own_state_is_refused(self):
        self.first_run()
        other = self.root / "other-checkout"
        whatsapp_source.configure(other)
        self.spool.batch(iso(10, 0), [record("b", iso(9, 0), "b")])
        found = self.listing()
        self.assertIn("chat-id key is missing", found.degraded)
        # Even with its own key and identity file it cannot write here.
        whatsapp_source._identity_key(whatsapp_source.state_dir(), False)
        shutil.copy(self.root / "daemon" / "state" / "whatsapp" / "identity.json",
                    whatsapp_source.state_dir() / "identity.json")
        found = self.listing()
        self.assertIn("different chat-id key", found.degraded)

    def test_corrupt_identity_or_captures_file_degrades_by_kind(self):
        ledger = self.first_run()
        for name, kind in (("identity.json", "identity file"),
                           ("captures.json", "capture-ids file")):
            with self.subTest(name=name):
                path = whatsapp_source.state_dir() / name
                good = path.read_text()
                path.write_text('{"secret text": ')
                found = self.listing(processed=ledger)
                self.assertIn(f"the {kind} is not valid JSON", found.degraded)
                self.assertNotIn("secret text", found.degraded)
                self.assertFalse(any("secret text" in line for line in self.logs))
                path.write_text(good)
                self.assertIsNone(self.listing(processed=ledger).degraded)

    def test_identity_dir_keeps_the_key_next_to_the_archive(self):
        keys = self.root / "identity"
        ledger = {}
        self.spool.batch(iso(9, 0), [record("a", iso(8, 0), "a")])
        found = self.listing(processed=ledger, identity_dir=str(keys))
        whatsapp_source.fetch({}, self.source(identity_dir=str(keys)), found[0])
        self.assertEqual(sorted(p.name for p in keys.iterdir()),
                         ["captures.json", "chat-id.key", "identity.json"])
        self.assertEqual(stat.S_IMODE(os.stat(keys).st_mode), 0o700)
        self.assertFalse((whatsapp_source.state_dir() / "chat-id.key").exists())
        # A re-cloned daemon (new, empty state) still has its identity.
        whatsapp_source.configure(self.root / "fresh-clone")
        found = self.listing(identity_dir=str(keys))
        self.assertIsNone(found.degraded)

    def test_identity_dir_is_validated_like_the_archive(self):
        routine = {"id": "wa", "enabled": True,
                   "analyze": {"provider": "gemini", "model": "m",
                               "instruction": "x"}}
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        open_dir = self.root / "open"
        open_dir.mkdir(mode=0o755)
        os.chmod(open_dir, 0o755)
        for value, needle in (("relative/keys", "must be an absolute path"),
                              (str(repo / "keys"), "inside a git work tree"),
                              (str(open_dir), "identity_dir is accessible"),
                              (str(self.spool.dir / "keys"),
                               "outside spool_dir")):
            routine["source"] = self.source(identity_dir=value)
            problems = "\n".join(config.validate(routine))
            self.assertIn(needle, problems, value)

    def test_the_settled_version_survives_many_unsettled_fetches(self):
        ledger = self.first_run()
        settled_digest = next(iter(ledger)).split("@")[1]
        for n in range(whatsapp_source.CAPTURE_VERSIONS + 3):
            self.spool.batch(iso(10, n), [record(f"m{n}", iso(9, n), "x")])
            found = self.listing(processed=ledger)
            for candidate in found:
                whatsapp_source.fetch({}, self.source(), candidate)  # fails later
        captures = json.loads(
            (whatsapp_source.state_dir() / "captures.json").read_text())
        versions = captures["days"][sid()]["versions"]
        self.assertIn(settled_digest, versions)
        self.assertEqual(len(versions), whatsapp_source.CAPTURE_VERSIONS + 1)


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
                         sid())
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
                         sid())
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

    def test_no_personal_data_in_dry_run_logs(self):
        self.run_daemon(dry_run=True)
        joined = "\n".join(self.all_logs)
        self.assertIn("[dry-run]", joined)
        for secret in (PARENT, "מחר", "Thanks!", GROUP_LABEL, GROUP_JID,
                       MEMBER_JID, "120363000000000001", "100000000000001"):
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
