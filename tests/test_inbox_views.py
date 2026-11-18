"""The inbox views and the Feed rules: pure model tests, no Textual."""
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from workspace_daemon import events
from workspace_daemon.inbox import (feedrules, items as itm, settings, sources,
                                    views)
from workspace_daemon.inbox.models import from_v1
from workspace_daemon.inbox.store import InboxStore

NOW = 1_700_000_000.0
H = 3600.0
# `ago_h` that puts a row at local noon, so rows around it share one local day
# whatever the timezone.
NOON = (NOW - datetime.fromtimestamp(NOW).replace(
    hour=12, minute=0, second=0, microsecond=0).timestamp()) / H


def mk(key, section="fyi", source="calendar", ago_h=1.0, who="Pat Sample",
       actions=("ack",), draft=None, origin="events", **meta):
    raw = itm.make_item(
        id=key, source=source, section=section,
        urgency="urgent" if section == "urgent" else "digest",
        sender=who, where="somewhere", gist=f"gist {key}", ts=NOW - ago_h * H,
        link=None, thread=[], draft=draft, actions=list(actions), meta=meta)
    it = from_v1(raw)
    it.origin = origin
    return it


def keys(rows):
    return [i.key for i in rows]


class InboxViewTest(unittest.TestCase):
    def test_sections_map_to_groups(self):
        rows = [
            mk("u1", "urgent", "alert", 1),
            mk("o1", "needs_reply", "slack", 3, actions=("reply",)),
            mk("o2", "needs_reply", "email", 1, actions=("reply",)),
            mk("n1", "waiting", "slack", 30, actions=("nudge",),
               draft="gentle nudge"),
            mk("w1", "waiting", "slack", 5, actions=("nudge",)),
            mk("f1", "fyi"),
        ]
        groups = {g.key: g for g in views.inbox_groups(rows)}
        self.assertEqual(keys(groups["urgent"].items), ["u1"])
        self.assertEqual(keys(groups["owe"].items), ["o2", "o1"])  # newest first
        self.assertEqual(keys(groups["nudge"].items), ["n1"])
        # the first three are listed even when empty; DUE TODAY (tasks) is
        # a fourth group the queue hides while it has no rows
        self.assertEqual([g.title for g in views.inbox_groups([])],
                         ["URGENT", "I OWE", "NUDGE DUE", "DUE TODAY"])

    def test_invites_belong_to_i_owe_soonest_first(self):
        later = mk("inv2", "needs_reply", "calendar", -48, actions=("ack",),
                   kind="invite")
        sooner = mk("inv1", "needs_reply", "calendar", -2, actions=("ack",),
                    kind="invite")
        owe = views.inbox_groups([later, sooner])[1]
        self.assertEqual(keys(owe.items), ["inv1", "inv2"])

    def test_a_promoted_feed_row_joins_urgent(self):
        row = mk("f1")
        row.feed = "inbox"
        quiet = mk("f2")
        groups = views.inbox_groups([row, quiet])
        self.assertEqual(keys(groups[0].items), ["f1"])
        self.assertEqual(keys(views.feed_view([row, quiet], NOW).groups[0]
                              .items), ["f2"])

    def test_direction(self):
        self.assertEqual(mk("a", "needs_reply", "slack",
                            actions=("reply",)).direction, "←")
        self.assertEqual(mk("b", "waiting", "slack",
                            actions=("nudge",)).direction, "→")
        self.assertEqual(mk("c", "fyi").direction, "")
        self.assertEqual(mk("d", "needs_reply", "calendar", actions=("reply",),
                            kind="invite").direction, "")


