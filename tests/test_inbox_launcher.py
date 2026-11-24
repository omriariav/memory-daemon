"""The inbox task launcher (0.13.3), the part that needs no Textual: the
`task_launcher:` settings, the protocol (request, reply parsing, a real
command run without a shell), the modal's form state and the badge mapping.
The command is `tests/inbox_fake_launcher.py`; nothing here calls a real
claude, a session tool or the network. Synthetic data only."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from workspace_daemon.inbox import items as itm
from workspace_daemon.inbox import launcher, settings
from workspace_daemon.inbox.actions import LocalActions
from workspace_daemon.inbox.models import from_v1
from workspace_daemon.inbox.store import InboxStore

FAKE = str(Path(__file__).resolve().parent / "inbox_fake_launcher.py")
CFG = {"argv": ["x"], "timeout_s": 30.0, "draft_timeout_s": 90.0,
       "roles": ["alpha", "beta", "gamma"], "models": ["small", "large"],
       "efforts": ["lo", "hi"]}


def row(i=1, **kw):
    base = dict(id=f"t:{i}", source="slack", section="needs_reply",
                urgency="digest", sender=f"Person {i}", where="#room",
                gist=f"Check the rollout plan {i}", ts=time.time() - 60,
                link=f"https://example.com/{i}", thread=[], draft=None,
                actions=["reply"], meta={})
    base.update(kw)
    return itm.make_item(**base)


def agent(i=1, source_key="t:1", ts=None, **meta):
    meta = dict({"kind": "agent", "source_key": source_key, "name": "scout-1",
                 "role": "alpha", "state": "in_progress",
                 "bucket": "working"}, **meta)
    return from_v1(itm.make_item(
        id=f"ag:{i}", source="other", section="fyi", urgency="digest",
        sender="helper", where="agents", gist="working on it",
        ts=ts or time.time() - 30, link=None, thread=[], draft=None,
        actions=[], meta=meta))


def ui_item(i=1, **kw):
    return from_v1(row(i, **kw))


class SettingsTest(unittest.TestCase):
    def validate(self, block=None, **extra):
        raw = {"task_launcher": block} if block is not None else {}
        raw.update(extra)
        return settings.validate(raw)

    def problems(self, block=None, **extra):
        with self.assertRaises(settings.SettingsError) as ctx:
            self.validate(block, **extra)
        return str(ctx.exception)

    GOOD = {"argv": ["python3", "~/launch.py"], "roles": ["alpha", "beta"]}

    def test_it_is_off_by_default(self):
        self.assertIsNone(self.validate()["task_launcher"])
        self.assertIsNone(settings.validate({})["task_launcher"])

    def test_a_minimal_block_gets_the_defaults(self):
        out = self.validate(self.GOOD)["task_launcher"]
        self.assertEqual(out["argv"], ["python3", "~/launch.py"])
        self.assertEqual((out["timeout_s"], out["draft_timeout_s"]),
                         (30.0, 90.0))
        self.assertEqual(out["roles"], ["alpha", "beta"])
        self.assertEqual(out["models"], ["haiku", "sonnet", "opus"])
        self.assertEqual(out["efforts"], ["low", "medium", "high"])

    def test_a_full_block_is_kept(self):
        out = self.validate(dict(
            self.GOOD, timeout_s=12, draft_timeout_s=45,
            models=["small"], efforts=[]))["task_launcher"]
        self.assertEqual((out["timeout_s"], out["draft_timeout_s"]),
                         (12.0, 45.0))
        self.assertEqual((out["models"], out["efforts"]), (["small"], []))

    def test_bad_blocks_are_named(self):
        for bad, text in [
            ("nope", "must be a mapping"),
            (dict(self.GOOD, extra=1), "unknown key 'extra'"),
            ({"roles": ["a"]}, "argv must be a non-empty list"),
            (dict(self.GOOD, argv=[]), "argv must be a non-empty list"),
            (dict(self.GOOD, argv="cmd"), "argv must be a non-empty list"),
            (dict(self.GOOD, argv=["ok", ""]), "argv must be a non-empty"),
            ({"argv": ["x"]}, "task_launcher.roles is required"),
            (dict(self.GOOD, roles=[]), "roles must be a non-empty list"),
            (dict(self.GOOD, roles=["Bad"]), "roles must be a non-empty"),
            (dict(self.GOOD, roles=["-x"]), "roles must be a non-empty"),
            (dict(self.GOOD, roles=["a", "a"]), "roles must be a non-empty"),
            (dict(self.GOOD, roles=[f"r{n}" for n in range(10)]),
             "up to 9"),
            (dict(self.GOOD, roles="alpha"), "roles must be a non-empty"),
            (dict(self.GOOD, models=["Big"]), "models must be a list"),
            (dict(self.GOOD, efforts=[1]), "efforts must be a list"),
            (dict(self.GOOD, timeout_s=4), "timeout_s must be 5 to 300"),
            (dict(self.GOOD, timeout_s=True), "timeout_s must be 5 to 300"),
            (dict(self.GOOD, draft_timeout_s=301),
             "draft_timeout_s must be 5 to 300"),
        ]:
            self.assertIn(text, self.problems(bad), bad)

    def test_t_is_reserved_and_not_a_free_key(self):
        self.assertIn("t", settings.RESERVED_KEYS)
        self.assertNotIn("t", settings.ACTION_KEY_CHOICES)

    def test_a_custom_action_on_t_is_refused_with_a_clear_message(self):
        action = {"key": "t", "label": "Agent card", "argv": ["x"]}
        text = self.problems(None, actions=[action])
        self.assertIn("actions[0].key 't' is already used by the inbox", text)
        self.assertIn("reserved since 0.13.3 to start a task", text)
        self.assertIn("task_launcher", text)
        self.assertIn("another key", text)
        # a different key is fine, even with a launcher configured
        out = self.validate(self.GOOD, actions=[dict(action, key="h")])
        self.assertEqual(out["actions"][0]["key"], "h")

    def test_other_reserved_keys_keep_their_message(self):
        action = {"key": "d", "label": "x", "argv": ["x"]}
        self.assertIn("already used by the inbox",
                      self.problems(None, actions=[action]))


class ProtocolParseTest(unittest.TestCase):
    def draft(self, doc, taken=()):
        text = doc if isinstance(doc, str) else json.dumps(doc)
        return launcher.parse_draft(text, CFG, taken)

    def test_the_request_has_the_contract_shape(self):
        fields = {"name": "n", "role": "alpha", "goal": "g",
                  "acceptance": ["a"], "persona": "p", "template": "t",
                  "model": "", "effort": "", "junk": "dropped"}
        doc = launcher.request("draft", {"id": "x"}, fields)
        self.assertEqual(set(doc), {"v", "action", "item", "fields"})
        self.assertEqual((doc["v"], doc["action"], doc["item"]),
                         (1, "draft", {"id": "x"}))
        self.assertEqual(set(doc["fields"]), set(launcher.FIELD_NAMES))
        self.assertEqual(doc["fields"]["acceptance"], ["a"])

    def test_a_good_draft_reply(self):
        out = self.draft({
            "v": 1, "ok": True, "message": "",
            "fields": {"name": "plan-check", "role": "beta", "model": "large",
                       "effort": "hi", "goal": "Read the plan",
                       "acceptance": ["one", "two"], "persona": "Careful",
                       "template": "research"},
            "templates": ["research", "review"]})
        self.assertTrue(out.ok)
        self.assertEqual(out.fields, {
            "name": "plan-check", "role": "beta", "model": "large",
            "effort": "hi", "goal": "Read the plan",
            "acceptance": ["one", "two"], "persona": "Careful",
            "template": "research"})
        self.assertEqual(out.templates, ["research", "review"])

    def test_unknown_keys_are_ignored(self):
        out = self.draft({"ok": True, "extra": 1, "fields": {
            "goal": "g", "cwd": "/etc", "argv": ["rm"]}, "x": {}})
        self.assertTrue(out.ok)
        self.assertEqual(out.fields, {"goal": "g"})

    def test_bad_replies_raise_item_errors(self):
        for bad in ("not json", "[1]", '"s"', "{}", '{"ok": "yes"}',
                    '{"ok": 1}', '{"v": 2, "ok": true}',
                    '{"v": true, "ok": true}'):
            with self.assertRaises(itm.ItemError, msg=bad):
                self.draft(bad)

    def test_ok_false_carries_the_message_and_no_fields(self):
        out = self.draft({"ok": False, "message": "no model today\nmore",
                          "fields": {"goal": "g"}})
        self.assertFalse(out.ok)
        self.assertEqual((out.fields, out.templates), ({}, []))
        self.assertEqual(out.message, "no model today")

    def test_fields_the_config_does_not_allow_are_dropped(self):
        out = self.draft({"ok": True, "fields": {
            "role": "admin", "model": "huge", "effort": "max",
            "name": "Bad Name", "template": "../etc"}})
        self.assertEqual(out.fields, {})

    def test_a_blank_model_or_effort_is_the_launcher_default(self):
        out = self.draft({"ok": True, "fields": {"model": "", "effort": "",
                                                 "role": ""}})
        self.assertEqual(out.fields, {"model": "", "effort": ""})

    def test_a_taken_name_is_dropped(self):
        out = self.draft({"ok": True, "fields": {"name": "busy"}},
                         taken={"busy"})
        self.assertEqual(out.fields, {})

    def test_text_is_cleaned_and_capped(self):
        out = self.draft({"ok": True, "fields": {
            "goal": "one\nline\x1b[31m red\x1b[0m " + "x" * 400,
            "acceptance": ["a\nb"] + [f"item {n}" for n in range(8)]
            + [3, None],
            "persona": "p" * 900}, "templates": [f"t{n}" for n in range(30)]
            + ["t1", "bad/name"]})
        goal = out.fields["goal"]
        self.assertNotIn("\n", goal)
        self.assertNotIn("\x1b", goal)
        self.assertLessEqual(len(goal), launcher.GOAL_MAX)
        self.assertEqual(len(out.fields["acceptance"]), 5)
        self.assertEqual(out.fields["acceptance"][0], "a b")
        self.assertLessEqual(len(out.fields["persona"]), launcher.PERSONA_MAX)
        self.assertEqual(len(out.templates), launcher.MAX_TEMPLATES)
        self.assertEqual(len(set(out.templates)), len(out.templates))
        self.assertNotIn("bad/name", out.templates)

    def test_wrong_field_types_are_dropped_not_raised(self):
        out = self.draft({"ok": True, "fields": {
            "goal": 5, "acceptance": "text", "persona": ["x"], "name": 3,
            "role": ["alpha"]}, "templates": "x"})
        self.assertEqual((out.fields, out.templates), ({}, []))
        self.assertTrue(self.draft({"ok": True, "fields": "nope"}).ok)

    def test_a_launch_reply(self):
        out = launcher.parse_launch(json.dumps(
            {"v": 1, "ok": True, "name": "plan-check", "message": "started"}))
        self.assertEqual(out, launcher.Launched(True, "plan-check", "started"))

    def test_a_failed_launch_keeps_the_first_line_of_the_message(self):
        out = launcher.parse_launch(json.dumps(
            {"ok": False, "message": "\n  refused: name taken\nsecond"}))
        self.assertEqual((out.ok, out.message),
                         (False, "refused: name taken"))

    def test_a_bad_launch_name_is_dropped_and_extra_keys_ignored(self):
        out = launcher.parse_launch(json.dumps(
            {"ok": True, "name": "Bad Name!", "extra": [1]}))
        self.assertEqual((out.ok, out.name), (True, ""))

    def test_bad_launch_replies_raise(self):
        for bad in ("nope", "[]", "{}", '{"ok": "yes"}', '{"ok": null}',
                    '{"ok": true, "message": 5}'):
            with self.assertRaises(itm.ItemError, msg=bad):
                launcher.parse_launch(bad)

    def test_first_line(self):
        self.assertEqual(launcher.first_line("\n\n a  b \nc"), "a b")
        self.assertEqual(launcher.first_line(None), "")
        self.assertLessEqual(len(launcher.first_line("x" * 900)),
                             launcher.MESSAGE_MAX)


class RunTest(unittest.TestCase):
    """The real call: the command is the fake launcher, run without a shell."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg = dict(CFG, argv=[sys.executable, FAKE, str(self.root)],
                        timeout_s=10.0, draft_timeout_s=10.0)

    def config(self, **doc):
        (self.root / "config.json").write_text(json.dumps(doc))

    def requests(self):
        text = (self.root / "requests.jsonl").read_text()
        return [json.loads(line) for line in text.splitlines() if line]

    FIELDS = {"name": "plan-check", "role": "alpha", "goal": "Read it"}

    def test_draft_round_trip_sends_json_on_stdin_only(self):
        self.config(draft={"v": 1, "ok": True, "fields": {
            "goal": "Better goal"}, "templates": ["research"]})
        secret = "SECRET; touch /tmp/pwned $(id) `id`"
        item = {"v": 1, "id": "t:1", "gist": secret}
        out = launcher.run_draft(self.cfg, item, self.FIELDS)
        self.assertTrue(out.ok)
        self.assertEqual(out.fields, {"goal": "Better goal"})
        (req,) = self.requests()
        self.assertEqual((req["v"], req["action"]), (1, "draft"))
        self.assertEqual(req["item"]["gist"], secret)
        self.assertEqual(req["fields"]["name"], "plan-check")
        argv = json.loads((self.root / "argv.json").read_text())
        self.assertEqual(argv, [FAKE, str(self.root)])      # no item text
        self.assertFalse(Path("/tmp/pwned").exists())

    def test_draft_failures_never_raise(self):
        for reply, text in [("badjson", "bad response"),
                            ("notobject", "bad response"),
                            ("exit", "exit 3"),
                            ({"ok": False, "message": "busy"}, "busy"),
                            ({"ok": False}, "could not draft"),
                            ({"v": 9, "ok": True}, "bad response")]:
            self.config(draft=reply)
            out = launcher.run_draft(self.cfg, {"id": "x"}, self.FIELDS)
            self.assertFalse(out.ok, reply)
            self.assertIn(text, out.message)
            self.assertEqual(out.fields, {})

    def test_a_missing_command_is_a_message(self):
        cfg = dict(self.cfg, argv=["/nonexistent/launcher"])
        out = launcher.run_draft(cfg, {"id": "x"}, self.FIELDS)
        self.assertFalse(out.ok)
        self.assertIn("cannot run", out.message)
        done = launcher.run_launch(cfg, {"id": "x"}, self.FIELDS)
        self.assertFalse(done.ok)
        self.assertIn("cannot run", done.message)

    def test_a_slow_draft_times_out_and_can_be_cancelled(self):
        self.config(draft={"ok": True}, sleep_draft=5)
        out = launcher.run_draft(dict(self.cfg, draft_timeout_s=0.5),
                                 {"id": "x"}, self.FIELDS)
        self.assertFalse(out.ok)
        self.assertIn("timed out", out.message)
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        out = launcher.run_draft(self.cfg, {"id": "x"}, self.FIELDS,
                                 cancel=cancel)
        self.assertEqual(out.message, "cancelled")
        self.assertLess(time.monotonic() - started, 4)

    def test_launch_round_trip(self):
        self.config(launch={"v": 1, "ok": True, "name": "plan-check",
                            "message": "started in a new tab"})
        out = launcher.run_launch(self.cfg, {"id": "t:1"}, self.FIELDS)
        self.assertEqual(out, launcher.Launched(
            True, "plan-check", "started in a new tab"))
        (req,) = self.requests()
        self.assertEqual(req["action"], "launch")

    def test_launch_ok_false_exit_and_bad_json_are_failures(self):
        for reply, text in [({"ok": False, "message": "refused"}, "refused"),
                            ({"ok": False}, "reported a failure"),
                            ("badjson", "bad response"), ("exit", "exit 3")]:
            self.config(launch=reply)
            out = launcher.run_launch(self.cfg, {"id": "x"}, self.FIELDS)
            self.assertFalse(out.ok, reply)
            self.assertIn(text, out.message)

    def test_a_launch_timeout_says_it_may_have_started(self):
        self.config(launch={"ok": True}, sleep_launch=5)
        out = launcher.run_launch(dict(self.cfg, timeout_s=0.5),
                                  {"id": "x"}, self.FIELDS)
        self.assertFalse(out.ok)
        self.assertIn("timed out", out.message)
        self.assertIn("may still have started", out.message)

    def test_local_actions_backend(self):
        self.config(draft={"ok": True, "fields": {"goal": "G"}},
                    launch={"ok": True, "name": "plan-check",
                            "message": "started"})
        acts = LocalActions(InboxStore(None), base_dir=self.root,
                            task_launcher=self.cfg)
        self.assertTrue(acts.task_launcher_configured)
        item = ui_item()
        draft = acts.task_draft(item, self.FIELDS, taken={"x"})
        self.assertEqual(draft.fields, {"goal": "G"})
        res = acts.task_launch(item, self.FIELDS)
        self.assertEqual((res.ok, res.name, res.detail),
                         (True, "plan-check", "started"))
        sent = self.requests()[-1]
        self.assertEqual(sent["item"]["id"], "t:1")
        self.assertEqual(sent["item"]["gist"], item.gist)

    def test_local_actions_without_a_launcher(self):
        acts = LocalActions(InboxStore(None), base_dir=self.root)
        self.assertFalse(acts.task_launcher_configured)
        self.assertFalse(acts.task_draft(ui_item(), self.FIELDS).ok)
        self.assertFalse(acts.task_launch(ui_item(), self.FIELDS).ok)


