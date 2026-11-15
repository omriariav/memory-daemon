"""Item format v1 contract, sanitising, settings and bidi mode selection."""
import copy
import json
import unittest

from workspace_daemon.inbox import actions, bidi, items as itm, settings, themes


def good(**over):
    base = {
        "v": 1, "id": "events:abc", "source": "calendar", "section": "fyi",
        "urgency": "digest", "sender": "Ann Example", "where": "calendar",
        "gist": "Moved to Thu", "ts": 1700000000.0, "link": None,
        "thread": [{"who": "when", "ts": 1700000000, "text": "a to b"}],
        "draft": None, "actions": ["ack", "open"], "meta": {"k": [1, 2]},
    }
    base.update(over)
    return base


class ContractTest(unittest.TestCase):
    def test_valid_item_has_no_problems(self):
        self.assertEqual(itm.validate_item(good()), [])

    def test_every_field_is_required(self):
        for key in good():
            bad = good()
            del bad[key]
            self.assertTrue(itm.validate_item(bad), f"missing {key} accepted")

    def test_enums_are_closed(self):
        for key, value in (("source", "fax"), ("section", "later"),
                           ("urgency", "meh"), ("actions", ["reply", "zap"])):
            self.assertTrue(itm.validate_item(good(**{key: value})), key)

    def test_all_declared_values_are_accepted(self):
        for s in itm.SOURCES:
            self.assertEqual(itm.validate_item(good(source=s)), [])
        for s in itm.SECTIONS:
            self.assertEqual(itm.validate_item(good(section=s)), [])
        self.assertEqual(itm.validate_item(good(actions=list(itm.ACTIONS))), [])

    def test_version_must_be_one(self):
        self.assertTrue(itm.validate_item(good(v=2)))
        self.assertTrue(itm.validate_item(good(v=True)))

    def test_gist_is_one_line_up_to_120(self):
        self.assertEqual(itm.validate_item(good(gist="x" * 120)), [])
        self.assertTrue(itm.validate_item(good(gist="x" * 121)))
        self.assertTrue(itm.validate_item(good(gist="two\nlines")))

    def test_id_rules(self):
        self.assertTrue(itm.validate_item(good(id="")))
        self.assertTrue(itm.validate_item(good(id="x" * 201)))
        self.assertTrue(itm.validate_item(good(id="a\x1bb")))

    def test_ts_must_be_a_finite_number(self):
        for bad in ("1700000000", None, float("nan"), float("inf"), -1, True):
            self.assertTrue(itm.validate_item(good(ts=bad)), repr(bad))

    def test_link_is_null_or_http(self):
        self.assertEqual(itm.validate_item(good(link="https://example.com/x")), [])
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://x",
                    "https://a b", 5):
            self.assertTrue(itm.validate_item(good(link=bad)), bad)

    def test_link_rejects_controls_del_and_trailing_newline(self):
        for bad in ("https://example.com/a\x9bb", "https://example.com/\x07",
                    "https://example.com/\n", "https://example.com/a\x7f",
                    "https://example.com/a\x1b[2J", "https://exa mple.com",
                    "https://example.com/a\u2028b", "https://example.com/a\x85b",
                    "https://", "http:///"):
            self.assertTrue(itm.validate_item(good(link=bad)), repr(bad))
            self.assertFalse(itm.valid_link(bad), repr(bad))
        self.assertTrue(itm.valid_link("https://example.com/p?q=1&r=\u05e9#x"))

    def test_open_action_refuses_unsafe_links(self):
        opened = []
        act = actions.LocalActions(None, opener=lambda url: opened.append(url) or True)
        class Row:
            def __init__(self, link):
                self.link = link
        for bad in ("https://example.com/a\x9bb", "https://example.com/\n",
                    "https://example.com/\x07", "file:///etc/passwd", None):
            self.assertFalse(act.open_link(Row(bad)).ok, repr(bad))
        self.assertTrue(act.open_link(Row("https://example.com/ok")).ok)
        self.assertEqual(opened, ["https://example.com/ok"])

    def test_thread_and_draft_shapes(self):
        self.assertTrue(itm.validate_item(good(thread={"who": "x"})))
        self.assertTrue(itm.validate_item(good(thread=[{"who": "x"}])))
        self.assertTrue(itm.validate_item(good(draft=5)))
        self.assertEqual(itm.validate_item(good(draft="hi")), [])

    def test_actions_no_duplicates_and_meta_is_object(self):
        self.assertTrue(itm.validate_item(good(actions=["ack", "ack"])))
        self.assertTrue(itm.validate_item(good(meta=[])))
        self.assertTrue(itm.validate_item(good(meta=None)))

    def test_not_an_object(self):
        self.assertTrue(itm.validate_item([]))
        self.assertTrue(itm.validate_item("x"))