class WaitingViewTest(unittest.TestCase):
    def test_grouped_by_person_oldest_ask_first(self):
        rows = [
            mk("a1", "waiting", "slack", 5, who="Avery Test"),
            mk("b1", "waiting", "slack", 72, who="Blake Test"),
            mk("a2", "waiting", "slack", 26, who="avery test"),
            mk("x", "needs_reply", "slack"),
        ]
        groups = views.waiting_groups(rows, NOW)
        self.assertEqual([g.title for g in groups], ["Blake Test", "Avery Test"])
        self.assertEqual(keys(groups[1].items), ["a2", "a1"])
        self.assertEqual(groups[0].tail, "1 open · oldest 3d")
        self.assertEqual(groups[1].tail, "2 open · oldest 1d")

    def test_bookmarks_close_the_list_as_tracking(self):
        rows = [
            mk("a1", "waiting", "slack", 5, who="Avery Test"),
            mk("t1", "waiting", "slack", 100, who="Casey Test",
               via="track_reaction"),
            mk("t2", "waiting", "slack", 10, who="Casey Test",
               cls="bookmarked"),
        ]
        groups = views.waiting_groups(rows, NOW)
        self.assertEqual([g.key for g in groups][-1], "tracking")
        self.assertEqual(keys(groups[-1].items), ["t1", "t2"])
        self.assertEqual(keys(groups[0].items), ["a1"])

    def test_via_prefix_only(self):
        self.assertTrue(views.is_bookmark(mk("a", via="track_reaction:eyes")))
        self.assertFalse(views.is_bookmark(mk("b", via="reply")))


