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

0.10.0 adds acts (acts.py): after a local mark and the undo window the inbox
tells a command source that opted in with `acts:` (`LocalActions.send_act`),
keeps unsent ops in a small pending file and retries them with the same op_id,
and logs every op for the audit screen. `D` drafts a reply (`draft_for_me`) and
`done_reaction` adds an emoji reaction after `d` (`react_done`).

0.4.0 adds two read-only calls: `fetch_context` (the configured context
command) and `summarize` (one no-tools bridge call, cached on disk). Neither
changes anything outside the inbox's own state files, so neither confirms.
"""
import fnmatch
import subprocess
import sys
import threading
import time
import webbrowser
from collections import namedtuple
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import acts as acts_mod
from . import context, customactions, draftme, invites, items, mrkdwn, views
from . import polish as polish_mod
from . import sources, writes
from . import summary as summary_mod
from .models import to_v1


@dataclass
class Result:
    ok: bool = True
    detail: str = ""
    # save: with ok False a likely duplicate (ask), with ok True "already in memory"
    duplicate: bool = False
    candidate: dict = None    # save: the memory it looks like
    # 0.10.0 acts: how the source answered (`ok`, `shadow`, `conflict`,
    # `pending` = kept for a retry), the op id, and a second line for the
    # toast (a done reaction that failed, ops dropped from the pending file).
    sync: str = ""
    op_id: str = ""
    note: str = ""
    shadow: bool = False


# What `send_act` and the retry report: `status` is one of acts.OK, SHADOW,
# CONFLICT, PENDING (kept for a retry) or "unavailable" (not configured);
# `dropped` counts pending entries dropped for age or the cap.
ActOutcome = namedtuple("ActOutcome", "status message op_id dropped shadow",
                        defaults=("", "", 0, False))
RetryReport = namedtuple("RetryReport", "results dropped")


def _invite_event(item):
    """The event id a pending-invite row shows (kept in its done/ack mark), or
    None for any other row."""
    meta = getattr(item, "meta", None)
    event = meta.get("event_id") if isinstance(meta, dict) else None
    return event if (str(getattr(item, "key", "")).startswith("invites:")
                     and isinstance(event, str)) else None


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
                 polish=None, copy=False, team=None, slack_open=None,
                 acts=None, done_reaction=None, nudge_template=None,
                 open_with=None, runner=None):
        """`rsvp`, `save`, `context`, `summary`, `send`, `react` and `polish`
        are the validated settings blocks (None: not configured); `actions`
        the validated list. `call_tools` and `complete` replace the Claude
        bridge's two entry points (tests). `copy` (the top-level `copy:`
        setting) offers "Copy for Slack" without `send:`; `team` and
        `slack_open` build the URL `o` opens for a Slack item.

        `open_with` is the validated `[{host, app}]` list: an https link whose
        host matches a rule opens in that macOS app (`open -a`, run by
        `runner`, default `subprocess.run`), else in the default browser.

        `acts` maps a command source's name to `{"argv", "timeout_s", "acts"}`
        for each source that listed `acts:` (None or empty: acts are off and
        nothing here runs). `done_reaction` is an emoji name or None, and
        `nudge_template` the text `n` falls back to (see acts.py)."""
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
        self.open_with = list(open_with or [])
        self._run = runner or subprocess.run
        self.rsvp_cfg, self.save_cfg = rsvp, save
        self.context_cfg, self.summary_cfg = context, summary
        self.base_dir, self._call_tools = base_dir, call_tools
        self._complete = complete
        self._summary_cache = (summary_mod.SummaryCache(base_dir)
                               if summary is not None and base_dir is not None
                               else None)
        self.acts_cfg = dict(acts or {}) if base_dir is not None else {}
        self.done_reaction = done_reaction
        self.nudge_template = nudge_template or acts_mod.DEFAULT_NUDGE_TEMPLATE
        self._ops = (acts_mod.OpsLog(base_dir) if self.acts_cfg else None)
        self._pending_ops = (acts_mod.PendingOps(base_dir)
                             if self.acts_cfg else None)

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

    # ---- acts (0.10.0) ----
    @property
    def acts_configured(self):
        """At least one command source lists `acts:`."""
        return bool(self.acts_cfg)

    def act_names(self, item):
        """The acts the item's source listed (empty for a source without
        `acts:` and for any row the views made: digest and saved rows never
        act)."""
        if item is None or views.is_synthetic(item):
            return ()
        cfg = self.acts_cfg.get(getattr(item, "origin", ""))
        return tuple(cfg["acts"]) if cfg else ()

    def act_enabled(self, item, action):
        return action in self.act_names(item)

    def send_act(self, item, action, args=None):
        """Tell the item's source (blocks: call from a worker): one `act`
        call with a fresh op_id. A failure keeps the op in the pending file
        for a retry; a conflict is not retried. An `ActOutcome`."""
        if not self.act_enabled(item, action):
            return ActOutcome("unavailable", f"{action} is not enabled for "
                                             "this source")
        request = acts_mod.build_request(
            acts_mod.new_op_id(), action, item.key, item.meta, item.ts, args)
        return self._deliver(item.origin, request, item.who, item.where)

    def _log_op(self, source, request, who, where, status, message,
                inverse_of=None, retry=False):
        if self._ops is None:
            return
        row = {"at": time.time(), "op_id": request["op_id"],
               "action": request["action"], "id": request["id"],
               "who": str(who)[:acts_mod.WHO_MAX],
               "where": str(where)[:acts_mod.WHO_MAX], "source": source,
               "args": request.get("args") or {},
               "meta": acts_mod.small_meta(request.get("meta"),
                                           acts_mod.LOG_META_MAX_BYTES),
               "result": status,
               "message": str(message)[:200]}
        if inverse_of:
            row["inverse_of"] = inverse_of
        if retry:
            row["retry"] = True
        try:
            self._ops.append(row)
        except OSError:
            pass            # the log must never break an act

    def _call(self, source, request):
        cfg = self.acts_cfg[source]
        resp = sources.run_command_act(cfg["argv"], request, cfg["timeout_s"])
        if resp["conflict"]:
            status = acts_mod.CONFLICT
        elif resp["ok"]:
            status = acts_mod.SHADOW if resp["shadow"] else acts_mod.OK
        else:
            status = acts_mod.PENDING
        return status, resp

    def _deliver(self, source, request, who, where, inverse_of=None):
        """One act call. The op is written to the pending file BEFORE the call
        (so a crash or a kill during it leaves it for a retry with the same
        op_id) and removed once the source answers ok, shadow or conflict. An
        inverse is not retried, so it is not kept."""
        dropped, kept, now = 0, True, time.time()
        keep = inverse_of is None and self._pending_ops is not None
        if keep:
            entry = {"op_id": request["op_id"], "request": request,
                     "source": source, "action": request["action"],
                     "id": request["id"], "who": str(who)[:acts_mod.WHO_MAX],
                     "where": str(where)[:acts_mod.WHO_MAX],
                     "queued_at": now, "last_try": now, "tries": 1,
                     "message": "sent, no answer yet"}
            try:
                dropped = sum(self._pending_ops.add(entry, now))
            except OSError:
                kept = False
        status, resp = self._call(source, request)
        if keep and kept:
            try:
                if status == acts_mod.PENDING:
                    self._pending_ops.note(request["op_id"], resp["message"])
                else:
                    self._pending_ops.remove(request["op_id"])
            except OSError:
                pass
        lost = status == acts_mod.PENDING and not kept and inverse_of is None
        self._log_op(source, request, who, where,
                     "failed" if lost else status, resp["message"],
                     inverse_of)
        return ActOutcome(acts_mod.LOST if lost else status, resp["message"],
                          request["op_id"], dropped, resp["shadow"])

    def pending_count(self):
        """How many ops are waiting for a retry. Entries of a source that is
        no longer configured are not counted: nothing can retry them, so
        counting them would show an "N not synced" no action can clear. They
        stay in the file (a source put back in `inbox.yaml` finds them again)
        and expire after 7 days."""
        if self._pending_ops is None:
            return 0
        try:
            return sum(1 for e in self._pending_ops.all()
                       if e.get("source") in self.acts_cfg)
        except OSError:
            return 0

    def retry_pending(self, now=None, cancel=None):
        """Resend every pending op that was last tried at least
        acts.RETRY_EVERY_S ago, with the SAME op_id (the source is idempotent
        on it). Blocks: call from a worker. Entries older than 7 days are
        dropped first. A conflict drops the entry and puts the local mark
        back. A `RetryReport` of `(entry, ActOutcome)` pairs and the number of
        entries dropped for age."""
        if self._pending_ops is None:
            return RetryReport([], 0)
        now = time.time() if now is None else now
        try:
            dropped = self._pending_ops.prune(now)
            due = self._pending_ops.due(now)
        except OSError:
            return RetryReport([], 0)
        results = []
        for entry in due:
            if cancel is not None and cancel.is_set():
                break
            source, request = entry.get("source"), entry.get("request")
            if source not in self.acts_cfg or not isinstance(request, dict):
                continue
            try:     # stamp the try first: another pane skips it, a crash keeps it
                self._pending_ops.touch(entry["op_id"], now, "retrying")
            except OSError:
                pass
            status, resp = self._call(source, request)
            try:
                if status == acts_mod.PENDING:
                    self._pending_ops.note(entry["op_id"], resp["message"])
                else:
                    self._pending_ops.remove(entry["op_id"])
            except OSError:
                pass
            if status == acts_mod.CONFLICT and request["action"] in (
                    "close", "snooze", "mute"):
                try:    # a mark made after the op was queued is a newer decision
                    self.store.restore_unless_newer(
                        request["id"], entry.get("queued_at") or now)
                except OSError:
                    pass
            self._log_op(source, request, entry.get("who", ""),
                         entry.get("where", ""),
                         "pending" if status == acts_mod.PENDING else status,
                         resp["message"], retry=True)
            results.append((entry, ActOutcome(
                status, resp["message"], entry["op_id"], 0, resp["shadow"])))
        return RetryReport(results, dropped)

    def queue_act(self, item, action, args=None):
        """Keep an op for a later retry without calling the source (the app is
        quitting, so nothing may block). Returns True when it was stored."""
        if not self.act_enabled(item, action) or self._pending_ops is None:
            return False
        request = acts_mod.build_request(
            acts_mod.new_op_id(), action, item.key, item.meta, item.ts, args)
        now = time.time()
        entry = {"op_id": request["op_id"], "request": request,
                 "source": item.origin, "action": action, "id": item.key,
                 "who": str(item.who)[:acts_mod.WHO_MAX],
                 "where": str(item.where)[:acts_mod.WHO_MAX],
                 "queued_at": now, "last_try": 0, "tries": 0,
                 "message": "not sent: the inbox was closed"}
        try:
            self._pending_ops.add(entry, now)
        except OSError:
            return False
        self._log_op(item.origin, request, item.who, item.where, "pending",
                     entry["message"])
        return True

    def ops_rows(self):
        """The ops log, newest first (the audit screen)."""
        if self._ops is None:
            return []
        try:
            rows = self._ops.rows()
        except OSError:
            return []
        undone = {r.get("inverse_of") for r in rows if r.get("inverse_of")
                  and r.get("result") in (acts_mod.OK, acts_mod.SHADOW)}
        for r in rows:       # a row an inverse already undid offers no `u`
            if r.get("op_id") in undone:
                r["undone"] = True
        return rows

    def invertible(self, row):
        """The act that undoes a logged op, or None: only a `close` or a `mute`
        that reached the source (ok or shadow) has one, and only while its
        source still lists the act."""
        action = acts_mod.INVERSE.get(row.get("action"))
        cfg = self.acts_cfg.get(row.get("source"))
        if (action is None or cfg is None or row.get("inverse_of")
                or row.get("undone")
                or row.get("result") not in (acts_mod.OK, acts_mod.SHADOW)
                or row.get("action") not in cfg["acts"]):
            return None
        return action

    def send_inverse(self, row):
        """Send the inverse of a logged op (blocks: call from a worker) and,
        when the source took it, drop the local mark. An `ActOutcome`."""
        action = self.invertible(row)
        if action is None or row.get("op_id") in self._undone_ids():
            return ActOutcome("unavailable", "this op has no inverse (or it "
                                             "was already undone)")
        request = acts_mod.build_request(
            acts_mod.new_op_id(), action, row["id"], row.get("meta") or {},
            time.time(), {})
        out = self._deliver(row["source"], request, row.get("who", ""),
                            row.get("where", ""), inverse_of=row["op_id"])
        if out.status in (acts_mod.OK, acts_mod.SHADOW):
            try:
                self.store.restore(row["id"])
                if self._pending_ops is not None:   # a pending op is stale now
                    self._pending_ops.drop_for_id(row["id"])
            except OSError:
                pass
        return out

    def _undone_ids(self):
        """The op ids that a later inverse (ok or shadow) already undid."""
        return {r.get("inverse_of") for r in self.ops_rows()
                if r.get("inverse_of") and r.get("result") in (
                    acts_mod.OK, acts_mod.SHADOW)}

    def last_act_shadow(self):
        """Whether the newest op the source took (ok or shadow) was a shadow
        one: rebuilds the health strip's marker at startup."""
        for row in self.ops_rows():
            if row.get("result") in (acts_mod.OK, acts_mod.SHADOW):
                return row["result"] == acts_mod.SHADOW
        return False

    def undo_marks(self, item, since=None):
        """Take the local marks of an item back (a conflict). With `since`,
        only when they were made at or before it: a newer mark stays."""
        if since is None:
            self.store.restore(item.key)
        else:
            self.store.restore_unless_newer(item.key, since)

    def act_nudge_text(self, item):
        """The `nudge_template` text for an item (see acts.render_nudge)."""
        return acts_mod.render_nudge(self.nudge_template, item.who, item.gist)

    # ---- done reaction (0.10.0) ----
    def done_reaction_for(self, item):
        """The emoji `d` also reacts with, or None: `done_reaction` is set,
        `react:` is configured and the item has its own message to react to
        (`reply_to.message_ts`: never a thread's root post, which is somebody
        else's)."""
        if (not self.done_reaction or not self.react_configured
                or item is None or views.is_synthetic(item)
                or writes.message_target(item.meta) is None):
            return None
        return self.done_reaction

    def react_done(self, item, emoji):
        """The done reaction: the existing react path (one pinned call, never
        retried) with the configured emoji. Blocks: call from a worker."""
        if not self.react_configured:
            return Result(False, "React not configured")
        target = writes.message_target(item.meta)
        if target is None:
            return Result(False, "this item has no message of its own to "
                                 "react to (reply_to.message_ts)")
        if not writes.valid_emoji(emoji):
            return Result(False, "not a valid emoji name")
        out = writes.run_react(self.react_cfg, self.base_dir, target[0],
                               target[1], emoji, call_tools=self._call_tools)
        if not out.ok:
            return Result(False, out.detail)
        return Result(True, out.detail or f"Reacted :{emoji}:")

    # ---- D: a draft for me (0.10.0) ----
    @property
    def draft_me_configured(self):
        """`D` needs the model settings of `summary:` or `polish:`."""
        return ((self.summary_cfg is not None or self.polish_cfg is not None)
                and self.base_dir is not None)

    def _draft_me_cfg(self):
        return self.summary_cfg if self.summary_cfg is not None \
            else self.polish_cfg

    def draft_me_key(self, item):
        return draftme.cache_key(to_v1(item))

    def cached_draft_me(self, item):
        """The draft `D` made for this item's current thread, or None."""
        fn = getattr(self.store, "get_draft", None)
        return fn(item.key, self.draft_me_key(item)) if fn else None

    def draft_for_me(self, item, force=False):
        """A draft reply for `item`: from the store unless `force`, else one
        no-tools bridge call (then kept in the store). Never sends. Blocks:
        call from a worker. A `draftme.Outcome`."""
        if not self.draft_me_configured:
            return draftme.Outcome(False, "", "Draft for me needs summary: "
                                              "or polish: in inbox.yaml")
        if not force:
            hit = self.cached_draft_me(item)
            if hit:
                return draftme.Outcome(True, hit, "", True)
        out = draftme.run_draft(self._draft_me_cfg(), self.base_dir,
                                to_v1(item), complete=self._complete)
        if not out.ok:
            return out
        try:
            self.store.set_draft(item.key, out.text, self.draft_me_key(item))
        except (OSError, ValueError, AttributeError):
            return out._replace(detail="not kept: cannot write the state")
        return out

    def mark_done(self, item):
        self.store.done(item.key, _invite_event(item))
        return Result()

    def ack(self, item):
        self.store.ack(item.key, _invite_event(item))
        return Result()

    def snooze(self, item, until):
        self.store.snooze(item.key, until)
        return Result()

    def restore(self, item):
        self.store.restore(item.key)

    def open_app_for(self, link):
        """The app name of the first `open_with` rule that matches the host
        of an https `link`, else None (any other scheme never matches)."""
        if not self.open_with:
            return None
        try:
            parts = urlsplit(link)
            host = (parts.hostname or "").lower()
        except ValueError:
            return None
        if parts.scheme != "https" or not host:
            return None
        for rule in self.open_with:
            if fnmatch.fnmatchcase(host, rule["host"].lower()):
                return rule["app"]
        return None

    def _open_in_app(self, app, link):
        """`(opened, reason)`: run `open -a <app> <link>` without a shell.
        `reason` says why it did not open (macOS only)."""
        if sys.platform != "darwin":
            return False, "only on macOS"
        try:
            done = self._run(["open", "-a", app, link], timeout=5,
                             check=False, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, "timed out"
        except OSError as exc:
            return False, f"cannot run open: {exc.__class__.__name__}"
        code = getattr(done, "returncode", 1)
        return (True, "") if code == 0 else (False, f"exit {code}")

    def open_link(self, item):
        link = self.open_target(item)
        if link is None:
            return Result(False, "this item has no link")
        app = self.open_app_for(link)
        note = ""
        if app:
            opened, why = self._open_in_app(app, link)
            if opened:
                return Result(True, f"opened in {app}")
            note = f" ({app}: {why})"
        try:
            ok = self._open(link)
        except Exception as exc:  # webbrowser can raise on odd platforms
            return Result(False, f"cannot open: {exc}")
        if app and ok:
            return Result(True, f"opened in browser{note}")
        return Result(bool(ok), link if ok else f"no browser available{note}")


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
            self.store.done(item.key, _invite_event(item))
        return Result()

    def ack(self, item):
        self.calls.append(("ack", item.key))
        if self.store is not None:
            self.store.ack(item.key, _invite_event(item))
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
