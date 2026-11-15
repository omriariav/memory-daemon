"""Action layer. The UI only talks to `Actions`.

Phase 1a: done, snooze and ack are local marks in the inbox store, open shows
an http(s) link in the browser, and nothing is ever sent. `LocalActions.send`
and `.nudge` refuse; they exist so the confirm/undo flow is complete and the
phase 2 `act` call has one place to plug in (see docs/inbox.md).
"""
import time
import webbrowser
from dataclasses import dataclass, field


@dataclass
class Result:
    ok: bool = True
    detail: str = ""


def safe_link(link):
    """The link if it is a plain http(s) URL, else None."""
    if isinstance(link, str) and link.startswith(("http://", "https://")) \
            and not any(c in link for c in " \n\r\t\x1b"):
        return link
    return None


class LocalActions:
    """Real backend for phase 1a: local marks only."""

    supports_send = False

    def __init__(self, store, opener=None):
        self.store = store
        self._open = opener or webbrowser.open

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