class FeedViewTest(unittest.TestCase):
    def test_every_fyi_row_grouped_by_routine(self):
        rows = [mk("c1", routine="cal", ago_h=2), mk("c2", routine="cal"),
                mk("r1", source="recording", routine="rec", ago_h=0.5),
                mk("plain"), mk("u", "urgent", "alert")]
        feed = views.feed_view(rows, NOW)
        names = [g.title for g in feed.groups]
        # newest row first; a row with no routine groups by its source's name
        self.assertEqual(names, ["REC", "CAL", "EVENTS"])
        self.assertEqual(keys(feed.groups[1].items), ["c2", "c1"])
        self.assertEqual(feed.counts, [("rec", 1), ("cal", 2), ("events", 1)])
        self.assertEqual(feed.total, 4)

    def test_window_ages_rows_out_and_show_all_brings_them_back(self):
        rows = [mk("new", routine="cal", ago_h=2),
                mk("old", routine="cal", ago_h=30)]
        self.assertEqual(keys(views.feed_view(rows, NOW).groups[0].items),
                         ["new"])
        self.assertEqual(keys(views.feed_view(rows, NOW, window_h=48)
                              .groups[0].items), ["new", "old"])
        self.assertEqual(keys(views.feed_view(rows, NOW, show_all=True)
                              .groups[0].items), ["new", "old"])

    def test_only_event_rows_age_out_of_the_feed(self):
        events_row = mk("ev", routine="cal", ago_h=30)
        state_row = mk("st", source="slack", routine="reg", ago_h=300,
                       origin="registry")
        feed = views.feed_view([events_row, state_row], NOW)
        self.assertEqual([k for g in feed.groups for k in keys(g.items)],
                         ["st"])
        # a deployment may name its events source anything
        feed = views.feed_view([events_row, state_row], NOW,
                               windowed=("registry",))
        self.assertEqual(sorted(k for g in feed.groups for k in keys(g.items)),
                         ["ev"])

    def test_an_active_alert_is_never_aged_out_hidden_or_digested(self):
        old = mk("a1", source="alert", routine="zoom", ago_h=30)
        gone = mk("a2", source="alert", routine="zoom", ago_h=30,
                  resolved=True)
        for row in (old, gone):
            row.feed = "hide"
        digested = mk("a3", source="alert", routine="zoom", ago_h=2)
        digested.feed = "digest"
        feed = views.feed_view([old, gone, digested], NOW)
        rows = [i for g in feed.groups for i in g.items]
        self.assertEqual(sorted(keys(rows)), ["a1", "a3"])   # not digested
        self.assertEqual(feed.muted, [])
        # a resolved alert is an ordinary row: here it is hidden by its rule
        # (and one that is old would age out)
        aged = mk("a4", source="alert", routine="zoom", ago_h=30,
                  resolved=True)
        self.assertEqual(views.feed_view([aged], NOW).total, 0)

    def test_hidden_rows_are_counted_as_muted(self):
        a, b = mk("a", routine="cal"), mk("b", routine="cal")
        a.feed, a.feed_label = "hide", "cal · rsvp"
        feed = views.feed_view([a, b], NOW)
        self.assertEqual(keys(feed.groups[0].items), ["b"])
        self.assertEqual(feed.muted, [("cal · rsvp", 1)])

    def test_digest_folds_one_row_per_routine_per_day(self):
        # three rows around local noon: one local day whatever the timezone
        noon = datetime.fromtimestamp(NOW).replace(
            hour=12, minute=0, second=0, microsecond=0).timestamp()
        rows = [mk(f"s{n}", source="sheet", routine="sheet-watch",
                   ago_h=(NOW - noon) / H + 0.1 * n) for n in range(1, 4)]
        for r in rows:
            r.feed = "digest"
        feed = views.feed_view(rows, NOW)
        (group,) = feed.groups
        self.assertEqual(len(group.items), 1)
        row = group.items[0]
        self.assertTrue(views.is_digest(row))
        self.assertEqual(len(row.members), 3)
        self.assertTrue(row.gist.startswith("3 changes"))
        self.assertIn("3 changes → 1 row", group.tail)
        self.assertEqual(feed.total, 3)
        self.assertEqual([m.key for m in row.members], ["s3", "s2", "s1"])

    def test_a_digest_has_a_link_only_when_its_rows_share_one(self):
        a = mk("l1", routine="r", ago_h=NOON + 0.3)
        b = mk("l2", routine="r", ago_h=NOON + 0.2)
        for row in (a, b):
            row.feed = "digest"
            row.link = "https://example.com/x"
        (group,) = views.feed_view([a, b], NOW).groups
        self.assertEqual(group.items[0].link, "https://example.com/x")
        b.link = "https://example.com/y"
        (group,) = views.feed_view([a, b], NOW).groups
        self.assertIsNone(group.items[0].link)

    def test_a_digest_is_rebuilt_from_the_members_a_filter_keeps(self):
        rows = [mk(f"d{n}", routine="r", ago_h=NOON + 0.1 * n, who=who)
                for n, who in ((1, "Alpha One"), (2, "Beta Two"),
                               (3, "Alpha Three"))]
        for row in rows:
            row.feed = "digest"
        (group,) = views.feed_view(rows, NOW).groups
        digest = group.items[0]
        part = views.digest_subset(digest, lambda m: "alpha" in m.who.lower())
        self.assertEqual(sorted(m.key for m in part.members), ["d1", "d3"])
        self.assertEqual(part.key, digest.key)
        self.assertTrue(part.gist.startswith("2 changes"))
        self.assertIs(views.digest_subset(digest, lambda m: True), digest)
        self.assertIsNone(views.digest_subset(digest, lambda m: False))
        plain = mk("p")
        self.assertIs(views.digest_subset(plain, lambda m: False), plain)

    def test_saved_to_memory_is_one_more_routine(self):
        saved = [views.saved_item({
            "id": "e1", "source": "slack", "kind": "new", "ts": NOW - 600,
            "title": "A thread", "link": None, "source_id": "slack:x"})]
        feed = views.feed_view([mk("c1", routine="cal")], NOW, saved=saved)
        self.assertEqual([n for n, _ in feed.counts], ["cal", "saved to memory"])
        row = feed.groups[-1].items[0]
        self.assertTrue(views.is_saved_row(row))
        self.assertTrue(row.saved)
        self.assertEqual(row.routine, "saved to memory")
        self.assertEqual(keys(views.only_routine(feed.groups, "saved to memory")
                              [0].items), [row.key])

    def test_only_routine_filters(self):
        feed = views.feed_view([mk("a", routine="x"), mk("b", routine="y")], NOW)
        self.assertEqual(len(views.only_routine(feed.groups, None)), 2)
        self.assertEqual(keys(views.view_rows(
            views.only_routine(feed.groups, "y"))), ["b"])


