"""Action layer. The UI only talks to `Actions`.

Phase 1a: done, snooze and ack are local marks in the inbox store, open shows
an http(s) link in the browser, and nothing is ever sent. `LocalActions.send`
and `.nudge` refuse; they exist so the confirm/undo flow is complete and the
phase 2 `act` call has one place to plug in (see docs/inbox.md).

0.3.0 adds the first writes that leave the inbox: `rsvp` (a calendar answer
through the Claude bridge) and `save` (a configured command). Both run only
after the confirm modal and the undo window, in a worker thread.

0.5.0 adds the generic `actions:` hook (`action_choices`, `action_prepare`,
`run_action`; see customactions.py) and lets a successful save or action run
mark the item done (`save_mark_done`, `action_mark_done`).

0.6.0 adds three opt-in writes (`send:`, `react:`, `polish:`; see writes.py and
polish.py). `send` posts the draft to Slack for a reply or a nudge; `react`
adds an emoji reaction; `polish` rewrites the draft in a configured tone and
never sends. Without `send:` a reply or nudge still only works in `--demo`
(`LocalActions.supports_send` is False and `FakeActions` pretends).

0.7.0 turns "send" into a choice (some workplaces block headless Slack sends):
`reply_options` lists what `a` / `n` may do (`draft`: place the text in the
Slack draft box, `send`, `copy`: a local clipboard copy as Slack mrkdwn),
gated by `send.mode`; `place_draft` is the pinned draft call (never marks the
item done); `copy_text` is the pure conversion; `open_target` is the URL `o`
opens (`slack_open.url_template`, else the item's link).

0.4.0 adds two read-only calls: `fetch_context` (the configured context
command) and `summarize` (one no-tools bridge call, cached on disk). Neither
changes anything outside the inbox's own state files, so neither confirms.
"""
import threading
import time
import webbrowser
from dataclasses import dataclass, field

from . import context, customactions, invites, items, mrkdwn, writes
from . import polish as polish_mod
from . import summary as summary_mod
from .models import to_v1


@dataclass
class Result:
    ok: bool = True
    detail: str = ""
    # save: with ok False a likely duplicate (ask), with ok True "already in memory"
    duplicate: bool = False
    candidate: dict = None    # save: the memory it looks like


def _cached(row):
    if row is None:
        return None
    return summary_mod.Outcome(True, row["text"], "", True, row["at"],
                               row["ctx"])


def safe_link(link):
    """The link if it is a plain http(s) URL, else None."""
    return link if items.valid_link(link) else None


