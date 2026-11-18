"""Draft and copy instead of send (inbox 0.7.0): `send.mode` and
`send.draft_tool`, the pinned draft call, the markdown to Slack mrkdwn
conversion, `copy:`, the `slack_open:` URL, and the Textual flow (the confirm
screen offers draft / send / copy, never both unasked).

The bridge is mocked: nothing here calls a real claude, Slack or the network.
The non-Textual classes run without Textual."""
import importlib.util
import threading
import time
import unittest
from unittest import mock

from workspace_daemon import claude_bridge, claude_bridge_hook
from workspace_daemon.inbox import items as itm
from workspace_daemon.inbox import mrkdwn, models, settings, sources, writes
from workspace_daemon.inbox.actions import FakeActions, LocalActions
from workspace_daemon.inbox.store import InboxStore

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox.app import (COPY_TOAST, DRAFT_TOAST,
                                            InboxApp)
    from workspace_daemon.inbox.screens import ConfirmSend

SEND_TOOL = "mcp__slack_connector__send_message"
DRAFT_TOOL = "mcp__slack_connector__send_message_draft"
ARGS = {"channel": "ch", "thread_ts": "tts", "text": "txt"}
CHANNEL, DM, THREAD, MESSAGE = ("C0EXAMPLE01", "D0EXAMPLE01",
                                "1893492000.000100", "1893492060.000200")
TEAM = "T0EXAMPLE1"
SLACK_URL = "slack://channel?team={team}&id={channel}"
WEB_URL = ("https://app.slack.com/client/{team}/{channel}/thread/"
           "{channel}-{thread_ts}")
SIZE = (150, 42)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}


def send_block(**over):
    block = {"tool": SEND_TOOL, "draft_tool": DRAFT_TOOL, "args": dict(ARGS),
             "timeout_s": 90}
    block.update(over)
    return {k: v for k, v in block.items() if v is not None}


def cfg(**over):
    return settings.validate({"send": send_block(**over)})["send"]


def reply_item(i=1, reply_to=None, draft="Thanks, on it.", kind="reply",
               **kw):
    meta = {} if reply_to is False else {"reply_to": reply_to or {
        "channel": CHANNEL, "thread_ts": THREAD, "message_ts": MESSAGE}}
    base = dict(
        id=f"t:{i}", source="slack",
        section="waiting" if kind == "nudge" else "needs_reply",
        urgency="digest", sender=f"Person {i}", where="#launch",
        gist=f"gist {i}", ts=time.time() - 60 * i,
        link=f"https://example.com/{i}", thread=[], draft=draft,
        actions=[kind], meta=meta)
    base.update(kw)
    return itm.make_item(**base)


def obj(*args, **kw):
    """A reply item as the app and LocalActions see it (an `Item`)."""
    return models.from_v1(reply_item(*args, **kw))


def good_call(calls=None, text='{"ok": true, "draft_id": "Dr0EXAMPLE1"}'):
    def call(prompt, tools, **kw):
        if calls is not None:
            calls.append((prompt, tools, kw))
        (name, pins), = tools.items()
        return [{"name": name, "input": dict(pins), "text": text,
                 "is_error": False}]
    return call


def boom(prompt, tools, **kw):
    raise AssertionError("a tool call was made")


# ---------------------------------------------------------------- mrkdwn

class MrkdwnTest(unittest.TestCase):
    """Only unambiguous standard markdown is converted: the drafts usually
    already are Slack text, where a single `*x*` is bold."""

    def conv(self, text):
        return mrkdwn.to_slack_mrkdwn(text)

    def test_the_unambiguous_conversions(self):
        self.assertEqual(self.conv("**bold**"), "*bold*")
        self.assertEqual(self.conv("__bold__"), "*bold*")
        self.assertEqual(self.conv("~~x~~"), "~x~")
        self.assertEqual(self.conv("[text](https://example.com/a)"),
                         "<https://example.com/a|text>")
        self.assertEqual(self.conv("# Heading"), "*Heading*")
        self.assertEqual(self.conv("###### Deep"), "*Deep*")

    def test_bold_whose_body_holds_a_single_star_is_left_alone(self):
        for text in ("**a *b* c**", "**x * y**", "__a *b* c__", "**a *b***"):
            self.assertEqual(self.conv(text), text, text)
        self.assertEqual(self.conv("**a** and **b**"), "*a* and *b*")

    def test_single_star_and_underscore_are_left_alone(self):
        for text in ("*already bold in Slack*", "_italic in Slack_",
                     "*a* and _b_ and *c d*", "x *y* z", "***both***"):
            self.assertEqual(self.conv(text), text, text)
        self.assertEqual(self.conv("mixed *slack bold* and **md bold**"),
                         "mixed *slack bold* and *md bold*")

    def test_link_labels_are_escaped(self):
        self.assertEqual(self.conv("[a & <b>](https://example.com/x)"),
                         "<https://example.com/x|a &amp; &lt;b&gt;>")
        self.assertEqual(self.conv("[Q&A](https://example.com)"),
                         "<https://example.com|Q&amp;A>")

    def test_a_heading_with_bold_inside_is_not_double_wrapped(self):
        self.assertEqual(self.conv("## **Big** one ##"), "*Big one*")
        self.assertEqual(self.conv("# [t](https://example.com)"),
                         "*<https://example.com|t>*")
        self.assertEqual(self.conv("#"), "#")           # not a heading
        self.assertEqual(self.conv("#hashtag"), "#hashtag")

    def test_lists_and_quotes_are_kept_as_they_are(self):
        text = "- a\n* b\n+ c\n  * nested\n1. one\n2. two"
        self.assertEqual(self.conv(text), text)
        self.assertEqual(self.conv("- a **x**\n* b __y__"),
                         "- a *x*\n* b *y*")
        self.assertEqual(self.conv("> quote **b**\n> more"),
                         "> quote *b*\n> more")
        self.assertEqual(self.conv("> - item"), "> - item")

    def test_code_is_never_touched(self):
        self.assertEqual(self.conv("`**no** __x__` and **yes**"),
                         "`**no** __x__` and *yes*")
        self.assertEqual(
            self.conv("pre **b**\n```\n**code** __x__ # h\n```\nafter **b**"),
            "pre *b*\n```\n**code** __x__ # h\n```\nafter *b*")

    def test_tilde_fences_are_protected(self):
        self.assertEqual(
            self.conv("a **b**\n~~~\n**code** ~~x~~\n~~~\nc **d**"),
            "a *b*\n~~~\n**code** ~~x~~\n~~~\nc *d*")
        # a backtick fence is not closed by a tilde fence
        self.assertEqual(self.conv("```\n~~~\n**x**\n```\n**y**"),
                         "```\n~~~\n**x**\n```\n*y*")

    def test_indented_code_blocks_are_protected(self):
        self.assertEqual(
            self.conv("text **a**\n\n    **code** __x__\n    more\n\nafter **b**"),
            "text *a*\n\n    **code** __x__\n    more\n\nafter *b*")
        self.assertEqual(self.conv("\t**tab code**"), "\t**tab code**")

    def test_a_nested_list_is_not_a_code_block(self):
        self.assertEqual(self.conv("- item\n    - nested **b**"),
                         "- item\n    - nested *b*")
        self.assertEqual(self.conv("1. one\n\n    continued **b**"),
                         "1. one\n\n    continued *b*")
        # an indented line straight after a paragraph is a continuation
        self.assertEqual(self.conv("para\n    **b**"), "para\n    *b*")

    def test_urls_keep_their_underscores_and_tildes(self):
        self.assertEqual(
            self.conv("see https://example.com/a_b_c~x and __bold__"),
            "see https://example.com/a_b_c~x and *bold*")
        self.assertEqual(self.conv("[a_b](https://example.com/x_y_z)"),
                         "<https://example.com/x_y_z|a_b>")
        self.assertEqual(self.conv("[a](https://example.com/w_(x)_y)"),
                         "<https://example.com/w_(x)_y|a>")

    def test_a_bare_url_does_not_swallow_closing_markup(self):
        self.assertEqual(self.conv("**https://example.com/a_b**"),
                         "*https://example.com/a_b*")
        self.assertEqual(self.conv("~~https://example.com/c~~ x"),
                         "~https://example.com/c~ x")
        self.assertEqual(self.conv("__https://example.com/d__"),
                         "*https://example.com/d*")
        self.assertEqual(self.conv("*https://example.com/e*"),
                         "*https://example.com/e*")
        self.assertEqual(self.conv("see **https://example.com/x/** now"),
                         "see *https://example.com/x/* now")

    def test_links_edge_cases(self):
        self.assertEqual(self.conv("[](https://example.com)"),
                         "<https://example.com>")
        self.assertEqual(self.conv("![alt](https://example.com/i.png)"),
                         "<https://example.com/i.png|alt>")
        self.assertEqual(self.conv('[t](https://example.com "title")'),
                         "<https://example.com|t>")
        self.assertEqual(self.conv("already <https://example.com|x> here"),
                         "already <https://example.com|x> here")

    def test_plain_text_and_lookalikes_are_unchanged(self):
        for text in ("Thanks, on it.", "2 * 3 * 4", "snake_case_name",
                     "a*b*c", "---", "* * *", "***", "~ alone", "x __ y",
                     "", "line one\n\nline two", "~~~", "a ~b~ c"):
            self.assertEqual(self.conv(text), text, text)

    def test_hebrew_and_bidi_marks_pass_through(self):
        self.assertEqual(
            self.conv("**שלום** עולם __היי__ ~~לא~~ [קישור](https://example.com/he)"),
            "*שלום* עולם *היי* ~לא~ <https://example.com/he|קישור>")
        self.assertEqual(self.conv("# כותרת\n- פריט **חשוב**\n> ציטוט"),
                         "*כותרת*\n- פריט *חשוב*\n> ציטוט")
        self.assertEqual(self.conv("*שלום* _עולם_"), "*שלום* _עולם_")
        self.assertEqual(self.conv("a\u200fb **x**\u200f"),
                         "a\u200fb *x*\u200f")
        self.assertEqual(self.conv("שלום, מה שלומך?"), "שלום, מה שלומך?")

    def test_multi_line_mixed_draft(self):
        draft = ("# Update\n\nHi **team**, the *plan* is in "
                 "[the doc](https://example.com/doc_1).\n\n- item ~~old~~\n"
                 "- item __new__\n\n```\nkeep **this**\n```\n")
        self.assertEqual(
            self.conv(draft),
            "*Update*\n\nHi *team*, the *plan* is in "
            "<https://example.com/doc_1|the doc>.\n\n- item ~old~\n"
            "- item *new*\n\n```\nkeep **this**\n```\n")

    def test_forged_placeholders_are_removed_and_non_strings_are_empty(self):
        self.assertEqual(self.conv("a\ue000" + "0\ue001b"), "a0b")
        self.assertEqual(self.conv(None), "")
        self.assertEqual(self.conv(5), "")