class FeedSourceMappingTest(unittest.TestCase):
    def test_sheet_events_map_to_a_feed_source(self):
        with tempfile.TemporaryDirectory() as base:
            state = Path(base) / "state"
            state.mkdir()
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(NOW - 600))
            event = events.build("sheet-watch", "change", None, "sheet:r1",
                                 "Plan: 2 rows moved", [], ts=ts)
            (state / "events.jsonl").write_text(json.dumps(event) + "\n")
            res = sources.load_events(base, now=NOW)
        (item,) = res.items
        self.assertEqual(item["source"], "sheet")
        self.assertEqual(item["section"], "fyi")
        self.assertEqual(item["meta"]["routine"], "sheet-watch")
        self.assertEqual(item["meta"]["title"], "Plan: 2 rows moved")
        self.assertEqual(itm.validate_item(item), [])
        row = from_v1(item)
        self.assertEqual(row.routine, "sheet-watch")

    def test_calendar_rows_carry_the_start_time(self):
        with tempfile.TemporaryDirectory() as base:
            state = Path(base) / "state"
            state.mkdir()
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60))
            start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + H))
            event = events.build("cal-cache", "cancel", None, "gcal:e1",
                                 "Sync", [], ts=ts, details={"start": start})
            (state / "events.jsonl").write_text(json.dumps(event) + "\n")
            (item,) = sources.load_events(base, now=NOW).items
        self.assertAlmostEqual(item["meta"]["starts"], NOW + H, delta=1)
        self.assertEqual(item["meta"]["routine"], "cal-cache")


class RuleMatchingTest(unittest.TestCase):
    def rows(self):
        return {
            "rsvp": mk("r", routine="cal", kind="rsvp", title="Standup"),
            "cancel_soon": mk("c1", routine="cal", kind="cancel",
                              title="Review", starts=NOW + 5 * H),
            "cancel_late": mk("c2", routine="cal", kind="cancel",
                              title="Review", starts=NOW + 50 * H),
            "weekly": mk("w", routine="cal", kind="update",
                         title="Weekly sync"),
            "sheet": mk("s", source="sheet", routine="sheet-watch",
                        kind="change", title="Plan"),
        }

    def test_first_match_wins(self):
        rules = [
            {"routine": "cal", "kind": "rsvp", "action": "hide"},
            {"routine": "sheet-watch", "action": "digest"},
            {"routine": "cal", "kind": "cancel", "starts_within_h": 24,
             "action": "inbox"},
            {"routine": "cal", "title_regex": "^Weekly", "action": "hide"},
            {"routine": "cal", "action": "show"},
        ]
        r = self.rows()
        act = lambda row: feedrules.classify(row, rules, NOW)[0]
        self.assertEqual(act(r["rsvp"]), "hide")
        self.assertEqual(act(r["sheet"]), "digest")
        self.assertEqual(act(r["cancel_soon"]), "inbox")  # starts in 5h
        self.assertEqual(act(r["weekly"]), "hide")
        self.assertEqual(act(r["cancel_late"]), "show")

    def test_starts_within_h_promotes_to_inbox(self):
        rules = [{"routine": "cal", "kind": "cancel", "starts_within_h": 24,
                  "action": "inbox"}]
        r = self.rows()
        self.assertEqual(feedrules.classify(r["cancel_soon"], rules, NOW)[0],
                         "inbox")
        self.assertEqual(feedrules.classify(r["cancel_late"], rules, NOW)[0],
                         "show")
        # an event that already started is not "starting within"
        past = mk("p", routine="cal", kind="cancel", starts=NOW - H)
        self.assertEqual(feedrules.classify(past, rules, NOW)[0], "show")

    def test_an_earlier_show_stops_later_rules(self):
        rules = [{"routine": "cal", "kind": "rsvp", "action": "show"},
                 {"routine": "cal", "action": "hide"}]
        r = self.rows()
        self.assertEqual(feedrules.classify(r["rsvp"], rules, NOW)[0], "show")
        self.assertEqual(feedrules.classify(r["weekly"], rules, NOW)[0], "hide")

    def test_no_match_shows(self):
        self.assertEqual(feedrules.classify(self.rows()["sheet"], [], NOW),
                         ("show", None))

    def test_a_rule_with_no_match_key_matches_everything(self):
        self.assertEqual(feedrules.classify(
            mk("x"), [{"action": "hide"}], NOW)[0], "hide")

    def test_source_key_and_title_fallback_to_gist(self):
        row = mk("g1", source="drive")
        self.assertEqual(feedrules.classify(
            row, [{"source": "drive", "title_regex": "^gist", "action": "hide"}],
            NOW)[0], "hide")

    def test_synthetic_rows_are_told_apart(self):
        row = views.saved_item({"id": "e", "source": "slack", "kind": "new",
                                "ts": NOW, "title": "t", "link": None})
        self.assertTrue(views.is_synthetic(row))
        self.assertFalse(views.is_synthetic(mk("x")))

    def test_window_ageing_is_applied_after_rules(self):
        old = mk("old", routine="cal", ago_h=30)
        old.feed = "show"
        self.assertEqual(views.feed_view([old], NOW).total, 0)