class SanitiseTest(unittest.TestCase):
    def test_escapes_and_controls_are_stripped(self):
        item = itm.parse_item(good(
            sender="\x1b[31mred\x1b[0m‮evil\x07",
            gist="a\nb\tc\x00d",
            thread=[{"who": "w\x1b]0;title\x07", "ts": 1,
                     "text": "line1\r\nline2\x1b[2J⁦x"}],
            draft="d\x1b[1m",
        ))
        self.assertEqual(item["sender"], "redevil")
        self.assertEqual(item["gist"], "a b cd")
        self.assertEqual(item["thread"][0]["who"], "w")
        self.assertEqual(item["thread"][0]["text"], "line1\nline2x")
        self.assertEqual(item["draft"], "d")

    def test_long_gist_is_cut_not_rejected(self):
        item = itm.parse_item(good(gist="word " * 60))
        self.assertLessEqual(len(item["gist"]), 120)
        self.assertTrue(item["gist"].endswith("…"))

    def test_hebrew_and_marks_survive(self):
        item = itm.parse_item(good(gist="שלום‏ ok"))
        self.assertIn("שלום", item["gist"])
        self.assertIn("‏", item["gist"])

    def test_unknown_keys_dropped_and_input_untouched(self):
        raw = good(extra="x")
        before = copy.deepcopy(raw)
        item = itm.parse_item(raw)
        self.assertNotIn("extra", item)
        self.assertEqual(raw, before)

    def test_structure_errors_still_raise(self):
        with self.assertRaises(itm.ItemError):
            itm.parse_item(good(section="nope"))


