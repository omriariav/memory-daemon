"""`open_with` (inbox 0.10.1): `o` can hand an https link to a named macOS
app (`open -a`) instead of the default browser.

The runner and the opener are injected: nothing here spawns `open` or a
browser. Synthetic hosts only."""
import subprocess
import sys
import time
import unittest
from unittest import mock

from workspace_daemon.inbox import items as itm
from workspace_daemon.inbox import models, settings
from workspace_daemon.inbox.actions import LocalActions
from workspace_daemon.inbox.store import InboxStore

RULES = [{"host": "*.example-chat.com", "app": "Example Chat"}]
LINK = "https://team.example-chat.com/archives/C0EXAMPLE01/p1893492060000200"
CHANNEL, THREAD, MESSAGE = ("C0EXAMPLE01", "1893492000.000100",
                            "1893492060.000200")
TEAM = "T0EXAMPLE1"


def item(link=LINK, reply_to=False):
    meta = {"reply_to": {"channel": CHANNEL, "thread_ts": THREAD,
                         "message_ts": MESSAGE}} if reply_to else {}
    return models.from_v1(itm.make_item(
        id="t:1", source="slack", section="needs_reply", urgency="digest",
        sender="Person 1", where="#launch", gist="gist", ts=time.time() - 60,
        link=link, thread=[], draft="", actions=["reply"], meta=meta))


class Runner:
    """Records the argv and kwargs; returns an exit code or raises."""

    def __init__(self, code=0, raises=None):
        self.code, self.raises, self.calls = code, raises, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.code)


class Harness:
    def __init__(self, rules=RULES, runner=None, **kw):
        self.opened = []
        self.runner = runner or Runner()
        self.acts = LocalActions(
            InboxStore(None), base_dir="/b", open_with=rules,
            runner=self.runner,
            opener=lambda u: self.opened.append(u) or True, **kw)


class MatchTest(unittest.TestCase):
    def app(self, link, rules=RULES):
        return Harness(rules).acts.open_app_for(link)

    def test_a_subdomain_matches(self):
        self.assertEqual(self.app(LINK), "Example Chat")

    def test_the_match_ignores_case(self):
        self.assertEqual(self.app("https://TEAM.Example-Chat.COM/x"),
                         "Example Chat")
        self.assertEqual(
            self.app(LINK, [{"host": "*.EXAMPLE-chat.com", "app": "A"}]), "A")

    def test_no_match(self):
        self.assertIsNone(self.app("https://example.org/x"))
        self.assertIsNone(self.app("https://example-chat.com/x"))
        self.assertIsNone(self.app("https://evil-example-chat.com.example.org/"))

    def test_the_first_match_wins(self):
        rules = [{"host": "team.example-chat.com", "app": "First"},
                 {"host": "*.example-chat.com", "app": "Second"}]
        self.assertEqual(self.app(LINK, rules), "First")

    def test_no_rules_means_no_match(self):
        self.assertIsNone(self.app(LINK, []))
        self.assertIsNone(self.app(LINK, None))

    def test_only_https_matches(self):
        for url in ("http://team.example-chat.com/x",
                    "slack://channel?team=T0EXAMPLE1&id=C0EXAMPLE01",
                    "file:///etc/passwd", "not a url", ""):
            self.assertIsNone(self.app(url), url)