class RuleOrderTest(unittest.TestCase):
    def test_ui_rules_come_first_expired_and_off_ones_are_skipped(self):
        ui = [
            {"id": "aaaa", "action": "hide", "routine": "cal", "enabled": True,
             "until": None, "created": 1.0},
            {"id": "bbbb", "action": "hide", "routine": "x", "enabled": False,
             "until": None, "created": 2.0},
            {"id": "cccc", "action": "hide", "routine": "y", "enabled": True,
             "until": NOW - 1, "created": 3.0},
        ]
        config = [{"action": "show", "routine": "cal"}]
        got = feedrules.ordered(ui, config, NOW)
        self.assertEqual([r.get("id") for r in got], ["aaaa", None])
        row = mk("r", routine="cal")
        self.assertEqual(feedrules.classify(row, got, NOW)[0], "hide")

    def test_new_ui_rule_scopes(self):
        row = mk("r", routine="cal", kind="rsvp", title="Standup 2030-01-08")
        kind = feedrules.new_ui_rule(row, "kind", "hide", 86400, NOW)
        self.assertEqual((kind["routine"], kind["kind"], kind["action"]),
                         ("cal", "rsvp", "hide"))
        self.assertEqual(kind["until"], NOW + 86400)
        routine = feedrules.new_ui_rule(row, "routine", "hide", None, NOW)
        self.assertNotIn("kind", routine)
        self.assertIsNone(routine["until"])
        title = feedrules.new_ui_rule(row, "title", "hide", None, NOW)
        self.assertTrue(feedrules.matches(
            title, mk("t", routine="cal", title="Standup 2031-02-09"), NOW))
        self.assertFalse(feedrules.matches(
            title, mk("t", routine="cal", title="Other"), NOW))
        self.assertIsNone(feedrules.new_ui_rule(
            mk("n", routine="cal"), "kind", "hide", None, NOW))


class UiRulesPersistTest(unittest.TestCase):
    def test_rules_live_in_the_state_prefs_not_the_settings(self):
        with tempfile.TemporaryDirectory() as base:
            store = InboxStore(Path(base))
            row = mk("r", routine="cal", kind="rsvp")
            rule = feedrules.new_ui_rule(row, "kind", "hide", None, NOW)
            store.add_feed_rule(rule)
            other = InboxStore(Path(base))        # a second pane
            self.assertEqual([r["id"] for r in other.feed_rules()],
                             [rule["id"]])
            doc = json.loads((Path(base) / "state"
                              / "inbox-state.json").read_text())
            self.assertEqual(doc["prefs"]["feed_rules"][0]["routine"], "cal")
            self.assertFalse((Path(base) / "inbox.yaml").exists())
            self.assertTrue(other.set_feed_rule_enabled(rule["id"], False))
            self.assertFalse(InboxStore(Path(base)).feed_rules()[0]["enabled"])
            self.assertTrue(other.remove_feed_rule(rule["id"]))
            self.assertEqual(InboxStore(Path(base)).feed_rules(), [])
            self.assertFalse(other.remove_feed_rule("nope"))

    def test_marks_and_other_prefs_survive_a_rule_write(self):
        with tempfile.TemporaryDirectory() as base:
            store = InboxStore(Path(base))
            store.set_pref("thread_order", "oldest")
            store.done("x")
            store.add_feed_rule(feedrules.new_ui_rule(
                mk("r", routine="cal"), "routine", "digest", None, NOW))
            again = InboxStore(Path(base))
            self.assertEqual(again.get_pref("thread_order"), "oldest")
            self.assertTrue(again.is_hidden("x"))
            self.assertEqual(len(again.feed_rules()), 1)

    def test_malformed_stored_rules_are_dropped(self):
        with tempfile.TemporaryDirectory() as base:
            state = Path(base) / "state"
            state.mkdir()
            (state / "inbox-state.json").write_text(json.dumps({
                "v": 1, "items": {}, "prefs": {"feed_rules": [
                    {"id": "good1", "action": "hide", "routine": "cal"},
                    {"id": "bad", "action": "explode"},
                    "junk",
                    {"id": "reg1", "action": "hide", "title_regex": "("}]}}))
            self.assertEqual([r["id"] for r in InboxStore(Path(base))
                              .feed_rules()], ["good1"])

    def test_adding_a_rule_prunes_expired_ones_before_the_cap(self):
        clock = [1000.0]
        store = InboxStore(None, clock=lambda: clock[0])
        row = mk("r", routine="cal")
        for n in range(feedrules.MAX_RULES):
            store.add_feed_rule(feedrules.new_ui_rule(
                row, "routine", "hide", 100 + n, clock[0]))
        self.assertEqual(len(store.feed_rules()), feedrules.MAX_RULES)
        clock[0] = 5000.0                       # every one of them has ended
        live = feedrules.new_ui_rule(row, "routine", "digest", None, clock[0])
        store.add_feed_rule(live)
        self.assertEqual([r["id"] for r in store.feed_rules()], [live["id"]])

    def test_a_full_list_drops_the_oldest_live_rule(self):
        store = InboxStore(None, clock=lambda: 1000.0)
        row = mk("r", routine="cal")
        ids = []
        for _ in range(feedrules.MAX_RULES + 1):
            rule = feedrules.new_ui_rule(row, "routine", "hide", None, 1000.0)
            ids.append(rule["id"])
            store.add_feed_rule(rule)
        kept = [r["id"] for r in store.feed_rules()]
        self.assertEqual(len(kept), feedrules.MAX_RULES)
        self.assertNotIn(ids[0], kept)

    def test_memory_store_works_too(self):
        store = InboxStore(None)
        rule = feedrules.new_ui_rule(mk("r", routine="cal"), "routine",
                                     "hide", None, NOW)
        store.add_feed_rule(rule)
        self.assertEqual(len(store.feed_rules()), 1)