class ListOutputTest(unittest.TestCase):
    def out(self, **over):
        doc = {"v": 1, "items": [good()], "stale": False, "age_min": 3,
               "error": None}
        doc.update(over)
        return json.dumps(doc)

    def test_valid(self):
        rows, stale, age, err = itm.parse_list_output(self.out())
        self.assertEqual(len(rows), 1)
        self.assertEqual((stale, age, err), (False, 3, None))

    def test_whole_output_errors(self):
        for text in ("", "not json", "[]", '{"v": 2, "items": []}',
                     '{"v": 1}', '{"v": 1, "items": {}}',
                     self.out(stale="yes"), self.out(age_min="x"),
                     self.out(error=5)):
            with self.assertRaises(itm.ItemError, msg=text):
                itm.parse_list_output(text)

    def test_bad_rows_are_dropped_and_reported(self):
        rows, _, _, err = itm.parse_list_output(
            self.out(items=[good(), good(id="b", section="zzz"), good()]))
        self.assertEqual(len(rows), 1)
        self.assertIn("2 item(s) dropped", err)

    def test_source_error_passes_through(self):
        _, _, _, err = itm.parse_list_output(self.out(error="upstream down"))
        self.assertEqual(err, "upstream down")

    def test_act_messages(self):
        req = itm.parse_act_request(json.dumps(
            {"v": 1, "action": "done", "id": "x", "meta": {}}))
        self.assertEqual(req["action"], "done")
        with self.assertRaises(itm.ItemError):
            itm.parse_act_request('{"v": 1, "action": "zap", "id": "x", "meta": {}}')
        self.assertEqual(
            itm.parse_act_response('{"ok": true, "message": "done"}'),
            {"ok": True, "message": "done"})
        with self.assertRaises(itm.ItemError):
            itm.parse_act_response('{"ok": "yes", "message": "x"}')


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = settings.default_settings()
        self.assertEqual(s["bidi_mode"], "auto")
        self.assertEqual(s["theme"], themes.DEFAULT)
        self.assertEqual([x["type"] for x in s["sources"]], ["events"])
        self.assertEqual(s["sources"][0]["window_h"], 48.0)

    def test_full_document(self):
        s = settings.validate({
            "poll_interval_s": 30, "theme": "light", "bidi_mode": "visual",
            "sources": [
                {"name": "events", "type": "events", "window_h": 24,
                 "urgent": [{"source": "gcal", "kind": "cancel",
                             "starts_within_h": 12}]},
                {"name": "mail", "type": "command",
                 "argv": ["python3", "/x.py"], "timeout_s": 10}]})
        self.assertEqual(s["sources"][1]["timeout_s"], 10.0)
        self.assertEqual(s["sources"][0]["urgent"][0]["kind"], "cancel")

    def test_problems_are_all_listed(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.validate({
                "poll_interval_s": 1, "theme": "neon", "bidi_mode": "x",
                "bogus": 1,
                "sources": [{"type": "command", "argv": "ls"},
                            {"type": "events", "urgent": [{"title_regex": "("}]},
                            {"type": "pipe"}]})
        text = " ".join(ctx.exception.problems)
        for needle in ("poll_interval_s", "theme", "bidi_mode", "bogus",
                       "argv", "title_regex", "type"):
            self.assertIn(needle, text)

    def test_command_timeout_must_stay_below_the_poll_interval(self):
        def cfg(poll, timeout=None):
            src = {"name": "c", "type": "command", "argv": ["x"]}
            if timeout is not None:
                src["timeout_s"] = timeout
            return settings.validate({"poll_interval_s": poll, "sources": [src]})
        self.assertEqual(cfg(30, 29)["sources"][0]["timeout_s"], 29.0)
        for poll, timeout in ((30, 30), (30, 45), (10, 60)):
            with self.assertRaises(settings.SettingsError, msg=(poll, timeout)) as ctx:
                cfg(poll, timeout)
            self.assertIn("poll_interval_s", str(ctx.exception))
        # the default timeout adapts instead of failing a short interval
        self.assertEqual(cfg(60)["sources"][0]["timeout_s"], 20.0)
        self.assertEqual(cfg(10)["sources"][0]["timeout_s"], 9.0)
        self.assertEqual(cfg(5)["sources"][0]["timeout_s"], 4.0)

    def test_gmail_account(self):
        self.assertEqual(settings.default_settings()["gmail_account"], 0)
        self.assertEqual(settings.validate({"gmail_account": 3})["gmail_account"], 3)
        for bad in (-1, 1.5, "0", True, None):
            with self.assertRaises(settings.SettingsError, msg=repr(bad)):
                settings.validate({"gmail_account": bad})

    def test_duplicate_source_names_rejected(self):
        with self.assertRaises(settings.SettingsError):
            settings.validate({"sources": [
                {"name": "a", "type": "events"}, {"name": "a", "type": "events"}]})


class BidiModeTest(unittest.TestCase):
    def test_iterm_auto_is_logical(self):
        self.assertEqual(bidi.resolve("auto", {"TERM_PROGRAM": "iTerm.app"}),
                         "logical")

    def test_unknown_terminal_falls_back_to_visual(self):
        self.assertEqual(bidi.resolve("auto", {"TERM_PROGRAM": "weirdterm"}),
                         "visual")
        self.assertEqual(bidi.resolve("auto", {}), "visual")

    def test_explicit_modes_ignore_the_terminal(self):
        env = {"TERM_PROGRAM": "iTerm.app"}
        self.assertEqual(bidi.resolve("visual", env), "visual")
        self.assertEqual(bidi.resolve("logical", {"TERM_PROGRAM": "x"}), "logical")

    def test_lc_terminal_iterm2_survives_tmux(self):
        tmux = {"TERM_PROGRAM": "tmux", "LC_TERMINAL": "iTerm2"}
        self.assertEqual(bidi.resolve("auto", tmux), "logical")
        self.assertEqual(bidi.resolve("auto", {"TERM_PROGRAM": "tmux"}), "visual")
        self.assertEqual(bidi.resolve("auto", {"LC_TERMINAL": "other"}), "visual")
        self.assertEqual(bidi.resolve("visual", tmux), "visual")  # explicit wins

    def test_env_var_overrides_config(self):
        self.assertEqual(bidi.configured_mode("logical", {"INBOX_BIDI": "visual"}),
                         "visual")
        self.assertEqual(bidi.configured_mode("visual", {"INBOX_BIDI": ""}),
                         "visual")
        self.assertEqual(bidi.configured_mode(None, {}), "auto")

    def test_legacy_aliases_and_garbage(self):
        self.assertEqual(bidi.normalize_mode("on"), "visual")
        self.assertEqual(bidi.normalize_mode("OFF"), "logical")
        self.assertEqual(bidi.normalize_mode("???"), "auto")

    def test_iterm_end_to_end_default(self):
        mode = bidi.resolve(bidi.configured_mode(
            "auto", {"TERM_PROGRAM": "iTerm.app"}), {"TERM_PROGRAM": "iTerm.app"})
        self.assertEqual(mode, "logical")

    def test_logical_text_is_untouched(self):
        text = "שלום world"
        self.assertEqual(bidi.visual_lines(text, 80, False), [text])

    def test_rtl_detection(self):
        self.assertTrue(bidi.has_rtl("ש"))
        self.assertFalse(bidi.has_rtl("abc"))
        self.assertTrue(bidi.is_rtl_paragraph("ש abc"))
        self.assertFalse(bidi.is_rtl_paragraph("abc ש"))


if __name__ == "__main__":
    unittest.main()