# -------------------------------------------------------------- settings

class SendModeSettingsTest(unittest.TestCase):
    def problems(self, block):
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.validate({"send": block})
        return " | ".join(ctx.exception.problems)

    def test_the_mode_defaults_to_send_for_backward_compatibility(self):
        old = {"tool": SEND_TOOL, "args": dict(ARGS)}       # a 0.6.0 block
        out = settings.validate({"send": old})["send"]
        self.assertEqual(out["mode"], "send")
        self.assertIsNone(out["draft_tool"])
        self.assertEqual(out["tool"], SEND_TOOL)

    def test_all_three_modes(self):
        for mode in ("send", "draft", "both"):
            self.assertEqual(cfg(mode=mode)["mode"], mode)

    def test_a_bad_mode_is_refused(self):
        for bad in ("Send", "draftonly", "", 1, True, ["send"]):
            self.assertIn("send.mode must be one of send, draft, both",
                          self.problems(send_block(mode=bad)))

    def test_draft_and_both_need_a_draft_tool(self):
        for mode in ("draft", "both"):
            self.assertIn("send.draft_tool must be a connector tool name",
                          self.problems(send_block(mode=mode, draft_tool=None)))
            self.assertIn("send.draft_tool must be a connector tool name",
                          self.problems(send_block(mode=mode, draft_tool="x")))

    def test_send_and_both_need_a_send_tool_but_draft_mode_does_not(self):
        for mode in ("send", "both"):
            self.assertIn("send.tool must be a connector tool name",
                          self.problems(send_block(mode=mode, tool=None)))
        out = cfg(mode="draft", tool=None)
        self.assertIsNone(out["tool"])
        self.assertEqual(out["draft_tool"], DRAFT_TOOL)

    def test_a_bad_optional_tool_is_still_refused(self):
        self.assertIn("send.draft_tool must be a connector tool name",
                      self.problems(send_block(mode="send", draft_tool="bad")))
        self.assertIn("send.tool must be a connector tool name",
                      self.problems(send_block(mode="draft", tool="bad")))

    def test_the_two_tools_must_differ(self):
        self.assertIn("must be different tools", self.problems(
            send_block(mode="both", draft_tool=SEND_TOOL)))

    def test_the_draft_uses_the_same_args_mapping(self):
        out = cfg(mode="draft")
        self.assertEqual(out["args"], ARGS)
        self.assertIn("send.args.channel", self.problems(
            send_block(mode="draft", args={"thread_ts": "t", "text": "x"})))

    def test_copy_is_a_bool_and_off_by_default(self):
        self.assertIs(settings.default_settings()["copy"], False)
        self.assertIs(settings.validate({"copy": True})["copy"], True)
        for bad in ("yes", 1, {}, []):
            with self.assertRaises(settings.SettingsError) as ctx:
                settings.validate({"copy": bad})
            self.assertIn("copy must be true or false", str(ctx.exception))


class SlackOpenSettingsTest(unittest.TestCase):
    def problems(self, doc):
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.validate(doc)
        return " | ".join(ctx.exception.problems)

    def test_off_by_default(self):
        out = settings.default_settings()
        self.assertEqual((out["slack_open"], out["team"]), (None, None))

    def test_both_documented_templates_are_valid(self):
        for tpl in (SLACK_URL, WEB_URL):
            out = settings.validate({"team": TEAM,
                                     "slack_open": {"url_template": tpl}})
            self.assertEqual(out["slack_open"], {"url_template": tpl})
            self.assertEqual(out["team"], TEAM)

    def test_the_team_id_format(self):
        for ok in ("T0EXAMPLE1", "TABCDEF", "T" + "A1" * 6):
            self.assertEqual(settings.validate({"team": ok})["team"], ok)
        for bad in ("t0example1", "T123", "T" + "A" * 13, "U0EXAMPLE1",
                    "T0EXAMPLE1 ", "T0-EXAMPL", "", 5, ["T0EXAMPLE1"]):
            self.assertIn("team must be a Slack workspace id",
                          self.problems({"team": bad}))

    def test_the_template_must_start_with_slack_or_https(self):
        for bad in ("http://example.com/{channel}", "javascript:alert(1)",
                    "file:///etc/passwd", "ftp://example.com", "example.com",
                    "", 5, None, " https://example.com"):
            self.assertIn("must be a string starting with slack:// or https://",
                          self.problems({"slack_open": {"url_template": bad}}))

    def test_placeholders_are_checked(self):
        self.assertIn("unknown placeholder {user}", self.problems(
            {"slack_open": {"url_template": "https://example.com/{user}"}}))
        self.assertIn("stray brace", self.problems(
            {"slack_open": {"url_template": "https://example.com/{channel"}}))
        self.assertIn("uses {team}", self.problems(
            {"slack_open": {"url_template": SLACK_URL}}))
        # a template without {team} does not need `team:`
        out = settings.validate({"slack_open": {
            "url_template": "https://example.com/c/{channel}"}})
        self.assertEqual(out["slack_open"]["url_template"],
                         "https://example.com/c/{channel}")

    def test_one_line_no_spaces_and_a_cap(self):
        for bad in ("https://example.com/a b", "https://example.com/\n",
                    "https://example.com/\u00e9", "https://example.com/\u200b",
                    "https://example.com/\x7f", "https://example.com/" + "a" * 500):
            self.assertIn("printable ASCII on one line", self.problems(
                {"slack_open": {"url_template": bad}}))

    def test_shape_problems(self):
        self.assertIn("slack_open must be a mapping",
                      self.problems({"slack_open": "https://example.com"}))
        self.assertIn("slack_open: unknown key 'url'", self.problems(
            {"slack_open": {"url_template": "https://example.com", "url": 1}}))


# -------------------------------------------------------------- slack_open_url

