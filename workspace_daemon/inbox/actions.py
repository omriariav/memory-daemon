"""Action layer. The UI only talks to `Actions`.

Phase 1a: done, snooze and ack are local marks in the inbox store, open shows
an http(s) link in the browser, and nothing is ever sent. `LocalActions.send`
and `.nudge` refuse; they exist so the confirm/undo flow is complete and the
phase 2 `act` call has one place to plug in (see docs/inbox.md).

0.3.0 adds the first writes that leave the inbox: `rsvp` (a calendar answer
through the Claude bridge) and `save` (a configured command). Both run only
after the confirm modal and the undo window, in a worker thread.

0.4.0 adds two read-only calls: `fetch_context` (the configured context
command) and `summarize` (one no-tools bridge call, cached on disk). Neither
changes anything outside the inbox's own state files, so neither confirms.
"""
import time
import webbrowser
from dataclasses import dataclass, field

from . import context, invites, items, writes
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
                 complete=None):
        """`rsvp`, `save`, `context` and `summary` are the validated settings
        blocks (None: not configured). `call_tools` and `complete` replace
        the Claude bridge's two entry points (tests)."""
        self.store = store
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
    def context_configured(self):
        return self.context_cfg is not None

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

    def send(self, item, text):
        return Result(False, "sending is not available yet; nothing was sent")

    def nudge(self, item, text):
        return Result(False, "nudging is not available yet; nothing was sent")

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
        link = safe_link(item.link)
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
    _summaries: dict = field(default_factory=dict)

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