class FormTest(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(launcher.slug("Check the Rollout, plan #1!"),
                         "check-the-rollout-plan-1")
        self.assertEqual(launcher.slug("  --x--  "), "x")
        self.assertEqual(launcher.slug("שלום עולם"), "task")
        self.assertEqual(launcher.slug(""), "task")
        long = launcher.slug("word " * 30)
        self.assertLessEqual(len(long), launcher.NAME_MAX)
        self.assertFalse(long.endswith("-"))
        for text in ("Check the Rollout", "x" * 80, "9 lives", "a-b"):
            self.assertRegex(launcher.slug(text), launcher.NAME_RE)

    def test_unique_name(self):
        self.assertEqual(launcher.unique_name("scout", set()), "scout")
        self.assertEqual(launcher.unique_name("scout", {"scout"}), "scout-2")
        self.assertEqual(launcher.unique_name(
            "scout", {"scout", "scout-2", "scout-3"}), "scout-4")
        base = "x" * launcher.NAME_MAX
        out = launcher.unique_name(base, {base})
        self.assertRegex(out, launcher.NAME_RE)
        self.assertTrue(out.endswith("-2"))

    def test_local_defaults(self):
        form = launcher.TaskForm.local(ui_item(), CFG, {"check-the-rollout"})
        self.assertEqual(form.name, "check-the-rollout-plan-1")
        self.assertEqual(form.role, "alpha")             # roles[0]: no match
        self.assertEqual((form.model, form.effort), ("", ""))
        self.assertEqual(form.goal, "Look into: Check the rollout plan 1")
        self.assertEqual(form.roles, ["alpha", "beta", "gamma"])
        self.assertEqual(form.touched, set())
        self.assertEqual(form.problem(), "")

    def test_a_taken_name_gets_a_suffix(self):
        form = launcher.TaskForm.local(
            ui_item(), CFG, {"check-the-rollout-plan-1"})
        self.assertEqual(form.name, "check-the-rollout-plan-1-2")

    def test_the_default_role_follows_the_row(self):
        cfg = dict(CFG, roles=["researcher", "analyst", "reviewer"])
        pr = ui_item(link="https://git.example.com/org/repo/pull/42")
        self.assertEqual(launcher.row_kind(pr), "pr")
        self.assertEqual(launcher.TaskForm.local(pr, cfg).role, "reviewer")
        self.assertIn("Review", launcher.TaskForm.local(pr, cfg).goal)
        mr = ui_item(2, link=None, gist="see https://g.example.com/a/b/"
                     "-/merge_requests/7 please")
        self.assertEqual(launcher.row_kind(mr), "pr")
        decision = ui_item(3, source="other", actions=[], meta={
            "kind": "task", "task_type": "decision"})
        self.assertEqual(launcher.row_kind(decision), "decision")
        self.assertEqual(launcher.TaskForm.local(decision, cfg).role,
                         "analyst")
        self.assertEqual(launcher.TaskForm.local(ui_item(4), cfg).role,
                         "researcher")
        todo = ui_item(5, source="other", actions=[], meta={
            "kind": "task", "task_type": "todo"})
        self.assertEqual(launcher.TaskForm.local(todo, cfg).role,
                         "researcher")
        # a configured list without the preferred name: the first role
        only = dict(CFG, roles=["alpha"])
        self.assertEqual(launcher.TaskForm.local(pr, only).role, "alpha")

    def test_a_draft_fills_only_untouched_fields(self):
        form = launcher.TaskForm.local(ui_item(), CFG)
        form.edit("goal", "my own goal")
        form.pick_role(3)
        draft = launcher.Draft(True, {
            "name": "drafted-name", "role": "beta", "goal": "drafted goal",
            "model": "large", "acceptance": ["done when x"],
            "persona": "Careful reader", "template": "review"},
            ["research", "review"], "")
        done = form.apply_draft(draft)
        self.assertEqual(sorted(done), ["acceptance", "model", "name",
                                        "persona", "template"])
        self.assertEqual((form.goal, form.role), ("my own goal", "gamma"))
        self.assertEqual((form.name, form.model), ("drafted-name", "large"))
        self.assertEqual(form.acceptance, ["done when x"])
        self.assertEqual(form.persona, "Careful reader")
        self.assertEqual(form.templates, ["research", "review"])
        self.assertEqual(form.template, "review")

    def test_a_failed_draft_changes_nothing(self):
        form = launcher.TaskForm.local(ui_item(), CFG)
        before = form.fields()
        self.assertEqual(form.apply_draft(
            launcher.Draft(False, {"goal": "x"}, [], "no")), [])
        self.assertEqual(form.fields(), before)

    def test_chips(self):
        form = launcher.TaskForm.local(ui_item(), CFG)
        self.assertFalse(form.pick_role(0))
        self.assertFalse(form.pick_role(4))
        self.assertTrue(form.pick_role(2))
        self.assertEqual(form.role, "beta")
        seen = []
        for _ in range(4):
            form.cycle("model")
            seen.append(form.model)
        self.assertEqual(seen, ["small", "large", "", "small"])
        form.cycle("effort", -1)
        self.assertEqual(form.effort, "hi")
        self.assertEqual(form.touched, {"role", "model", "effort"})
        form.cycle("template")                    # no templates: stays blank
        self.assertEqual(form.template, "")

    def test_problems(self):
        form = launcher.TaskForm.local(ui_item(), CFG, {"taken-name"})
        for key, value, text in [
            ("name", "Bad Name", "a-z, 0-9 and dashes"),
            ("name", "-x", "starting with a letter or digit"),
            ("name", "", "a-z, 0-9"), ("name", "x" * 42, "1 to 41"),
            ("name", "taken-name", "is taken"),
            ("role", "nobody", "pick a role"),
            ("goal", "   ", "a goal is required")]:
            f = launcher.TaskForm.local(ui_item(), CFG, {"taken-name"})
            f.edit(key, value)
            self.assertIn(text, f.problem(), (key, value))
        self.assertEqual(form.problem(), "")

    def test_fields_is_the_request_shape(self):
        form = launcher.TaskForm.local(ui_item(), CFG)
        form.edit("goal", "  trim me  ")
        out = form.fields()
        self.assertEqual(set(out), set(launcher.FIELD_NAMES))
        self.assertEqual(out["goal"], "trim me")
        self.assertIsNot(out["acceptance"], form.acceptance)


class BadgeTest(unittest.TestCase):
    def test_an_agent_row_with_a_source_key_is_a_card(self):
        card = launcher.card_of(agent())
        self.assertEqual(card, launcher.Card(
            "t:1", "scout-1", "alpha", "in progress", False, None))
        self.assertEqual(launcher.badge_text(card),
                         "▶ scout-1 · alpha · in progress")

    def test_rows_that_are_not_cards(self):
        self.assertIsNone(launcher.card_of(ui_item()))
        self.assertIsNone(launcher.card_of(agent(source_key="")))
        self.assertIsNone(launcher.card_of(agent(source_key=7)))
        self.assertIsNone(launcher.card_of(agent(source_key="k" * 400)))
        plain = agent()
        plain.meta.pop("source_key")
        self.assertIsNone(launcher.card_of(plain))
        task = agent()
        task.meta["kind"] = "task"
        self.assertIsNone(launcher.card_of(task))

    def test_a_source_that_leaves_fields_out(self):
        card = launcher.card_of(agent(name=None, role=None, state=None,
                                      bucket="needs_you"))
        self.assertEqual((card.name, card.role, card.state),
                         ("helper", "", "needs you"))
        self.assertEqual(launcher.badge_text(card), "▶ helper · needs you")

    def test_completed_by_state_or_bucket(self):
        self.assertTrue(launcher.card_of(agent(state="completed")).completed)
        self.assertTrue(launcher.card_of(
            agent(state="blocked", bucket="done")).completed)
        self.assertFalse(launcher.card_of(agent(state="blocked")).completed)

    def test_the_report_link_is_the_rows_link_when_valid(self):
        row_ = agent(state="completed")
        row_.link = "https://example.com/report"
        self.assertEqual(launcher.card_of(row_).link,
                         "https://example.com/report")
        row_.link = "javascript:alert(1)"
        self.assertIsNone(launcher.card_of(row_).link)

    def test_the_badge_text_is_cleaned_and_cut(self):
        card = launcher.card_of(agent(name="a\x1b[31m" + "n" * 90,
                                      state="x\ny"))
        self.assertNotIn("\x1b", card.name)
        self.assertLessEqual(len(card.name), launcher.LABEL_MAX)
        self.assertEqual(card.state, "x y")

    def test_index_prefers_an_open_card_then_the_newest(self):
        now = time.time()
        old_open = agent(1, name="old-open", ts=now - 500)
        new_open = agent(2, name="new-open", ts=now - 10)
        done = agent(3, name="finished", state="completed", ts=now - 1)
        other = agent(4, source_key="t:2", name="other")
        idx = launcher.index_cards([old_open, new_open, done, other,
                                    ui_item()])
        self.assertEqual(set(idx), {"t:1", "t:2"})
        self.assertEqual(idx["t:1"][1].name, "new-open")
        self.assertIs(idx["t:1"][0], new_open)
        only_done = launcher.index_cards([done])
        self.assertEqual(only_done["t:1"][1].name, "finished")


if __name__ == "__main__":
    unittest.main()
