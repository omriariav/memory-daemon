"""Inbox 0.13.2: the optional `status` lines of a `list` reply, the Agents view
(6) and the Inbox without working agent rows. Pure model tests, no Textual.

Synthetic data only: the names below are made up."""
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from workspace_daemon.inbox import items as itm, settings, sources, views
from workspace_daemon.inbox.models import from_v1
from workspace_daemon.inbox.store import InboxStore

NOW = 1_700_000_000.0


def raw_item(i, section="fyi", **meta):
    return {"v": 1, "id": f"ag:{i}", "source": "other", "section": section,
            "urgency": "urgent" if section == "urgent" else "digest",
            "sender": f"bot-{i}", "where": "agents · helper", "gist": f"gist {i}",
            "ts": NOW - i * 60, "link": None, "thread": [], "draft": None,
            "actions": [], "meta": meta}


def agent(i, section="fyi", bucket=None, **meta):
    if bucket:
        meta["bucket"] = bucket
    row = from_v1(itm.parse_item(raw_item(i, section, kind="agent", **meta)))
    row.origin = "agents"
    return row


def plain(i, section="fyi", **meta):
    row = from_v1(itm.parse_item(raw_item(i, section, **meta)))
    row.origin = "other"
    return row


def keys(rows):
    return [i.key for i in rows]


def doc(**extra):
    return json.dumps(dict({"v": 1, "items": [], "stale": False,
                            "age_min": None, "error": None}, **extra))


class ParseStatusTest(unittest.TestCase):
    def test_valid_lines_are_kept_with_their_level(self):
        out = itm.parse_status([
            {"text": "one", "level": "ok"}, {"text": "two", "level": "warn"},
            {"text": "three", "level": "alert", "item_id": "ag:3"}])
        self.assertEqual(out, [
            {"text": "one", "level": "ok"}, {"text": "two", "level": "warn"},
            {"text": "three", "level": "alert", "item_id": "ag:3"}])

    def test_text_is_one_clean_line_and_capped(self):
        out = itm.parse_status([
            {"text": "a\nb\tc \x1b[31mred\x1b[0m\x07 ‮flip"},
            {"text": "x" * 500}])
        self.assertEqual(out[0]["text"], "a b c red flip")
        self.assertEqual(len(out[1]["text"]), itm.STATUS_TEXT_MAX)
        self.assertTrue(out[1]["text"].endswith("…"))

    def test_an_unknown_or_missing_level_reads_as_ok(self):
        out = itm.parse_status([{"text": "a", "level": "panic"},
                                {"text": "b"}, {"text": "c", "level": 3},
                                {"text": "d", "level": None}])
        self.assertEqual([r["level"] for r in out], ["ok"] * 4)

    def test_item_id_is_optional_and_must_be_a_string(self):
        out = itm.parse_status([
            {"text": "a", "item_id": 7}, {"text": "b", "item_id": ""},
            {"text": "c", "item_id": "x" * 500}, {"text": "d", "item_id": "ok:1"}])
        self.assertEqual(["item_id" in r for r in out],
                         [False, False, False, True])

    def test_at_most_twelve_lines_and_bad_entries_are_skipped(self):
        many = [{"text": f"line {n}"} for n in range(30)]
        out = itm.parse_status(["junk", 5, None, {"level": "warn"},
                                {"text": ""}, {"text": "   "},
                                {"text": 7}] + many)
        self.assertEqual(len(out), itm.STATUS_MAX)
        self.assertEqual(out[0]["text"], "line 0")

    def test_unknown_keys_are_ignored(self):
        out = itm.parse_status([{"text": "a", "color": "red", "cmd": "rm -rf /"}])
        self.assertEqual(out, [{"text": "a", "level": "ok"}])

    def test_not_a_list_is_dropped(self):
        for bad in (None, "text", {"text": "a"}, 5, True):
            self.assertEqual(itm.parse_status(bad), [])