class SlackOpenUrlTest(unittest.TestCase):
    META = {"reply_to": {"channel": CHANNEL, "thread_ts": THREAD,
                         "message_ts": MESSAGE}}

    def url(self, tpl, meta=None, team=TEAM):
        return writes.slack_open_url(tpl, team, self.META if meta is None
                                     else meta)

    def test_the_channel_template(self):
        self.assertEqual(self.url(SLACK_URL),
                         f"slack://channel?team={TEAM}&id={CHANNEL}")

    def test_a_thread_opens_at_the_thread(self):
        self.assertEqual(
            self.url(WEB_URL),
            f"https://app.slack.com/client/{TEAM}/{CHANNEL}/thread/"
            f"{CHANNEL}-{THREAD}")

    def test_thread_ts_is_preferred_and_message_ts_is_the_fallback(self):
        tpl = "https://example.com/{channel}/{thread_ts}/{message_ts}"
        self.assertEqual(self.url(tpl), f"https://example.com/{CHANNEL}/"
                         f"{THREAD}/{MESSAGE}")
        only_msg = {"reply_to": {"channel": CHANNEL, "message_ts": MESSAGE}}
        self.assertEqual(self.url(tpl, only_msg), f"https://example.com/"
                         f"{CHANNEL}/{MESSAGE}/{MESSAGE}")
        only_thread = {"reply_to": {"channel": CHANNEL, "thread_ts": THREAD}}
        self.assertEqual(self.url(tpl, only_thread), f"https://example.com/"
                         f"{CHANNEL}/{THREAD}/{THREAD}")

    def test_a_template_needing_a_missing_value_gives_none(self):
        dm = {"reply_to": {"channel": DM}}
        self.assertIsNone(self.url(WEB_URL, dm))                 # no ts
        self.assertEqual(self.url(SLACK_URL, dm),
                         f"slack://channel?team={TEAM}&id={DM}")
        self.assertIsNone(self.url(SLACK_URL, team=None))        # no team

    def test_without_a_valid_reply_to_there_is_no_url(self):
        for meta in ({}, {"reply_to": "C0EXAMPLE01"},
                     {"reply_to": {"channel": "bad"}},
                     {"reply_to": {"channel": CHANNEL, "thread_ts": "x"}}):
            self.assertIsNone(self.url(SLACK_URL, meta))

    def test_a_bad_template_or_team_is_refused(self):
        self.assertIsNone(self.url("http://example.com/{channel}"))
        self.assertIsNone(self.url(None))
        self.assertIsNone(self.url(SLACK_URL, team="evil&team=1"))

    def test_the_url_is_only_made_of_validated_ids(self):
        meta = {"reply_to": {"channel": CHANNEL, "thread_ts": THREAD},
                "gist": "../../evil", "where": "#x?y=1"}
        self.assertNotIn("evil", self.url(WEB_URL, meta))


class OpenTargetTest(unittest.TestCase):
    def acts(self, opened=None, **kw):
        store = InboxStore(None)
        return LocalActions(
            store, base_dir="/b", opener=(opened.append or (lambda u: True))
            if opened is not None else (lambda u: True), **kw)

    def test_the_template_wins_when_the_item_has_a_reply_to(self):
        opened = []
        acts = LocalActions(InboxStore(None), base_dir="/b",
                            opener=lambda u: opened.append(u) or True,
                            team=TEAM, slack_open={"url_template": WEB_URL})
        item = obj(1)
        res = acts.open_link(item)
        self.assertTrue(res.ok)
        self.assertEqual(opened, [
            f"https://app.slack.com/client/{TEAM}/{CHANNEL}/thread/"
            f"{CHANNEL}-{THREAD}"])
        self.assertEqual(acts.open_target(item), opened[0])

    def test_slack_scheme_is_opened_as_it_is(self):
        opened = []
        acts = LocalActions(InboxStore(None), base_dir="/b",
                            opener=lambda u: opened.append(u) or True,
                            team=TEAM, slack_open={"url_template": SLACK_URL})
        acts.open_link(obj(1))
        self.assertEqual(opened, [f"slack://channel?team={TEAM}&id={CHANNEL}"])

    def test_without_the_setting_o_keeps_the_item_link(self):
        opened = []
        acts = LocalActions(InboxStore(None), base_dir="/b",
                            opener=lambda u: opened.append(u) or True)
        acts.open_link(obj(1))
        self.assertEqual(opened, ["https://example.com/1"])

    def test_an_item_without_reply_to_falls_back_to_its_link(self):
        opened = []
        acts = LocalActions(InboxStore(None), base_dir="/b",
                            opener=lambda u: opened.append(u) or True,
                            team=TEAM, slack_open={"url_template": WEB_URL})
        acts.open_link(obj(1, reply_to=False))
        self.assertEqual(opened, ["https://example.com/1"])

    def test_a_template_that_cannot_be_filled_falls_back_to_the_link(self):
        opened = []
        acts = LocalActions(InboxStore(None), base_dir="/b",
                            opener=lambda u: opened.append(u) or True,
                            team=TEAM, slack_open={"url_template": WEB_URL})
        acts.open_link(obj(1, reply_to={"channel": DM}))   # no thread
        self.assertEqual(opened, ["https://example.com/1"])

    def test_no_url_and_no_link_says_so(self):
        acts = LocalActions(InboxStore(None), base_dir="/b")
        res = acts.open_link(obj(1, reply_to=False, link=None))
        self.assertFalse(res.ok)
        self.assertIsNone(acts.open_target(obj(1, reply_to=False,
                                                      link=None)))


# ------------------------------------------------------------------ run_draft