@mock.patch.object(sys, "platform", "darwin")
class DarwinOpenTest(unittest.TestCase):
    def test_runs_open_a_with_the_exact_argv(self):
        h = Harness()
        res = h.acts.open_link(item())
        self.assertTrue(res.ok)
        self.assertEqual(res.detail, "opened in Example Chat")
        (argv, kw), = h.runner.calls
        self.assertEqual(argv, ["open", "-a", "Example Chat", LINK])
        self.assertIsInstance(argv, list)
        self.assertEqual(kw["timeout"], 5)
        self.assertIs(kw["check"], False)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw[stream], subprocess.DEVNULL)
        self.assertFalse(kw.get("shell"))
        self.assertEqual(h.opened, [])

    def test_no_match_uses_the_browser(self):
        h = Harness()
        res = h.acts.open_link(item("https://example.org/1"))
        self.assertTrue(res.ok)
        self.assertEqual(h.runner.calls, [])
        self.assertEqual(h.opened, ["https://example.org/1"])
        self.assertEqual(res.detail, "https://example.org/1")

    def test_a_non_https_link_never_goes_to_open_a(self):
        h = Harness([{"host": "*", "app": "Example Chat"}])
        h.acts.open_link(item("http://team.example-chat.com/x"))
        self.assertEqual(h.runner.calls, [])
        self.assertEqual(h.opened, ["http://team.example-chat.com/x"])

    def test_a_slack_scheme_template_uses_the_existing_path(self):
        h = Harness([{"host": "*", "app": "Example Chat"}], team=TEAM,
                    slack_open={"url_template":
                                "slack://channel?team={team}&id={channel}"})
        h.acts.open_link(item(reply_to=True))
        self.assertEqual(h.runner.calls, [])
        self.assertEqual(h.opened, [f"slack://channel?team={TEAM}&id={CHANNEL}"])

    def test_an_https_template_url_goes_to_the_app(self):
        tpl = "https://team.example-chat.com/archives/{channel}/p{message_ts}"
        h = Harness(team=TEAM, slack_open={"url_template": tpl})
        res = h.acts.open_link(item(reply_to=True))
        (argv, _), = h.runner.calls
        self.assertEqual(argv[:3], ["open", "-a", "Example Chat"])
        self.assertEqual(
            argv[3], f"https://team.example-chat.com/archives/{CHANNEL}/p"
                     f"{MESSAGE}")
        self.assertTrue(res.ok)
        self.assertEqual(h.opened, [])

    def test_slack_open_template_without_open_with_is_unchanged(self):
        tpl = "https://app.example-chat.com/client/{team}/{channel}"
        opened = []
        acts = LocalActions(InboxStore(None), base_dir="/b", team=TEAM,
                            opener=lambda u: opened.append(u) or True,
                            slack_open={"url_template": tpl})
        res = acts.open_link(item(reply_to=True))
        self.assertEqual(opened,
                         [f"https://app.example-chat.com/client/{TEAM}/{CHANNEL}"])
        self.assertEqual(res.detail, opened[0])

    def test_a_nonzero_exit_falls_back_to_the_browser(self):
        h = Harness(runner=Runner(code=1))
        res = h.acts.open_link(item())
        self.assertEqual(h.opened, [LINK])
        self.assertTrue(res.ok)
        self.assertEqual(res.detail,
                         "opened in browser (Example Chat: exit 1)")

    def test_a_timeout_falls_back_to_the_browser(self):
        h = Harness(runner=Runner(raises=subprocess.TimeoutExpired("open", 5)))
        res = h.acts.open_link(item())
        self.assertEqual(h.opened, [LINK])
        self.assertEqual(res.detail,
                         "opened in browser (Example Chat: timed out)")

    def test_an_oserror_falls_back_to_the_browser(self):
        h = Harness(runner=Runner(raises=FileNotFoundError("open")))
        res = h.acts.open_link(item())
        self.assertEqual(h.opened, [LINK])
        self.assertTrue(res.ok)
        self.assertIn("opened in browser (Example Chat: ", res.detail)

    def test_a_failing_browser_after_a_failed_app_is_reported(self):
        acts = LocalActions(InboxStore(None), base_dir="/b", open_with=RULES,
                            runner=Runner(code=2), opener=lambda u: False)
        res = acts.open_link(item())
        self.assertFalse(res.ok)
        self.assertIn("no browser available", res.detail)
        self.assertIn("Example Chat: exit 2", res.detail)


class NonDarwinTest(unittest.TestCase):
    def test_other_platforms_use_the_browser_and_never_run_open(self):
        for plat in ("linux", "win32"):
            with mock.patch.object(sys, "platform", plat):
                h = Harness()
                res = h.acts.open_link(item())
            self.assertEqual(h.runner.calls, [], plat)
            self.assertEqual(h.opened, [LINK], plat)
            self.assertTrue(res.ok)
            self.assertEqual(
                res.detail, "opened in browser (Example Chat: only on macOS)")


class SettingsTest(unittest.TestCase):
    def problems(self, doc):
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.validate(doc)
        return " | ".join(ctx.exception.problems)

    def test_off_by_default(self):
        self.assertEqual(settings.default_settings()["open_with"], [])
        self.assertEqual(settings.validate({})["open_with"], [])
        self.assertEqual(settings.validate({"open_with": None})["open_with"], [])

    def test_a_good_list_is_kept_in_order(self):
        rules = [{"host": "*.example-chat.com", "app": "Example Chat"},
                 {"host": "docs.example.org", "app": "My App-2.0_x"}]
        self.assertEqual(settings.validate({"open_with": rules})["open_with"],
                         rules)

    def test_a_bad_app_name_is_refused(self):
        for bad in ("Chat; rm -rf", "a/b", "../x", "", "x" * 65, "-a", 5,
                    None, "Chat\n", "Ch@t"):
            msg = self.problems({"open_with": [
                {"host": "*.example-chat.com", "app": bad}]})
            self.assertIn("open_with[0].app", msg, repr(bad))

    def test_a_bad_host_is_refused(self):
        for bad in ("", "a b", "http://x.com", "x.com/path", "x.com:80",
                    "h?st", "[ab].com", "host!name.example", "x" * 254, 5, None):
            msg = self.problems({"open_with": [
                {"host": bad, "app": "Example Chat"}]})
            self.assertIn("open_with[0].host", msg, repr(bad))

    def test_an_extra_key_is_refused(self):
        msg = self.problems({"open_with": [
            {"host": "*.example-chat.com", "app": "Example Chat",
             "args": "-n"}]})
        self.assertIn("open_with[0]: unknown key 'args'", msg)

    def test_a_missing_key_is_refused(self):
        self.assertIn("open_with[0].app", self.problems(
            {"open_with": [{"host": "*.example-chat.com"}]}))
        self.assertIn("open_with[0].host", self.problems(
            {"open_with": [{"app": "Example Chat"}]}))

    def test_the_shape_is_checked(self):
        for bad in ("Slack", {"host": "x", "app": "y"}, ["x"], 3):
            self.assertIn("open_with", self.problems({"open_with": bad}))
        self.assertIn("up to 20", self.problems(
            {"open_with": [{"host": "a.example.org", "app": "A"}] * 21}))

    def test_the_second_entry_is_named(self):
        msg = self.problems({"open_with": [
            {"host": "a.example.org", "app": "A"},
            {"host": "b.example.org", "app": "B/C"}]})
        self.assertIn("open_with[1].app", msg)
        self.assertNotIn("open_with[0]", msg)


if __name__ == "__main__":
    unittest.main()
