"""Action layer. The UI only talks to `Actions`.

Phase 1a: done, snooze and ack are local marks in the inbox store, open shows
an http(s) link in the browser, and nothing is ever sent. `LocalActions.send`
and `.nudge` refuse; they exist so the confirm/undo flow is complete and the
phase 2 `act` call has one place to plug in (see docs/inbox.md).

0.3.0 adds the first writes that leave the inbox: `rsvp` (a calendar answer
through the Claude bridge) and `save` (a configured command). Both run only
after the confirm modal and the undo window, in a worker thread.
"""
import time
import webbrowser
from dataclasses import dataclass, field

from . import invites, items, writes
from .models import to_v1


@dataclass
class Result:
    ok: bool = True
    detail: str = ""
    # save: with ok False a likely duplicate (ask), with ok True "already in memory"
    duplicate: bool = False
    candidate: dict = None    # save: the memory it looks like


def safe_link(link):
    """The link if it is a plain http(s) URL, else None."""
    return link if items.valid_link(link) else None


class LocalActions:
    """Real backend for phase 1a: local marks only."""

    supports_send = False

    def __init__(self, store, opener=None, rsvp=None, save=None,
                 base_dir=None, call_tools=None):
        """`rsvp` and `save` are the validated settings blocks (None: not
        configured). `call_tools` replaces the Claude bridge (tests)."""
        self.store = store
        self._open = opener or webbrowser.open
        self.rsvp_cfg, self.save_cfg = rsvp, save
        self.base_dir, self._call_tools = base_dir, call_tools

    @property
    def rsvp_configured(self):
        return self.rsvp_cfg is not None and self.base_dir is not None

    @property
    def save_configured(self):
        return self.save_cfg is not None

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