class RunDraftTest(unittest.TestCase):
    def target(self, **kw):
        return writes.ReplyTo(**dict(
            {"channel": CHANNEL, "thread_ts": THREAD, "message_ts": None},
            **kw))

    def test_pins_are_the_draft_tool_with_channel_text_and_thread_ts(self):
        calls = []
        out = writes.run_draft(cfg(mode="draft"), "/b", self.target(),
                               "Hello", call_tools=good_call(calls))
        self.assertTrue(out.ok)
        self.assertEqual(out.detail, "")
        (prompt, tools, kw), = calls
        self.assertEqual(tools, {DRAFT_TOOL: {"ch": CHANNEL, "tts": THREAD,
                                              "txt": "Hello"}})
        self.assertNotIn(SEND_TOOL, tools)
        self.assertIn(f"Call the tool {DRAFT_TOOL} exactly once", prompt)

    def test_a_dm_without_a_thread_pins_no_thread_ts(self):
        calls = []
        writes.run_draft(cfg(), "/b", self.target(channel=DM, thread_ts=None),
                         "Hi", call_tools=good_call(calls))
        self.assertEqual(calls[0][1], {DRAFT_TOOL: {"ch": DM, "txt": "Hi"}})

    def test_same_safety_as_a_send(self):
        calls = []
        writes.run_draft(cfg(), "/b", self.target(), "x",
                         call_tools=good_call(calls))
        kw = calls[0][2]
        self.assertEqual((kw["attempts"], kw["exact_args"], kw["max_calls"]),
                         (1, True, 1))
        self.assertEqual(kw["timeout"], 90.0)
        self.assertEqual(kw["base_dir"], "/b")

    def test_the_hook_pins_exactly_the_draft_arguments(self):
        pins = writes.draft_pins(cfg(), self.target(), "Hello")
        self.assertEqual(list(pins), [DRAFT_TOOL])
        ok = {"tool_name": DRAFT_TOOL, "tool_input": dict(pins[DRAFT_TOOL])}
        self.assertTrue(claude_bridge_hook.decide(ok, pins, True)[0])
        extra = {"tool_name": DRAFT_TOOL,
                 "tool_input": dict(pins[DRAFT_TOOL], reply_broadcast=True)}
        self.assertFalse(claude_bridge_hook.decide(extra, pins, True)[0])
        changed = {"tool_name": DRAFT_TOOL,
                   "tool_input": dict(pins[DRAFT_TOOL], txt="Hello!")}
        self.assertFalse(claude_bridge_hook.decide(changed, pins, True)[0])
        # the send tool is not on the allowlist of a draft run
        self.assertFalse(claude_bridge_hook.decide(
            {"tool_name": SEND_TOOL, "tool_input": dict(pins[DRAFT_TOOL])},
            pins, True)[0])

    def test_never_retried_even_on_failure(self):
        calls = []

        def call(prompt, tools, **kw):
            calls.append(kw)
            raise claude_bridge.BridgeError("claude exited with code 1")
        out = writes.run_draft(cfg(), "/b", self.target(), "x",
                               call_tools=call)
        self.assertFalse(out.ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["attempts"], 1)

    def test_spend_is_logged_under_the_draft_tag(self):
        seen = []

        def call(prompt, tools, **kw):
            seen.append(claude_bridge.current_tag())
            return good_call()(prompt, tools, **kw)
        writes.run_draft(cfg(), "/b", self.target(), "x", call_tools=call)
        self.assertEqual(seen, ["inbox-draft"])

    def test_attached_draft_exists_in_the_tool_reply(self):
        for text in ('{"ok": false, "error": "attached_draft_exists"}',
                     "Error: attached_draft_exists",
                     "ATTACHED_DRAFT_EXISTS",
                     '{"error": "attached_draft_exists"}'):
            out = writes.run_draft(cfg(), "/b", self.target(), "x",
                                   call_tools=good_call(text=text))
            self.assertEqual((out.ok, out.detail),
                             (False, writes.ATTACHED_DRAFT_MSG), text)
        self.assertEqual(writes.ATTACHED_DRAFT_MSG,
                         "A draft already waits in that conversation; use "
                         "Copy instead")

    def test_the_marker_in_a_clean_ok_reply_is_a_success(self):
        for text in ('{"ok": true, "note": "ATTACHED_DRAFT_EXISTS"}',
                     '{"success": true, "draft": "attached_draft_exists?"}'):
            out = writes.run_draft(cfg(), "/b", self.target(), "x",
                                   call_tools=good_call(text=text))
            self.assertTrue(out.ok, text)

    def test_attached_draft_exists_as_a_tool_error(self):
        def call(prompt, tools, **kw):
            raise claude_bridge.BridgeError(
                f"tool returned an error: {DRAFT_TOOL} (attached_draft_exists)")
        out = writes.run_draft(cfg(), "/b", self.target(), "x",
                               call_tools=call)
        self.assertEqual((out.ok, out.detail),
                         (False, writes.ATTACHED_DRAFT_MSG))

    def test_the_bridge_tags_the_error_with_the_marker_only(self):
        tag = claude_bridge._error_category("x attached_draft_exists y")
        self.assertEqual(tag, " (attached_draft_exists)")
        self.assertEqual(claude_bridge._error_category("something else"), "")

    def test_the_marker_does_not_mean_anything_for_a_send(self):
        out = writes.run_send(
            cfg(), "/b", self.target(), "x",
            call_tools=good_call(text="Message sent attached_draft_exists"))
        self.assertTrue(out.ok)

    def test_failure_wording_never_claims_it_was_sent(self):
        def raising(exc):
            def call(prompt, tools, **kw):
                raise exc
            return call
        cases = [
            raising(claude_bridge.BridgeError("claude run timed out after 9s")),
            raising(claude_bridge.BridgeOutage("slept")),
            raising(claude_bridge.BridgeError("boom")),
            raising(claude_bridge.BridgeError(
                f"tool returned an error: {DRAFT_TOOL}")),
            raising(claude_bridge.BridgeDryRun("dry-run")),
            good_call(text='{"ok": false, "error": "channel_not_found"}'),
            good_call(text="Error: not_in_channel"),
            lambda prompt, tools, **kw: [],
        ]
        for call in cases:
            out = writes.run_draft(cfg(), "/b", self.target(), "x",
                                   call_tools=call)
            self.assertFalse(out.ok)
            self.assertNotRegex(out.detail.lower(), r"(?<!not )\bsent\b",
                                out.detail)
            self.assertNotIn("message was", out.detail)

    def test_unknown_outcomes_say_the_draft_may_have_been_placed(self):
        out = writes.run_draft(cfg(), "/b", self.target(), "x",
                               call_tools=lambda p, t, **kw: (_ for _ in ()).throw(
                                   claude_bridge.BridgeError(
                                       "claude run timed out after 9s")))
        self.assertIn("the draft may have been placed, check Slack", out.detail)

    def test_a_tool_error_says_no_draft_placed(self):
        def call(prompt, tools, **kw):
            raise claude_bridge.BridgeError(
                f"tool returned an error: {DRAFT_TOOL}")
        out = writes.run_draft(cfg(), "/b", self.target(), "x", call_tools=call)
        self.assertTrue(out.detail.startswith("no draft placed: "))

    def test_a_duplicate_run_warns_about_a_duplicate_draft(self):
        out = writes.run_draft(cfg(), "/b", self.target(), "x",
                               call_tools=lambda p, t, **kw: good_call()(
                                   p, t, **kw) * 2)
        self.assertTrue(out.ok)
        self.assertIn("duplicate draft", out.detail)

    def test_nothing_reaches_the_bridge_without_a_target_text_or_draft_tool(self):
        for args in ((None, "x"), (self.target(), ""), (self.target(), "  ")):
            out = writes.run_draft(cfg(), "/b", *args, call_tools=boom)
            self.assertFalse(out.ok)
        out = writes.run_draft(cfg(mode="send", draft_tool=None), "/b",
                               self.target(), "x", call_tools=boom)
        self.assertFalse(out.ok)
        self.assertIn("no draft tool", out.detail)

    def test_the_real_bridge_guard_stops_a_paused_draft_before_any_spawn(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state").mkdir()
            (Path(tmp) / "state" / "PAUSED").write_text("maintenance\n")
            with mock.patch.object(claude_bridge, "_spawn") as spawn:
                out = writes.run_draft(cfg(), tmp, self.target(), "x")
            spawn.assert_not_called()
        self.assertFalse(out.ok)
        self.assertTrue(out.detail.startswith("no draft placed: "))


# -------------------------------------------------------------- LocalActions

class ReplyOptionsTest(unittest.TestCase):
    def acts(self, send=None, copy=False, call=None, store=None):
        return LocalActions(store or InboxStore(None), send=send, copy=copy,
                            base_dir="/b", call_tools=call or boom)

    def test_mode_gating_per_option(self):
        expect = {"send": ("send", "copy"), "draft": ("draft", "copy"),
                  "both": ("draft", "send", "copy")}
        for mode, options in expect.items():
            self.assertEqual(self.acts(cfg(mode=mode)).reply_options(),
                             options, mode)

    def test_the_default_mode_is_the_old_send(self):
        old = settings.validate({"send": {"tool": SEND_TOOL,
                                          "args": dict(ARGS)}})["send"]
        self.assertEqual(self.acts(old).reply_options(), ("send", "copy"))
        self.assertEqual(self.acts(old).send_mode, "send")

    def test_copy_alone_is_enough(self):
        acts = self.acts(copy=True)
        self.assertEqual(acts.reply_options(), ("copy",))
        self.assertTrue(acts.supports_send)
        self.assertTrue(acts.reply_configured)
        self.assertFalse(acts.send_configured)
        self.assertIsNone(acts.send_mode)

    def test_nothing_configured_offers_nothing(self):
        acts = self.acts()
        self.assertEqual(acts.reply_options(), ())
        self.assertFalse(acts.supports_send)
        self.assertFalse(acts.reply_configured)

    def test_the_send_call_itself_is_refused_in_draft_mode(self):
        acts = self.acts(cfg(mode="draft"))
        for fn in (acts.send, acts.nudge):
            res = fn(obj(1), "hello")
            self.assertFalse(res.ok)
            self.assertIn("send.mode is draft", res.detail)
            self.assertIn("not sent", res.detail)

    def test_the_draft_call_is_refused_in_send_mode(self):
        acts = self.acts(cfg(mode="send"))
        res = acts.place_draft(obj(1), "hello")
        self.assertFalse(res.ok)
        self.assertIn("not configured", res.detail)

    def test_place_draft_does_not_mark_the_item_done(self):
        store, calls = InboxStore(None), []
        acts = self.acts(cfg(mode="both"), call=good_call(calls), store=store)
        res = acts.place_draft(obj(1), "hello  \n")
        self.assertTrue(res.ok)
        self.assertEqual(res.detail, "Draft placed")
        self.assertFalse(store.is_hidden("t:1"))
        self.assertEqual(calls[0][1], {DRAFT_TOOL: {
            "ch": CHANNEL, "tts": THREAD, "txt": "hello"}})

    def test_send_still_marks_done_in_both_mode(self):
        store = InboxStore(None)
        acts = self.acts(cfg(mode="both"), call=good_call(None, "Message sent"),
                         store=store)
        self.assertTrue(acts.send(obj(1), "hello").ok)
        self.assertTrue(store.is_hidden("t:1"))

    def test_draft_refusals_say_draft_not_send(self):
        acts = self.acts(cfg(mode="draft"))
        no_dest = acts.place_draft(obj(1, reply_to=False), "x").detail
        self.assertIn("no draft can be placed", no_dest)
        self.assertNotIn("sent", no_dest)
        top = acts.place_draft(obj(1, reply_to={"channel": CHANNEL}), "x").detail
        self.assertIn("draft a top-level message in the channel", top)
        self.assertEqual(acts.place_draft(obj(1), "  ").detail,
                         "There is no text to draft")
        self.assertEqual(writes.send_text("  ")[1], "there is no text to send")

    def test_place_draft_checks_the_destination_and_text_first(self):
        acts = self.acts(cfg(mode="draft"))
        for item, text in ((obj(1, reply_to=False), "x"),
                           (obj(1), "   "),
                           (obj(1), "z" * 4001),
                           (obj(1, reply_to={"channel": CHANNEL}), "x")):
            self.assertFalse(acts.place_draft(item, text).ok)

    def test_a_draft_shares_the_one_write_at_a_time_lock(self):
        gate, started = threading.Event(), threading.Event()

        def slow(prompt, tools, **kw):
            started.set()
            gate.wait(10)
            return good_call()(prompt, tools, **kw)
        acts = self.acts(cfg(mode="both"), call=slow)
        out = []
        t = threading.Thread(target=lambda: out.append(
            acts.place_draft(obj(1), "one")))
        t.start()
        try:
            self.assertTrue(started.wait(5))
            res = acts.place_draft(obj(2), "two")
            self.assertFalse(res.ok)
            self.assertIn("another Slack write is still running", res.detail)
            self.assertFalse(acts.send(obj(3), "three").ok)
        finally:
            gate.set()
            t.join(10)
        self.assertTrue(out[0].ok)

    def test_copy_text_converts_without_any_tool_call(self):
        acts = self.acts(cfg(mode="draft"), call=boom)
        out, err = acts.copy_text(obj(1), "**hi** [x](https://example.com/a_b)")
        self.assertEqual((out, err), ("*hi* <https://example.com/a_b|x>", ""))

    def test_copy_text_needs_no_destination(self):
        acts = self.acts(copy=True, call=boom)
        for item in (obj(1, reply_to=False),
                     obj(1, reply_to={"channel": CHANNEL}),    # no thread
                     obj(1, reply_to={"channel": "#bad"})):
            self.assertEqual(acts.copy_text(item, "**x**"), ("*x*", ""))

    def test_copy_text_refusals(self):
        acts = self.acts(copy=True, call=boom)
        self.assertEqual(acts.copy_text(obj(1), "  \n"),
                         ("", "There is no text to copy"))
        self.assertIn("4000", acts.copy_text(obj(1), "z" * 4001)[1])
        self.assertIn("control or bidi-override",
                      acts.copy_text(obj(1), "a\x1bb")[1])
        off = self.acts(call=boom)
        self.assertEqual(off.copy_text(obj(1), "x"),
                         ("", "Copy is not configured"))

    def test_send_plan_falls_back_to_copy_only_on_a_bad_destination(self):
        for acts in (self.acts(cfg(mode="both")), self.acts(copy=True)):
            for item, why in ((obj(1, reply_to=False), "no valid meta.reply_to"),
                              (obj(1, reply_to={"channel": CHANNEL}),
                               "channel post without a thread")):
                plan = acts.send_plan(item, "hello  ")
                self.assertTrue(plan.ok and plan.copy_only, why)
                self.assertEqual(plan.text, "hello")
                self.assertIn("Draft and Send need a valid thread/DM destination",
                              plan.error)
                self.assertIn(why, plan.error)
        # a bad text still refuses, with the text's reason
        plan = self.acts(copy=True).send_plan(obj(1, reply_to=False), "z" * 4001)
        self.assertFalse(plan.ok)
        self.assertIn("4000", plan.error)
        # a usable destination is a normal plan, not copy-only
        ok = self.acts(cfg(mode="both")).send_plan(
            obj(1, reply_to={"channel": DM}), "x")
        self.assertTrue(ok.ok)
        self.assertFalse(ok.copy_only)

# -------------------------------------------------------------------- Textual

def worker_threads():
    return [t for t in threading.enumerate()
            if t.name.startswith(("inbox-", "Thread-")) and t.is_alive()
            and t is not threading.current_thread()
            and t.name in ("inbox-context", "inbox-summary", "inbox-polish",
                           "inbox-action-choices", "inbox-action-prepare",
                           "inbox-precheck")]


class WorkerCleanup:
    """A thread of the app under test must be gone when the test ends (they
    are not daemon threads, so one left waiting would hang the run at exit).
    The join is bounded and a leftover fails the test."""

    async def asyncTearDown(self):
        deadline = time.monotonic() + 15
        for t in worker_threads():
            t.join(max(0.1, deadline - time.monotonic()))
        self.assertEqual([t.name for t in worker_threads()], [],
                         "a worker thread outlived its test")


class Fixed:
    def __init__(self, rows):
        self.results = [sources.SourceResult("t", rows, False, None, None)]

    def __call__(self):
        return self.results


async def wait_for(pilot, cond, tries=120, step=0.05):
    for _ in range(tries):
        try:
            ok = cond()
        except Exception:                # not built yet; the last try raises
            ok = False
        if ok:
            await pilot.pause()          # run what the change queued (compose)
            return True
        await pilot.pause(step)
    return cond()


async def settle(pilot, app):
    await wait_for(pilot, lambda: app.polled_once, tries=200, step=0.02)
    await pilot.pause(0.25)


def toasts(app):
    return [f"{n.title} {n.message}".strip() for n in app._notifications]


def keys(app):
    return [i.key for i in app.items]


def make_app(rows, actions, store, undo_s=0.15):
    return InboxApp(Fixed(rows), store, actions, settings=dict(DEFAULTS),
                    undo_s=undo_s, leave_s=0, stream_delay=0)


def acts_for(store, call=None, send=None, opened=None, **kw):
    return LocalActions(store, send=send, base_dir="/base",
                        call_tools=call or good_call(),
                        opener=(lambda u: opened.append(u) or True)
                        if opened is not None else None, **kw)


def buttons(modal):
    return [b.id for b in modal.query("#confirm-buttons Button")]


async def open_confirm(pilot, app, key="a"):
    await pilot.press(key)
    await wait_for(pilot, lambda: isinstance(app.screen, ConfirmSend))
    await pilot.pause(0.15)
    return app.screen


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ConfirmOptionsTest(WorkerCleanup, unittest.IsolatedAsyncioTestCase):
    async def run_mode(self, send, expect_buttons, expect_options, **kw):
        store = InboxStore(None)
        app = make_app([reply_item(1)], acts_for(store, send=send, **kw), store,
                       undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app)
            self.assertEqual(buttons(modal), expect_buttons)
            self.assertEqual(modal.options, expect_options)
            self.assertEqual(modal.focused.id, "cancel")        # Cancel default
            return modal

    async def test_mode_both_offers_three_actions_and_cancel(self):
        await self.run_mode(cfg(mode="both"),
                            ["cancel", "draft", "send", "copy"],
                            ("draft", "send", "copy"))

    async def test_mode_draft_has_no_send(self):
        await self.run_mode(cfg(mode="draft"), ["cancel", "draft", "copy"],
                            ("draft", "copy"))

    async def test_mode_send_has_no_draft(self):
        await self.run_mode(cfg(mode="send"), ["cancel", "send", "copy"],
                            ("send", "copy"))

    async def test_copy_alone_without_send(self):
        await self.run_mode(None, ["cancel", "copy"], ("copy",), copy=True)

    async def test_the_confirm_screen_shows_text_ids_and_the_open_url(self):
        send = cfg(mode="both")
        store = InboxStore(None)
        acts = acts_for(store, send=send, team=TEAM,
                        slack_open={"url_template": WEB_URL})
        app = make_app([reply_item(1)], acts, store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app)
            self.assertEqual(modal.text, "Thanks, on it.")
            self.assertEqual(modal.destination, "#launch (thread)")
            route = str(modal.query_one("#confirm-route").render())
            self.assertIn(CHANNEL, route)
            self.assertIn(THREAD, route)
            shown = str(modal.query_one("#confirm-open").render())
            self.assertIn(f"https://app.slack.com/client/{TEAM}/{CHANNEL}"
                          f"/thread/{CHANNEL}-{THREAD}", shown)
            self.assertEqual(str(modal.query_one("#confirm-text").render())
                             .splitlines(), ["Thanks, on it."])
            # every button fits inside the modal box
            box = modal.query_one("#confirm").region
            for b in modal.query("#confirm-buttons Button"):
                self.assertGreaterEqual(b.region.x, box.x, b.id)
                self.assertLessEqual(b.region.right, box.right, b.id)

    async def test_an_unoffered_key_does_nothing(self):
        for send, dead in ((cfg(mode="draft"), "y"), (cfg(mode="send"), "d")):
            calls = []
            store = InboxStore(None)
            app = make_app([reply_item(1)],
                           acts_for(store, good_call(calls), send=send), store,
                           undo_s=0.1)
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                modal = await open_confirm(pilot, app)
                await pilot.press(dead)
                self.assertTrue(await wait_for(
                    pilot, lambda: app.screen is modal))
                self.assertEqual((app.pending, calls), ([], []))
                await pilot.press("escape")
                await pilot.pause(0.2)

    async def test_escape_cancels_everything(self):
        calls = []
        store = InboxStore(None)
        app = make_app([reply_item(1)],
                       acts_for(store, good_call(calls), send=cfg(mode="both")),
                       store, undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await open_confirm(pilot, app)
            await pilot.press("escape")
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, ConfirmSend)))
            self.assertEqual((app.pending, calls), ([], []))
            self.assertTrue(any("Nothing sent, drafted or copied" in t
                                for t in toasts(app)))
            self.assertEqual([r["status"] for r in app.audit
                              if r["action"] == "send"], ["cancelled"])

    async def test_a_nudge_offers_the_same_options(self):
        store = InboxStore(None)
        app = make_app([reply_item(1, kind="nudge")],
                       acts_for(store, send=cfg(mode="both")), store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app, "n")
            self.assertEqual(modal.options, ("draft", "send", "copy"))
            self.assertEqual(modal.title_text, "Nudge in Slack")

    async def test_the_old_flow_still_sends_with_y(self):
        calls = []
        store = InboxStore(None)
        app = make_app([reply_item(1)],
                       acts_for(store, good_call(calls, "Message sent"),
                                send=cfg(mode="send")), store, undo_s=0.15)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await open_confirm(pilot, app)
            await pilot.press("y")
            self.assertTrue(await wait_for(pilot, lambda: calls))
            self.assertEqual(list(calls[0][1]), [SEND_TOOL])
            self.assertTrue(await wait_for(pilot, lambda: store.is_hidden("t:1")))

    async def test_the_demo_stays_a_single_send(self):
        store = InboxStore(None)
        acts = FakeActions(store=store, draft_delay=0)
        app = InboxApp(Fixed([reply_item(1, reply_to=False)]), store, acts,
                       dict(DEFAULTS), demo=True, stream_delay=0, undo_s=0.15,
                       leave_s=0)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app)
            self.assertEqual(modal.options, ("send",))
            self.assertEqual(buttons(modal), ["cancel", "send"])


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class DraftFlowTest(WorkerCleanup, unittest.IsolatedAsyncioTestCase):
    async def test_draft_confirm_undo_window_call_and_the_item_stays(self):
        calls, opened = [], []
        store = InboxStore(None)
        acts = acts_for(store, good_call(calls), send=cfg(mode="both"),
                        opened=opened, team=TEAM,
                        slack_open={"url_template": WEB_URL})
        app = make_app([reply_item(1), reply_item(2)], acts, store, undo_s=0.4)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await open_confirm(pilot, app)
            await pilot.press("d")
            await pilot.pause(0.1)
            [p] = app.pending
            self.assertEqual((p.kind, p.text, p.hide),
                             ("draft", "Thanks, on it.", False))
            self.assertEqual(calls, [])                      # undo window
            self.assertIn(first, keys(app))                  # still listed
            self.assertTrue(await wait_for(pilot, lambda: calls))
            (_p, tools, kw), = calls
            self.assertEqual(tools, {DRAFT_TOOL: {
                "ch": CHANNEL, "tts": THREAD, "txt": "Thanks, on it."}})
            self.assertEqual((kw["attempts"], kw["exact_args"],
                              kw["max_calls"]), (1, True, 1))
            self.assertTrue(await wait_for(pilot, lambda: any(
                "Draft placed in Slack: open it with o and press Enter." in t
                for t in toasts(app))))
            self.assertFalse(store.is_hidden(first))         # not done
            self.assertIn(first, keys(app))
            self.assertFalse(any("Sent to Slack" in t for t in toasts(app)))
            row = next(r for r in app.audit if r["action"] == "draft")
            self.assertEqual(row["status"], "committed")
            self.assertIn("#launch (thread)", row["detail"])
            # `o` now opens the thread
            await pilot.press("o")
            self.assertTrue(await wait_for(pilot, lambda: opened))
            self.assertEqual(opened, [
                f"https://app.slack.com/client/{TEAM}/{CHANNEL}/thread/"
                f"{CHANNEL}-{THREAD}"])
        self.assertEqual(DRAFT_TOAST,
                         "Draft placed in Slack: open it with o and press Enter.")

    async def test_u_inside_the_undo_window_places_no_draft(self):
        calls = []
        store = InboxStore(None)
        app = make_app([reply_item(1)],
                       acts_for(store, good_call(calls), send=cfg(mode="draft")),
                       store, undo_s=1.5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await open_confirm(pilot, app)
            await pilot.press("d")
            self.assertTrue(await wait_for(pilot, lambda: app.pending))
            await pilot.press("u")
            # negative check: it must not happen (waits out the whole window)
            await pilot.pause(2.0)
            self.assertEqual((app.pending, calls), ([], []))
            self.assertIn("t:1", keys(app))

    async def test_quit_inside_the_undo_window_cancels_the_draft(self):
        calls = []
        store = InboxStore(None)
        app = make_app([reply_item(1)],
                       acts_for(store, good_call(calls), send=cfg(mode="draft")),
                       store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await open_confirm(pilot, app)
            await pilot.press("d")
            await pilot.pause(0.1)
            await pilot.press("q")
            self.assertTrue(await wait_for(pilot, lambda: app._exit))
        self.assertEqual(calls, [])
        self.assertEqual([r["status"] for r in app.audit
                          if r["action"] == "draft"], ["cancelled on quit"])

    async def test_attached_draft_exists_says_use_copy_and_keeps_the_item(self):
        store = InboxStore(None)
        call = good_call(text='{"ok": false, "error": "attached_draft_exists"}')
        app = make_app([reply_item(1)],
                       acts_for(store, call, send=cfg(mode="draft")), store,
                       undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await open_confirm(pilot, app)
            await pilot.press("d")
            self.assertTrue(await wait_for(pilot, lambda: any(
                "A draft already waits in that conversation; use Copy instead"
                in t for t in toasts(app))))
            self.assertFalse(any("Draft placed" in t for t in toasts(app)))
            self.assertFalse(any("sent" in t.lower().replace("not sent", "")
                                 for t in toasts(app)
                                 if "waits in that conversation" in t))
            self.assertFalse(store.is_hidden("t:1"))
            self.assertIn("t:1", keys(app))
            row = next(r for r in app.audit if r["action"] == "draft")
            self.assertEqual(row["status"], "failed")

    async def test_a_failure_never_says_the_message_was_sent(self):
        def call(prompt, tools, **kw):
            raise claude_bridge.BridgeError("claude run timed out after 9s")
        store = InboxStore(None)
        app = make_app([reply_item(1)],
                       acts_for(store, call, send=cfg(mode="draft")), store,
                       undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await open_confirm(pilot, app)
            await pilot.press("d")
            self.assertTrue(await wait_for(pilot, lambda: any(
                "draft may have been placed" in t for t in toasts(app))))
            self.assertFalse(any("Sent to Slack" in t for t in toasts(app)))
            self.assertIn("t:1", keys(app))

    async def test_one_slack_write_at_a_time_includes_drafts(self):
        gate, started, calls = threading.Event(), threading.Event(), []

        def slow(prompt, tools, **kw):
            calls.append(tools)
            started.set()
            gate.wait(10)
            return good_call()(prompt, tools, **kw)
        store = InboxStore(None)
        app = make_app([reply_item(1), reply_item(2)],
                       acts_for(store, slow, send=cfg(mode="both")), store,
                       undo_s=0.15)
        try:
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                await open_confirm(pilot, app)
                await pilot.press("d")
                self.assertTrue(await wait_for(pilot, started.is_set))
                app.current_key = "t:2"
                app.redraw(keep="t:2")
                await pilot.press("a")
                # negative check: it must not happen
                await pilot.pause(0.2)
                self.assertNotIsInstance(app.screen, ConfirmSend)
                self.assertTrue(any("already in progress" in t
                                    for t in toasts(app)))
                gate.set()
                await wait_for(pilot, lambda: not app.running)
        finally:
            gate.set()

    async def test_a_running_draft_counts_in_the_quit_guard_and_message(self):
        import contextlib
        import io
        gate, started = threading.Event(), threading.Event()
        err = io.StringIO()

        def slow(prompt, tools, **kw):
            started.set()
            gate.wait(10)
            return good_call()(prompt, tools, **kw)
        store = InboxStore(None)
        app = make_app([reply_item(1)],
                       acts_for(store, slow, send=cfg(mode="draft")), store,
                       undo_s=0.15)
        try:
            with contextlib.redirect_stderr(err):
                async with app.run_test(size=SIZE) as pilot:
                    await settle(pilot, app)
                    await open_confirm(pilot, app)
                    await pilot.press("d")
                    self.assertTrue(await wait_for(pilot, started.is_set))
                    await pilot.press("q")
                    # negative check: it must not happen
                    await pilot.pause(0.2)
                    self.assertFalse(app._exit)
                    await pilot.press("q")
                    self.assertTrue(await wait_for(pilot, lambda: app._exit))
                    row = next(r for r in app.audit if r["action"] == "draft")
                    self.assertEqual(row["status"], "running at quit")
                    said = " ".join(str(x) for x in app._exit_renderables)
                    self.assertIn("may have been placed in your Slack drafts",
                                  said)
                    self.assertNotIn("may have been sent", said)
                    gate.set()
                    self.assertTrue(await wait_for(
                        pilot, lambda: row["status"] == "committed", tries=200))
        finally:
            gate.set()


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class CopyFlowTest(WorkerCleanup, unittest.IsolatedAsyncioTestCase):
    async def copy_once(self, draft, send=None, expect_modes=None, **kw):
        store = InboxStore(None)
        opened = []
        acts = acts_for(store, boom, send=send, opened=opened, **kw)
        app = make_app([reply_item(1, draft=draft)], acts, store, undo_s=30)
        clip = []
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            with mock.patch.object(app, "copy_to_clipboard",
                                   side_effect=clip.append):
                await open_confirm(pilot, app)
                await pilot.press("c")
                self.assertTrue(await wait_for(
                    pilot, lambda: not isinstance(app.screen, ConfirmSend)))
            self.assertNotIsInstance(app.screen, ConfirmSend)
            self.assertEqual(app.pending, [])               # no undo window
            self.assertIn("t:1", keys(app))                 # not done
            self.assertFalse(store.is_hidden("t:1"))
            self.assertEqual(app.running, [])
            return app, clip, toasts(app)

    async def test_copy_puts_mrkdwn_on_the_clipboard_and_calls_nothing(self):
        app, clip, notes = await self.copy_once(
            "# Hi\n**bold** and [doc](https://example.com/a_b)\n- item",
            send=cfg(mode="both"))                    # `boom` fails any call
        self.assertEqual(clip, ["*Hi*\n*bold* and <https://example.com/a_b|doc>\n- item"])
        self.assertTrue(any("Copied for Slack: open it with o and paste" in t
                            for t in notes))
        row = next(r for r in app.audit if r["action"] == "copy")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(COPY_TOAST,
                         "Copied for Slack: open it with o and paste")

    async def test_hebrew_copy(self):
        _app, clip, _ = await self.copy_once("**שלום** [קישור](https://example.com/he)",
                                             send=cfg(mode="draft"))
        self.assertEqual(clip, ["*שלום* <https://example.com/he|קישור>"])

    async def test_copy_works_without_send_at_all(self):
        _app, clip, notes = await self.copy_once("**x**", copy=True)
        self.assertEqual(clip, ["*x*"])

    async def test_the_clipboard_gets_the_text_that_was_confirmed(self):
        _app, clip, _ = await self.copy_once("keep **this**   \n\n",
                                             send=cfg(mode="send"))
        self.assertEqual(clip, ["keep *this*"])       # trailing space gone

    async def test_copy_toast_without_anything_to_open(self):
        store = InboxStore(None)
        app = make_app([reply_item(1, link=None)], acts_for(
            store, boom, send=cfg(mode="draft")), store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            with mock.patch.object(app, "copy_to_clipboard"):
                await open_confirm(pilot, app)
                await pilot.press("c")
                self.assertTrue(await wait_for(
                    pilot, lambda: any("Copied for Slack: open the conversation"
                                       in t for t in toasts(app))))
            self.assertFalse(any("open it with o" in t for t in toasts(app)))
            self.assertTrue(any("Copied for Slack: open the conversation" in t
                                for t in toasts(app)))

    async def test_the_real_clipboard_helper_is_the_osc52_one(self):
        # no patching: the app's own copy_to_clipboard runs (OSC 52)
        store = InboxStore(None)
        app = make_app([reply_item(1, draft="**x**")],
                       acts_for(store, boom, send=cfg(mode="draft")), store,
                       undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await open_confirm(pilot, app)
            await pilot.press("c")
            self.assertTrue(await wait_for(
                pilot, lambda: app.clipboard == "*x*"))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class CopyWithoutDestinationTest(WorkerCleanup, unittest.IsolatedAsyncioTestCase):
    """Copy is offered whenever the text passes the text checks, even when
    the item cannot be sent or drafted to."""

    CASES = {
        "no reply_to": {"reply_to": False},
        "a channel item without a thread": {"reply_to": {"channel": CHANNEL}},
        "a group item without a thread": {"reply_to": {"channel": "G0EXAMPLE01"}},
        "an invalid reply_to": {"reply_to": {"channel": "#launch"}},
    }

    async def copy_case(self, send, kw, row_kw, draft="**hi** there", nudge=False):
        store, opened, clip = InboxStore(None), [], []
        acts = acts_for(store, boom, send=send, opened=opened, **kw)
        app = make_app([reply_item(1, draft=draft, **row_kw)], acts, store,
                       undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            with mock.patch.object(app, "copy_to_clipboard",
                                   side_effect=clip.append):
                modal = await open_confirm(pilot, app, "a")
                self.assertEqual(modal.options, ("copy",))
                self.assertEqual(buttons(modal), ["cancel", "copy"])
                self.assertEqual(modal.focused.id, "cancel")
                warn = (str(modal.query_one("#confirm-warn").render())
                        if modal.query("#confirm-warn") else "")
                await pilot.press("y")                 # not offered
                await pilot.press("d")                 # not offered
                self.assertTrue(await wait_for(
                    pilot, lambda: app.screen is modal))
                await pilot.press("c")
                self.assertTrue(await wait_for(
                    pilot, lambda: not isinstance(app.screen, ConfirmSend)))
            self.assertNotIsInstance(app.screen, ConfirmSend)
            self.assertEqual((app.pending, app.running), ([], []))
            self.assertIn("t:1", keys(app))
            self.assertFalse(store.is_hidden("t:1"))
            await pilot.press("o")                     # falls back to item.link
            self.assertTrue(await wait_for(pilot, lambda: opened))
            return clip, warn, opened

    async def test_copy_is_offered_alone_with_the_reason(self):
        for name, row_kw in self.CASES.items():
            for send in (cfg(mode="both"), cfg(mode="draft"), cfg(mode="send")):
                clip, warn, opened = await self.copy_case(send, {}, row_kw)
                self.assertEqual(clip, ["*hi* there"], name)
                self.assertIn("Draft and Send need a valid thread/DM destination",
                              warn, name)
                self.assertEqual(opened, ["https://example.com/1"], name)

    async def test_copy_only_mode_works_for_every_item_with_text(self):
        for name, row_kw in self.CASES.items():
            clip, warn, opened = await self.copy_case(None, {"copy": True}, row_kw)
            self.assertEqual(clip, ["*hi* there"], name)
            self.assertEqual(warn, "", name)    # no draft or send to warn about
            self.assertEqual(opened, ["https://example.com/1"], name)

    async def test_a_valid_destination_in_copy_only_mode_is_the_same(self):
        clip, warn, _ = await self.copy_case(None, {"copy": True}, {})
        self.assertEqual((clip, warn), (["*hi* there"], ""))

    async def test_a_nudge_without_a_destination_offers_copy_too(self):
        store = InboxStore(None)
        app = make_app([reply_item(1, kind="nudge", reply_to=False)],
                       acts_for(store, boom, send=cfg(mode="both")), store,
                       undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app, "n")
            self.assertEqual(modal.options, ("copy",))
            self.assertEqual(modal.title_text, "Nudge in Slack")

    async def test_the_text_checks_still_refuse(self):
        for draft, needle in (("z" * 4001, "4000"),):
            store = InboxStore(None)
            app = make_app([reply_item(1, draft=draft, reply_to=False)],
                           acts_for(store, boom, send=cfg(mode="both")), store,
                           undo_s=30)
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                await pilot.press("a")
                # negative check: it must not happen
                await pilot.pause(0.3)
                self.assertNotIsInstance(app.screen, ConfirmSend)
                self.assertTrue(any(needle in t for t in toasts(app)), needle)

    async def test_no_precheck_runs_for_a_copy_only_modal(self):
        store = InboxStore(None)
        send = cfg(mode="both", precheck={"argv": ["/bin/false"], "timeout_s": 5})
        acts = acts_for(store, boom, send=send)
        app = make_app([reply_item(1, reply_to=False)], acts, store, undo_s=30)
        with mock.patch.object(acts, "send_precheck",
                               side_effect=AssertionError("precheck ran")):
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                modal = await open_confirm(pilot, app)
                self.assertEqual(modal.options, ("copy",))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ConfirmLayoutTest(WorkerCleanup, unittest.IsolatedAsyncioTestCase):
    async def modal(self, size, send):
        store = InboxStore(None)
        app = make_app([reply_item(1)], acts_for(
            store, boom, send=send, team=TEAM,
            slack_open={"url_template": WEB_URL}), store, undo_s=30)
        return app, store

    async def test_at_80_columns_the_buttons_wrap_instead_of_clipping(self):
        app, _ = await self.modal((80, 40), cfg(mode="both"))
        async with app.run_test(size=(80, 40)) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app)
            self.assertTrue(await wait_for(
                pilot, lambda: modal.query_one("#confirm").region.width > 0))
            box = modal.query_one("#confirm").region
            self.assertLessEqual(box.right, 80)
            self.assertTrue(modal.query_one("#confirm").has_class("narrow"))
            regions = {b.id: b.region for b in
                       modal.query("#confirm-buttons Button")}
            self.assertEqual(set(regions), {"cancel", "draft", "send", "copy"})
            for name, r in regions.items():
                self.assertGreaterEqual(r.x, box.x, name)
                self.assertLessEqual(r.right, box.right, name)
                self.assertLessEqual(r.bottom, box.bottom, name)
                self.assertGreater(r.width, len(name), name)    # not squeezed to nothing
            # wrapped into two rows, nothing overlaps
            self.assertEqual(len({r.y for r in regions.values()}), 2)
            items = list(regions.values())
            for i, a in enumerate(items):
                for b in items[i + 1:]:
                    self.assertFalse(a.intersection(b).area, (a, b))
            # every label is fully shown
            for b in modal.query("#confirm-buttons Button"):
                self.assertGreaterEqual(b.region.width, len(str(b.label)) + 2)

    async def test_wide_terminals_keep_one_row(self):
        app, _ = await self.modal((150, 42), cfg(mode="both"))
        async with app.run_test(size=(150, 42)) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app)
            self.assertFalse(modal.query_one("#confirm").has_class("narrow"))
            self.assertEqual(len({b.region.y for b in
                                  modal.query("#confirm-buttons Button")}), 1)

    async def test_o_opens_the_screens_own_item_after_the_selection_changes(self):
        opened, store = [], InboxStore(None)
        acts = acts_for(store, boom, send=cfg(mode="both"), opened=opened,
                        team=TEAM, slack_open={"url_template": SLACK_URL})
        other = "C0EXAMPLE02"
        rows = [reply_item(1), reply_item(2, reply_to={
            "channel": other, "thread_ts": THREAD, "message_ts": MESSAGE})]
        app = make_app(rows, acts, store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            modal = await open_confirm(pilot, app)
            mine = f"slack://channel?team={TEAM}&id={CHANNEL}"
            # the selection moves under the screen
            app.current_key = "t:2" if first == "t:1" else "t:1"
            await pilot.press("o")
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["action"] == "open" for r in app.audit)))
            self.assertIs(app.screen, modal)
            self.assertEqual(opened, [mine])
            row = [r for r in app.audit if r["action"] == "open"]
            self.assertEqual(len(row), 1)
            self.assertEqual(row[0]["item"], f"Person {first[-1]}")

    async def test_o_opens_the_screens_own_item_after_a_poll_drops_it(self):
        opened, store = [], InboxStore(None)
        acts = acts_for(store, boom, send=cfg(mode="both"), opened=opened,
                        team=TEAM, slack_open={"url_template": SLACK_URL})
        rows = [reply_item(1), reply_item(2, reply_to={
            "channel": "C0EXAMPLE02", "thread_ts": THREAD,
            "message_ts": MESSAGE})]
        app = make_app(rows, acts, store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            target = app.current_key
            url = (f"slack://channel?team={TEAM}&id="
                   f"{CHANNEL if target == 't:1' else 'C0EXAMPLE02'}")
            modal = await open_confirm(pilot, app)
            app.all_rows = [r for r in app.all_rows if r.key != target]
            app.refilter()
            self.assertTrue(await wait_for(
                pilot, lambda: target not in [i.key for i in app.items]))
            # the queue moved on to the other item (the old `o` opened that)
            self.assertNotEqual(app.current_key, target)
            await pilot.press("o")
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["action"] == "open" for r in app.audit)))
            self.assertIs(app.screen, modal)
            self.assertEqual(opened, [url])
            [row] = [r for r in app.audit if r["action"] == "open"]
            self.assertEqual(row["item"], f"Person {target[-1]}")
            self.assertEqual(row["status"], "ok")

    async def test_a_copy_only_screen_has_a_copy_only_footer(self):
        store = InboxStore(None)
        app = make_app([reply_item(1)], acts_for(store, boom, copy=True), store,
                       undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app)
            footer = str(modal.query_one("#confirm-fixed").render())
            self.assertEqual(footer, "Nothing is sent or drafted. Copy is local.")
        store = InboxStore(None)
        app = make_app([reply_item(1)], acts_for(
            store, boom, send=cfg(mode="both")), store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app)
            self.assertIn("Nothing happens until you choose",
                          str(modal.query_one("#confirm-fixed").render()))

    async def test_a_long_draft_keeps_the_title_and_buttons_on_screen(self):
        text = "\n".join(f"line {n} of a long draft" for n in range(80))
        for size in ((80, 24), (80, 30), (150, 24), (150, 42)):
            for send in (cfg(mode="both"), cfg(mode="send")):
                store = InboxStore(None)
                app = make_app([reply_item(1, draft=text)], acts_for(
                    store, boom, send=send, team=TEAM,
                    slack_open={"url_template": WEB_URL}), store, undo_s=30)
                async with app.run_test(size=size) as pilot:
                    await settle(pilot, app)
                    modal = await open_confirm(pilot, app)
                    self.assertTrue(await wait_for(
                        pilot, lambda: modal.query_one("#confirm").region.width > 0))
                    w, h = size
                    for wid in ("#confirm-title", "#confirm-dest"):
                        r = modal.query_one(wid).region
                        self.assertGreaterEqual(r.y, 0, (size, wid))
                        self.assertLess(r.bottom, h, (size, wid))
                    for b in modal.query("#confirm-buttons Button"):
                        self.assertLessEqual(b.region.bottom, h, (size, b.id))
                        self.assertGreaterEqual(b.region.y, 0, (size, b.id))
                    pane = modal.query_one("#confirm-scroll")
                    self.assertGreater(pane.max_scroll_y, 0)
                    self.assertGreaterEqual(pane.region.height, 3)

    async def test_every_mode_fits_at_80_columns(self):
        for send in (cfg(mode="send"), cfg(mode="draft"), cfg(mode="both")):
            app, _ = await self.modal((80, 40), send)
            async with app.run_test(size=(80, 40)) as pilot:
                await settle(pilot, app)
                modal = await open_confirm(pilot, app)
                self.assertTrue(await wait_for(
                    pilot, lambda: modal.query_one("#confirm").region.width > 0))
                box = modal.query_one("#confirm").region
                for b in modal.query("#confirm-buttons Button"):
                    self.assertGreaterEqual(b.region.x, box.x, b.id)
                    self.assertLessEqual(b.region.right, box.right, b.id)
                    self.assertGreaterEqual(b.region.width,
                                            len(str(b.label)) + 2, b.id)

    async def test_o_in_the_modal_opens_what_o_would_open_and_stays(self):
        opened, store = [], InboxStore(None)
        acts = acts_for(store, boom, send=cfg(mode="both"), opened=opened,
                        team=TEAM, slack_open={"url_template": SLACK_URL})
        app = make_app([reply_item(1)], acts, store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            modal = await open_confirm(pilot, app)
            shown = str(modal.query_one("#confirm-open").render())
            self.assertIn("Open (o)", shown)
            self.assertIn(f"slack://channel?team={TEAM}&id={CHANNEL}", shown)
            await pilot.press("o")
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["action"] == "open" for r in app.audit)))
            self.assertEqual(opened, [f"slack://channel?team={TEAM}&id={CHANNEL}"])
            self.assertIs(app.screen, modal)
            self.assertEqual(app.pending, [])


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class KeyMapInvariantTest(unittest.TestCase):
    """0.7.0 adds no key to the inbox: `d`, `y` and `c` keep their meaning in
    the queue, and mean draft / send / copy only inside the confirm screen."""

    def test_the_inbox_bindings_are_unchanged(self):
        actions = {k: b.action for b in InboxApp.BINDINGS
                   for k in b.key.split(",")}
        self.assertEqual((actions["d"], actions["y"], actions["c"],
                          actions["o"], actions["a"], actions["n"]),
                         ("done", "copy_item", "context", "open", "approve",
                          "nudge"))
        bound = [k for b in InboxApp.BINDINGS for k in b.key.split(",")]
        self.assertEqual(len(bound), len(set(bound)))
        letters = {k for k in bound if len(k) == 1 and k.isalpha()}
        self.assertEqual(letters, set(settings.RESERVED_KEYS))

    def test_the_confirm_screen_binds_exactly_its_own_keys(self):
        actions = {k: b.action for b in ConfirmSend.BINDINGS
                   for k in b.key.split(",")}
        self.assertEqual(actions["y"], "choose('send')")
        self.assertEqual(actions["d"], "choose('draft')")
        self.assertEqual(actions["c"], "choose('copy')")
        self.assertEqual(actions["o"], "open_url")
        self.assertEqual(actions["escape"], "cancel")
        # no confirm key collides with another confirm key
        keys_ = [k for b in ConfirmSend.BINDINGS for k in b.key.split(",")]
        self.assertEqual(len(keys_), len(set(keys_)))

    def test_actions_keys_are_unchanged(self):
        for key in "dcyo":
            self.assertNotIn(key, settings.ACTION_KEY_CHOICES)


if __name__ == "__main__":
    unittest.main()