class LocalActions:
    """Real backend for phase 1a: local marks only."""

    supports_send = False

    def __init__(self, store, opener=None, rsvp=None, save=None,
                 base_dir=None, call_tools=None, context=None, summary=None,
                 complete=None, actions=None, send=None, react=None,
                 polish=None, copy=False, team=None, slack_open=None):
        """`rsvp`, `save`, `context`, `summary`, `send`, `react` and `polish`
        are the validated settings blocks (None: not configured); `actions`
        the validated list. `call_tools` and `complete` replace the Claude
        bridge's two entry points (tests). `copy` (the top-level `copy:`
        setting) offers "Copy for Slack" without `send:`; `team` and
        `slack_open` build the URL `o` opens for a Slack item."""
        self.store = store
        self.send_cfg, self.react_cfg, self.polish_cfg = send, react, polish
        self.copy_cfg, self.team = bool(copy), team
        self.slack_open_cfg = slack_open
        # Replies and nudges reach the confirm flow only with `send:` or `copy:`.
        self.supports_send = ((send is not None or self.copy_cfg)
                              and base_dir is not None)
        self._send_lock = threading.Lock()   # one send runs at a time
        self.custom_actions = list(actions or [])
        self._open = opener or webbrowser.open
        self.rsvp_cfg, self.save_cfg = rsvp, save
        self.context_cfg, self.summary_cfg = context, summary
        self.base_dir, self._call_tools = base_dir, call_tools
        self._complete = complete
        self._summary_cache = (summary_mod.SummaryCache(base_dir)
                               if summary is not None and base_dir is not None
                               else None)

    @property
    def rsvp_configured(self):
        return self.rsvp_cfg is not None and self.base_dir is not None

    @property
    def save_configured(self):
        return self.save_cfg is not None

    @property
    def save_mark_done(self):
        """A successful save marks the item done (`save: {mark_done}`)."""
        return bool(self.save_cfg is None or self.save_cfg.get("mark_done", True))

    @property
    def context_configured(self):
        return self.context_cfg is not None

    def _action(self, name):
        return next((a for a in self.custom_actions if a["label"] == name),
                    None)

    def action_mark_done(self, name):
        """Whether a successful run of the action marks the item done."""
        cfg = self._action(name)
        return bool(cfg is None or cfg.get("mark_done", True))

    def action_choices(self, name, item, cancel=None):
        """The choices of an action for `item` (read-only). Blocks: call from
        a worker. A `customactions.Choices`."""
        cfg = self._action(name)
        if cfg is None:
            return customactions.Choices(False, [], "Unknown action")
        return customactions.run_choices(cfg, to_v1(item), cancel=cancel)

    def action_prepare(self, name, item, choice, cancel=None):
        """The confirmation rows for one choice (read-only; may take a while
        as the command reads fresh data). A `customactions.Prepared`."""
        cfg = self._action(name)
        if cfg is None:
            return customactions.Prepared(False, [], "", "Unknown action",
                                          False)
        return customactions.run_prepare(cfg, to_v1(item), choice,
                                         cancel=cancel)

    def run_action(self, name, item, choice, state):
        """Do the action once (never retried). A `Result`."""
        cfg = self._action(name)
        if cfg is None:
            return Result(False, "Unknown action")
        out = customactions.run_run(cfg, to_v1(item), choice, state)
        return Result(out.ok, out.message)

    @property
    def summary_configured(self):
        return self.summary_cfg is not None and self.base_dir is not None

    def fetch_context(self, item, cancel=None):
        """Run the context command for `item`: a `context.Outcome`. Blocks:
        call from a worker. Read-only. `cancel` (a `threading.Event`) stops
        the command (SIGTERM, then SIGKILL after the grace period)."""
        if not self.context_configured:
            return context.Outcome(False, [], "Context not configured")
        return context.run_context(self.context_cfg, to_v1(item), cancel=cancel)

    def summary_key(self, item):
        """The cache key of `item`'s summary (item, time, thread and model)."""
        return summary_mod.cache_key(to_v1(item), self.summary_cfg["model"])

    def cached_summary(self, item, touch=False):
        """The cached summary of `item` as an `Outcome`, or None. By default
        the entry is not marked as used (`touch=True` does)."""
        if not self.summary_configured:
            return None
        key = self.summary_key(item)
        row = (self._summary_cache.get(key) if touch
               else self._summary_cache.peek(key))
        return _cached(row)

    def summarize(self, item, sections=(), force=False):
        """The summary of `item`: from the disk cache unless `force`, else one
        no-tools bridge call (then cached). Blocks: call from a worker."""
        if not self.summary_configured:
            return summary_mod.Outcome(False, "", "Summary not configured")
        v1 = to_v1(item)
        key = summary_mod.cache_key(v1, self.summary_cfg["model"])
        if not force:
            hit = _cached(self._summary_cache.get(key))
            if hit is not None:
                return hit
        out = summary_mod.run_summary(self.summary_cfg, self.base_dir, v1,
                                      sections, complete=self._complete)
        if not out.ok:
            return out
        try:
            self._summary_cache.put(key, out.text, out.with_context,
                                    self.summary_cfg["model"], now=out.at)
        except OSError:
            return out._replace(detail="not cached: cannot write the cache")
        return out

    def rsvp(self, item, answer):
        """Answer a pending invite (`accepted`, `declined`, `tentative`)
        through the bridge, then hide the row. Blocks: call from a worker."""
        if not self.rsvp_configured:
            return Result(False, "RSVP not configured")
        target = invites.rsvp_target(item)
        if target is None:
            return Result(False, "not a calendar invite with usable ids")
        event_id, calendar_id, _recurring = target
        out = writes.run_rsvp(self.rsvp_cfg, self.base_dir, event_id,
                              calendar_id, answer,
                              call_tools=self._call_tools)
        if not out.ok:
            return Result(False, out.detail)
        try:
            self.store.rsvp(item.key, answer, event_id)
        except OSError as exc:
            return Result(True, f"{out.detail}; local mark failed: "
                                f"{exc.strerror or exc}", duplicate=out.duplicate)
        return Result(True, out.detail, duplicate=out.duplicate)

    def save(self, item, force=False):
        """Save the item to memory through the configured command. The item
        stays listed; a local mark gives it the saved glyph. `force` resends
        after the command reported a likely duplicate."""
        if not self.save_configured:
            return Result(False, "Save not configured")
        out = writes.run_save(self.save_cfg, to_v1(item), force=force)
        if not out.ok:
            return Result(False, out.detail, duplicate=out.duplicate,
                          candidate=out.candidate)
        try:
            self.store.mark_saved(item.key)
        except OSError as exc:
            return Result(True, f"{out.detail}; local mark failed: "
                                f"{exc.strerror or exc}", duplicate=out.duplicate)
        return Result(True, out.detail, duplicate=out.duplicate)

    def draft(self, item):
        return item.draft

    # ---- send, react and polish (0.6.0) ----
    @property
    def send_configured(self):
        return self.send_cfg is not None and self.base_dir is not None

    @property
    def reply_configured(self):
        """`a` / `n` open the real confirm flow: `send:` or `copy:` is set."""
        return bool((self.send_cfg is not None or self.copy_cfg)
                    and self.base_dir is not None)

    @property
    def send_mode(self):
        """`send.mode` (send, draft or both), or None without `send:`."""
        return self.send_cfg.get("mode", "send") if self.send_cfg else None

    @property
    def copy_enabled(self):
        """"Copy for Slack" is on: `send:` or `copy: true` is configured."""
        return self.send_cfg is not None or self.copy_cfg

    def reply_options(self):
        """What `a` / `n` may do after the confirm screen, in order, from
        `draft` (mode draft or both), `send` (mode send or both) and `copy`
        (always, once `send:` or `copy:` is set)."""
        opts = []
        cfg = self.send_cfg
        if cfg is not None:
            if cfg.get("mode", "send") in ("draft", "both") and cfg.get(
                    "draft_tool"):
                opts.append("draft")
            if cfg.get("mode", "send") in ("send", "both") and cfg.get("tool"):
                opts.append("send")
        if self.copy_enabled:
            opts.append("copy")
        return tuple(opts)

    def open_target(self, item):
        """The URL `o` opens for `item`: the `slack_open.url_template` URL
        when it is set and the item has a usable `meta.reply_to`, else the
        item's own http(s) link, else None."""
        if self.slack_open_cfg:
            url = writes.slack_open_url(self.slack_open_cfg["url_template"],
                                        self.team, item.meta)
            if url:
                return url
        return safe_link(item.link)

    def copy_text(self, item, text):
        """`(mrkdwn, error)`: `text` (the checked draft) as Slack mrkdwn for
        the clipboard. Purely local: no tool call, nothing written."""
        if "copy" not in self.reply_options():
            return "", "Copy is not configured"
        text, error = writes.send_text(text, "copy")
        if error:
            return "", error[0].upper() + error[1:]
        out = mrkdwn.to_slack_mrkdwn(text)
        if not out.strip():
            return "", "there is no text to copy"
        return out, ""

    @property
    def react_configured(self):
        return self.react_cfg is not None and self.base_dir is not None

    @property
    def polish_configured(self):
        return self.polish_cfg is not None and self.base_dir is not None

    @property
    def react_mark_done(self):
        """A successful reaction marks the item done (`react: {mark_done}`)."""
        return bool(self.react_cfg is None
                    or self.react_cfg.get("mark_done", True))

    @property
    def react_emojis(self):
        return list(self.react_cfg["emojis"]) if self.react_cfg else []

    @property
    def precheck_configured(self):
        return bool(self.send_configured and self.send_cfg.get("precheck"))

    def send_precheck(self, item, text, cancel=None):
        """Run the optional `send.precheck` command (read-only advice): a
        `writes.Precheck`. Blocks: call from a worker. Item text goes on
        stdin only."""
        if not self.precheck_configured:
            return writes.Precheck()
        return writes.run_precheck(self.send_cfg, to_v1(item), text,
                                   cancel=cancel)

    def send_plan(self, item, text):
        """What a send of `text` to `item` would do, checked (read-only): a
        `writes.Sendable` with the exact text, the destination label and
        whether the text was cut, or `ok` False with the reason."""
        plan = writes.send_plan(item.meta, text, self._allow_channel_posts())
        if not plan.ok:
            if plan.dest_refused and self.copy_enabled:
                # The destination is unusable but the text may still be
                # copied: offer only Copy for Slack, with the reason.
                clean, error = writes.send_text(text, "copy")
                if error:
                    return writes.Sendable(False, error=error[0].upper()
                                           + error[1:])
                why = ("no valid meta.reply_to" if writes.reply_to(item.meta)
                       is None else "a channel post without a thread is off "
                       "(send.allow_channel_posts)")
                return writes.Sendable(
                    True, clean, None, "",
                    f"Draft and Send need a valid thread/DM destination "
                    f"({why})", item.where, "", copy_only=True)
            return plan
        return plan._replace(destination=f"{item.where} ({plan.kind})")

    def _allow_channel_posts(self):
        return bool(self.send_cfg and self.send_cfg.get("allow_channel_posts"))

    def _send(self, item, text, what):
        if not self.supports_send or self.send_cfg is None:
            return Result(False, f"{what} not configured")
        if "send" not in self.reply_options():
            return Result(False, f"{what} is off (send.mode is "
                                 f"{self.send_mode}); not sent")
        plan = writes.send_plan(item.meta, text, self._allow_channel_posts())
        if not plan.ok:
            return Result(False, plan.error)
        if not self._send_lock.acquire(blocking=False):
            return Result(False, "another send is still running; not sent")
        try:
            out = writes.run_send(self.send_cfg, self.base_dir, plan.target,
                                  plan.text, call_tools=self._call_tools)
        finally:
            self._send_lock.release()
        if not out.ok:
            return Result(False, out.detail)
        detail = out.detail or "Sent"
        try:
            self.store.done(item.key)
        except OSError as exc:
            return Result(True, f"{detail}; local mark failed: "
                                f"{exc.strerror or exc}")
        return Result(True, detail)

    def send(self, item, text):
        """Post the reply to Slack through the configured connector tool,
        then mark the item done. Blocks: call from a worker."""
        return self._send(item, text, "Send")

    def nudge(self, item, text):
        """A nudge is the same write with the nudge text."""
        return self._send(item, text, "Nudge")

    def place_draft(self, item, text):
        """Put the reply in the Slack draft box of the item's conversation
        through `send.draft_tool` (pinned, one call, never retried). Nothing
        is posted and the item is NOT marked done. Blocks: call from a
        worker. A `Result`."""
        if (not self.supports_send or self.send_cfg is None
                or "draft" not in self.reply_options()):
            return Result(False, "Draft in Slack is not configured "
                                 "(send.mode draft or both, send.draft_tool)")
        plan = writes.send_plan(item.meta, text, self._allow_channel_posts(),
                                what="draft")
        if not plan.ok:
            return Result(False, plan.error)
        if not self._send_lock.acquire(blocking=False):
            return Result(False, "another Slack write is still running; no "
                                 "draft placed")
        try:
            out = writes.run_draft(self.send_cfg, self.base_dir, plan.target,
                                   plan.text, call_tools=self._call_tools)
        finally:
            self._send_lock.release()
        return Result(out.ok, out.detail or ("Draft placed" if out.ok else ""))

    def react_plan(self, item):
        """`(channel, timestamp)` a reaction to `item` would go to, or None."""
        return writes.react_target(item.meta)

    def react_choices(self, item):
        """`(emojis, preselected)` for the picker: the configured names, with
        the item's valid `meta.suggested_reaction` preselected when it is one
        of them. A suggestion outside the list is ignored, unless
        `react.allow_suggested` is true (then it is added in front and
        allowed for that item)."""
        emojis = list(self.react_emojis)
        suggested = writes.suggested_reaction(item.meta)
        if suggested is not None and suggested not in emojis:
            if self.react_cfg and self.react_cfg.get("allow_suggested"):
                emojis.insert(0, suggested)
            else:
                suggested = None
        return emojis, suggested

    def react(self, item, emoji):
        """Add the reaction `emoji` (one of `react.emojis`) to the item's
        message. Blocks: call from a worker. A `Result`."""
        if not self.react_configured:
            return Result(False, "React not configured")
        target = writes.react_target(item.meta)
        if target is None:
            return Result(False, "this item has no message to react to "
                                 "(meta.reply_to.message_ts or thread_ts)")
        allowed = self.react_choices(item)[0]
        if emoji not in allowed or not writes.valid_emoji(emoji):
            return Result(False, "not one of the configured emojis")
        out = writes.run_react(self.react_cfg, self.base_dir, target[0],
                               target[1], emoji, call_tools=self._call_tools)
        if not out.ok:
            return Result(False, out.detail)
        return Result(True, out.detail or f"Reacted :{emoji}:")

    def polish(self, item, draft):
        """Rewrite `draft` in the configured tone: a `polish.Outcome`. Never
        sends. Blocks: call from a worker."""
        if not self.polish_configured:
            return polish_mod.Outcome(False, "", "Polish not configured")
        return polish_mod.run_polish(
            self.polish_cfg, self.base_dir, to_v1(item), draft,
            call_tools=self._call_tools, complete=self._complete)

    def mark_done(self, item):
        self.store.done(item.key)
        return Result()

    def ack(self, item):
        self.store.ack(item.key)
        return Result()

    def snooze(self, item, until):
        self.store.snooze(item.key, until)
        return Result()

    def restore(self, item):
        self.store.restore(item.key)

    def open_link(self, item):
        link = self.open_target(item)
        if link is None:
            return Result(False, "this item has no link")
        try:
            ok = self._open(link)
        except Exception as exc:  # webbrowser can raise on odd platforms
            return Result(False, f"cannot open: {exc}")
        return Result(bool(ok), link if ok else "no browser available")