class ListOutputTest(unittest.TestCase):
    def test_the_four_value_form_is_unchanged(self):
        out = itm.parse_list_output(doc(status=[{"text": "a"}]))
        self.assertEqual(len(out), 4)

    def test_with_status_adds_the_lines(self):
        rows, stale, age, err, status = itm.parse_list_output(
            doc(items=[raw_item(1)], status=[{"text": "a", "level": "warn"}]),
            with_status=True)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(err)
        self.assertEqual(status, [{"text": "a", "level": "warn"}])

    def test_an_old_source_without_status_changes_nothing(self):
        _, _, _, err, status = itm.parse_list_output(doc(), with_status=True)
        self.assertEqual(status, [])
        self.assertIsNone(err)

    def test_a_bad_status_never_breaks_or_flags_the_items(self):
        for bad in ("nope", {"a": 1}, [1, 2, 3], [{"text": None}], 7):
            rows, _, _, err, status = itm.parse_list_output(
                doc(items=[raw_item(1), raw_item(2)], status=bad),
                with_status=True)
            self.assertEqual(len(rows), 2, bad)
            self.assertIsNone(err, bad)
            self.assertEqual(status, [], bad)


class CommandSourceStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_src(self, payload):
        path = Path(self.tmp.name) / "src.py"
        path.write_text(textwrap.dedent(f"""
            import json
            print(json.dumps({payload!r}))
        """))
        return sources.run_command_source("cmd", [sys.executable, str(path)], 10)

    def test_the_result_carries_the_status_lines(self):
        res = self.run_src({"v": 1, "items": [], "status": [
            {"text": "bot-1  working · 4m", "level": "ok"}]})
        self.assertIsNone(res.error)
        self.assertEqual(res.status, [{"text": "bot-1 working · 4m",
                                       "level": "ok"}])

    def test_a_source_without_status_has_an_empty_tuple(self):
        res = self.run_src({"v": 1, "items": []})
        self.assertEqual(tuple(res.status), ())

    def test_a_failing_source_has_no_status(self):
        res = sources.run_command_source("cmd", ["/no/such/binary-xyz"], 2)
        self.assertTrue(res.error)
        self.assertEqual(tuple(res.status), ())

    def test_five_positional_fields_still_build_a_result(self):
        res = sources.SourceResult("x", [], False, None, None)
        self.assertEqual(tuple(res.status), ())

    def test_merge_keeps_the_status(self):
        res = sources.SourceResult("x", [], False, None, None,
                                   [{"text": "a", "level": "ok"}])
        out = sources.merge([[res]])
        self.assertEqual(out[0].status, [{"text": "a", "level": "ok"}])


class AgentViewTest(unittest.TestCase):
    def test_view_6_exists_and_lists_queue_rows(self):
        self.assertEqual(views.VIEW_KEYS[5], "agents")
        self.assertEqual(views.VIEW_TITLES["agents"], "Agents")
        self.assertIn("agents", views.QUEUE_VIEWS)
        self.assertEqual(len(views.VIEWS), 6)

    def test_groups_follow_meta_bucket(self):
        rows = [agent(1, "needs_reply", bucket="needs_you"),
                agent(2, "fyi", bucket="working"),
                agent(3, "fyi", bucket="done"),
                agent(4, "fyi", bucket="working"),
                plain(5)]
        groups = views.agent_groups(rows)
        self.assertEqual([g.title for g in groups],
                         ["NEEDS YOU", "WORKING", "DONE"])
        self.assertEqual([keys(g.items) for g in groups],
                         [["ag:1"], ["ag:2", "ag:4"], ["ag:3"]])
        self.assertTrue(groups[0].hot)

    def test_a_missing_or_unknown_bucket_falls_back_to_the_section(self):
        rows = [agent(1, "urgent"), agent(2, "needs_reply"), agent(3, "fyi"),
                agent(4, "fyi", bucket="bogus"),
                agent(5, "needs_reply", bucket="bogus")]
        by = {g.key: keys(g.items) for g in views.agent_groups(rows)}
        self.assertEqual(by["needs_you"], ["ag:1", "ag:2", "ag:5"])
        self.assertEqual(by["working"], ["ag:3", "ag:4"])
        self.assertNotIn("done", by)

    def test_newest_first_and_empty_groups_are_left_out(self):
        rows = [agent(3, bucket="working"), agent(1, bucket="working"),
                agent(2, bucket="working")]
        groups = views.agent_groups(rows)
        self.assertEqual([g.key for g in groups], ["working"])
        self.assertEqual(keys(groups[0].items), ["ag:1", "ag:2", "ag:3"])
        self.assertEqual(views.agent_groups([plain(1)]), [])

    def test_a_row_a_feed_rule_mutes_stays_hidden_in_view_6(self):
        rows = [agent(1, bucket="working"), agent(2, bucket="working"),
                agent(3, "needs_reply", bucket="needs_you")]
        rows[1].feed = "hide"
        listed = views.view_rows(views.agent_groups(rows))
        self.assertEqual(keys(listed), ["ag:3", "ag:1"])   # NEEDS YOU first
        rows[0].feed = rows[2].feed = "digest"     # only a mute hides a row
        self.assertEqual(len(views.view_rows(views.agent_groups(rows))), 2)

    def test_the_groups_hold_the_same_objects(self):
        rows = [agent(1, "needs_reply", bucket="needs_you"),
                agent(2, bucket="working")]
        listed = views.view_rows(views.agent_groups(rows))
        self.assertTrue(all(a is b for a in listed for b in rows
                            if a.key == b.key))

    def test_a_non_dict_meta_is_not_an_agent(self):
        row = plain(1)
        row.meta = None
        self.assertFalse(views.is_agent(row))
        self.assertEqual(views.agent_groups([row]), [])