class FeedSettingsTest(unittest.TestCase):
    def test_defaults(self):
        cfg = settings.default_settings()
        self.assertEqual(cfg["feed"], {"window_h": 24.0, "rules": []})
        self.assertEqual(cfg["health"], {"heartbeats": []})

    def test_valid_block(self):
        cfg = settings.validate({"feed": {"window_h": 12, "rules": [
            {"routine": "cal", "kind": "rsvp", "action": "hide"},
            {"routine": "sheet", "action": "digest"},
            {"routine": "cal", "title_regex": "^Weekly", "action": "hide"},
            {"routine": "cal", "kind": "cancel", "starts_within_h": 24,
             "action": "inbox"}]}})
        self.assertEqual(cfg["feed"]["window_h"], 12.0)
        self.assertEqual(len(cfg["feed"]["rules"]), 4)

    def test_problems_are_listed(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.validate({"feed": {"window_h": 0, "extra": 1, "rules": [
                {"routine": "cal", "action": "explode"},
                {"title_regex": "(", "action": "hide"},
                {"kind": "x", "starts_within_h": -1, "action": "hide"},
                {"bogus": 1, "action": "hide"}, "text"]}})
        text = " | ".join(ctx.exception.problems)
        for needle in ("feed.window_h", "unknown key 'extra'",
                       "rules[0].action", "rules[1].title_regex",
                       "rules[2].starts_within_h", "unknown key 'bogus'",
                       "rules[4] must be a mapping"):
            self.assertIn(needle, text)


class SettingsHeartbeatTest(unittest.TestCase):
    def test_valid(self):
        cfg = settings.validate({"health": {"heartbeats": [
            {"name": "poller", "path": "state/hb", "stale_min": 10,
             "hours": "sun-thu 09:00-19:00"},
            {"name": "other", "path": "~/hb", "stale_min": 5}]}})
        self.assertEqual(len(cfg["health"]["heartbeats"]), 2)
        self.assertNotIn("hours", cfg["health"]["heartbeats"][1])

    def test_problems(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.validate({"health": {"heartbeats": [
                {"name": "a", "path": "p", "stale_min": 0},
                {"name": "a", "path": "p", "stale_min": 5, "hours": "soon"},
                {"name": "b", "stale_min": 5}, {"name": "c bad", "path": "p",
                                                  "stale_min": 5}]}})
        text = " | ".join(ctx.exception.problems)
        for needle in ("stale_min", "used twice", "hours must look like",
                       ".path", ".name"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