@dataclass
class FakeActions:
    """Demo backend: keeps marks in memory, records calls, sends nothing."""

    store: object = None
    draft_delay: float = 0.6
    calls: list = field(default_factory=list)
    supports_send = True
    rsvp_configured = True
    save_configured = True
    context_configured = True
    summary_configured = True
    context_delay: float = 0.3
    summary_delay: float = 0.6
    action_delay: float = 0.3
    save_mark_done = True
    custom_actions: list = field(default_factory=lambda: [
        {"key": "p", "label": "Add to agenda", "argv": ["demo"],
         "timeout_s": 60.0, "mark_done": True}])
    _summaries: dict = field(default_factory=dict)

    def action_mark_done(self, name):
        return next((a.get("mark_done", True) for a in self.custom_actions
                     if a["label"] == name), True)

    def action_choices(self, name, item, cancel=None):
        time.sleep(self.action_delay)
        self.calls.append(("choices", name, item.key))
        return customactions.Choices(True, [
            {"id": "demo-1", "label": "Example Person",
             "detail": "invented demo agenda"},
            {"id": "demo-2", "label": "Another Person", "detail": ""}], None)

    def action_prepare(self, name, item, choice, cancel=None):
        time.sleep(self.action_delay)
        self.calls.append(("prepare", name, item.key, choice))
        return customactions.Prepared(True, [
            ("Agenda", "Example Person"), ("Add", item.gist)],
            "demo-state", None, False)

    def run_action(self, name, item, choice, state):
        self.calls.append(("run", name, item.key, choice, state))
        return Result(True, "would add to the agenda")

    def summary_key(self, item):
        return item.key

    def fetch_context(self, item, cancel=None):
        time.sleep(self.context_delay)
        self.calls.append(("context", item.key))
        return context.Outcome(True, [
            {"title": "Person", "lines": [
                f"{item.who}: invented demo profile",
                "Team: Example Platform", "Timezone: UTC+2"]},
            {"title": "Related", "lines": [
                "Ticket EX-101 (open): invented example",
                "Doc: https://example.com/demo-doc"]}], None)

    def cached_summary(self, item, touch=False):
        row = self._summaries.get(item.key)
        return summary_mod.Outcome(True, row, "", True, time.time() - 120,
                                   False) if row else None

    def summarize(self, item, sections=(), force=False):
        if not force and item.key in self._summaries:
            return self.cached_summary(item)
        time.sleep(self.summary_delay)
        self.calls.append(("summary", item.key, "force") if force
                          else ("summary", item.key))
        text = ("- Invented demo summary: no model was called.\n"
                f"- Asked by {item.who}: {item.gist}\n"
                "- Next step: reply or mark it done.\n"
                "- Deadline: none stated.")
        self._summaries[item.key] = text
        return summary_mod.Outcome(True, text, "", False, time.time(),
                                   bool(sections))

    def rsvp(self, item, answer):
        self.calls.append(("rsvp", item.key, answer))
        if self.store is not None:
            self.store.rsvp(item.key, answer, item.meta.get("event_id"))
        return Result(True, f"would answer {answer}")

    def save(self, item, force=False):
        self.calls.append(("save", item.key, "force") if force
                          else ("save", item.key))
        if self.store is not None:
            self.store.mark_saved(item.key)
        return Result(True, "would save to memory")

    def draft(self, item):
        time.sleep(self.draft_delay)  # stands in for a model call (worker thread)
        self.calls.append(("draft", item.key))
        return item.draft

    def send(self, item, text):
        self.calls.append(("send", item.key, text))
        return Result(True, "would send")

    def nudge(self, item, text):
        self.calls.append(("nudge", item.key, text))
        return Result(True, "would send nudge")

    def mark_done(self, item):
        self.calls.append(("mark_done", item.key))
        if self.store is not None:
            self.store.done(item.key)
        return Result()

    def ack(self, item):
        self.calls.append(("ack", item.key))
        if self.store is not None:
            self.store.ack(item.key)
        return Result()

    def snooze(self, item, until):
        self.calls.append(("snooze", item.key, until))
        if self.store is not None:
            self.store.snooze(item.key, until)
        return Result()

    def restore(self, item):
        if self.store is not None:
            self.store.restore(item.key)

    def open_link(self, item):
        self.calls.append(("open", item.key))
        return Result(True, f"would open {item.link or '(no link)'}")

    def sent(self):
        return [c for c in self.calls if c[0] in ("send", "nudge")]