class InboxWithoutWorkingAgentsTest(unittest.TestCase):
    def test_a_working_agent_row_leaves_the_inbox(self):
        rows = [agent(1, "needs_reply", bucket="needs_you"),
                agent(2, "urgent", bucket="needs_you"),
                agent(3, "fyi", bucket="working"),
                agent(4, "fyi", bucket="working", state="queued")]
        by = {g.key: keys(g.items) for g in views.inbox_groups(rows)}
        self.assertEqual(by["urgent"], ["ag:2"])
        self.assertEqual(by["owe"], ["ag:1"])
        shown = [k for g in views.inbox_groups(rows) for k in keys(g.items)]
        self.assertNotIn("ag:3", shown)
        self.assertNotIn("ag:4", shown)

    def test_a_rule_that_promotes_a_working_agent_row_does_not_bring_it_back(self):
        row = agent(3, "fyi", bucket="working")
        row.feed = "inbox"
        by = {g.key: keys(g.items) for g in views.inbox_groups([row])}
        self.assertEqual(by["urgent"], [])

    def test_other_fyi_rows_promoted_by_a_rule_still_join_urgent(self):
        row = plain(3, "fyi")
        row.feed = "inbox"
        by = {g.key: keys(g.items) for g in views.inbox_groups([row])}
        self.assertEqual(by["urgent"], ["ag:3"])

    def test_agent_rows_are_not_in_the_feed_either(self):
        rows = [agent(1, "fyi", bucket="working"), plain(2, "fyi")]
        feed = views.feed_view(rows, NOW, show_all=True)
        listed = [i.key for g in feed.groups for i in g.items]
        self.assertEqual(listed, ["ag:2"])

    def test_an_agent_row_in_the_needs_you_group_is_in_both_views(self):
        row = agent(1, "needs_reply", bucket="needs_you")
        inbox = views.view_rows(views.inbox_groups([row]))
        agents = views.view_rows(views.agent_groups([row]))
        self.assertEqual(keys(inbox), ["ag:1"])
        self.assertEqual(keys(agents), ["ag:1"])
        self.assertIs(inbox[0], agents[0])


class StripSettingsTest(unittest.TestCase):
    def test_strip_lines_defaults_to_four(self):
        self.assertEqual(settings.validate({})["strip_lines"], 4)

    def test_strip_lines_accepts_1_to_12(self):
        for n in (1, 4, 12):
            self.assertEqual(settings.validate({"strip_lines": n})["strip_lines"], n)

    def test_strip_lines_rejects_anything_else(self):
        for bad in (0, 13, -1, 2.5, "4", True, None):
            with self.assertRaises(settings.SettingsError, msg=repr(bad)):
                settings.validate({"strip_lines": bad})

    def test_the_view_key_is_not_a_valid_action_key(self):
        # `actions:` keys must be a letter, so a digit view key can never clash
        self.assertNotIn("6", settings.ACTION_KEY_CHOICES)
        for key in "123456":
            with self.assertRaises(settings.SettingsError):
                settings.validate({"actions": [{
                    "key": key, "label": "x", "argv": ["x"]}]})

    def test_the_strip_preference_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = InboxStore(Path(tmp) / "inbox-state.json")
            self.assertIsNone(store.get_pref("sessions_strip"))
            store.set_pref("sessions_strip", "off")
            again = InboxStore(Path(tmp) / "inbox-state.json")
            self.assertEqual(again.get_pref("sessions_strip"), "off")
            with self.assertRaises(ValueError):
                store.set_pref("sessions_strip", "maybe")


if __name__ == "__main__":
    unittest.main()
