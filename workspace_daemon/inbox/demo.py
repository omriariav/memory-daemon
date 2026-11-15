"""Invented demo data for `inbox --demo`. Every name, channel and message
here is made up; nothing is read from or sent anywhere."""
import time

from . import items as itm
from .sources import SourceResult

M = 60.0
H = 3600.0

REPLY = ["reply", "done", "snooze", "open"]
NUDGE = ["nudge", "done", "snooze", "open"]
FYI = ["done", "snooze", "open"]
ACK = ["ack", "done", "snooze", "open"]


def _thread(now, spec):
    """spec: [(who, minutes_ago, text)]; who == "me" is the owner."""
    return [{"who": "You" if w == "me" else w, "ts": now - m * M, "text": t}
            for w, m, t in spec]


def demo_items(now=None):
    """The demo inbox as validated v1 item dicts."""
    now = now or time.time()
    out = []

    def add(key, section, sender, where, ago_min, gist, spec, draft=None,
            actions=REPLY, source="slack"):
        out.append(itm.make_item(
            id=f"demo:{key}", source=source, section=section,
            urgency="urgent" if section == "urgent" else "digest",
            sender=sender, where=where, gist=gist, ts=now - ago_min * M,
            link=f"https://example.invalid/item/{key}",
            thread=_thread(now, spec), draft=draft, actions=actions,
            meta={"demo": True}))

    add("c01", "urgent", "Dana Levi", "#launch-sync", 8,
        "Rollback decision needed before 15:00",
        [("Dana Levi", 41, "*Heads up*: error rate on the checkout service is up since the 13:10 deploy."),
         ("Ravi Menon", 33, "Seeing it too. Logs point at `retry_budget` being exhausted, see <https://example.invalid/runbook|the runbook>."),
         ("Dana Levi", 9, "<@U_ME|you> we need a rollback call before 15:00. Roll back, or hold and patch forward?"),
         ("Dana Levi", 8, "Customer success is asking for an ETA.")],
        "Roll back now. Please keep the patch on a branch and we re-ship after the 16:00 check-in. I'll tell CS the ETA is 17:00.")
    add("d02", "urgent", "Noam Barak", "DM", 22,
        "אפשר לאשר את התקציב עד סוף היום?",
        [("Noam Barak", 25, "היי, אפשר לאשר את התקציב עד סוף היום? הלקוח מחכה לתשובה."),
         ("Noam Barak", 22, "צריך רק אישור אחד, ה-PO כבר מוכן.")],
        "היי נועם, מאשר. תשלח לי את ה-PO ואני סוגר היום.")
    add("c03", "needs_reply", "Priya Nair", "#design-review", 47,
        "Can you review the onboarding flow?",
        [("Priya Nair", 47, "Could you review the new onboarding flow (`onboarding_v3`)? Figma: <https://example.invalid/fig/abc|onboarding v3>. cc <@U_TAL|Tal Green>")],
        "Will do. I'll leave comments in the file by tomorrow noon.")
    add("d04", "needs_reply", "Tomer Shani", "DM", 95,
        "מתי נוח לך לדבר על ה-roadmap?",
        [("Tomer Shani", 95, "מתי נוח לך לדבר על התוכנית לרבעון הבא? צריך לסגור את ה-roadmap לפני יום חמישי.")],
        "מה דעתך על מחר ב-10:30? אשלח הזמנה.")
    add("g05", "needs_reply", "Elena Petrova", "group DM", 130,
        "Handoff for the on-call rotation",
        [("Elena Petrova", 140, "Who covers on-call next week? I'm out Mon-Wed."),
         ("Sam Okafor", 133, "I can take Tue."),
         ("Elena Petrova", 130, "Great. <@U_ME|you> can you take Mon and Wed?")],
        "Yes, I'll take Mon and Wed. Please update the rota doc.")
    add("c06", "needs_reply", "Marcus Webb", "#infra-ask", 210,
        "Approve staging quota bump",
        [("Marcus Webb", 210, "Need approval to raise the staging quota:\n```\nquota.cpu: 64 -> 96\nquota.mem: 256Gi -> 384Gi\n```\n> only for the load test week")],
        "Approved for the load-test week; please revert on Friday.")
    add("d07", "waiting", "Sam Okafor", "DM", 26 * 60,
        "You asked for the final numbers (nudge due)",
        [("me", 26 * 60, "Can you send the final Q4 numbers when you have them?")],
        "Hi Sam, a gentle nudge on the Q4 numbers. Could you send them today?",
        actions=NUDGE)
    add("c08", "waiting", "Yael Mizrahi", "#launch-sync", 29 * 60,
        "ביקשת ממנה את המסמך (nudge due)",
        [("me", 29 * 60, "@Yael אפשר לקבל את מסמך ההשקה המעודכן?")],
        "היי יעל, תזכורת קטנה על מסמך ההשקה. אפשר לשתף היום?", actions=NUDGE)
    add("c09", "waiting", "Lena Fischer", "#q4-planning", 6 * 60,
        "Waiting on the capacity estimate",
        [("me", 6 * 60, "Lena, what is the capacity estimate for the December push?")],
        None, actions=NUDGE)
    add("c10", "fyi", "Alex Kim", "#platform-releases", 55,
        "Release 4.12 is out: faster cold starts",
        [("Alex Kim", 55, "*Release 4.12* is live. Highlights: faster cold starts, new `--dry-run` flag.")],
        actions=FYI)
    add("drive1", "fyi", "Jo Park", "Drive", 180,
        "shared Roadmap draft v2",
        [("Jo Park", 180, "Jo Park shared <https://example.invalid/doc/roadmap|Roadmap draft v2> with you.")],
        actions=FYI, source="drive")
    add("c11", "fyi", "Mia Costa", "#announcements", 320,
        "Office closed Friday afternoon",
        [("Mia Costa", 320, "The office closes at 13:00 on Friday for maintenance.")],
        actions=FYI)
    add("mail1", "needs_reply", "Naomi Katz", "Vendor contract renewal", 75,
        "Please confirm the renewal terms by Friday",
        [("Naomi Katz", 75, "Subject: Vendor contract renewal\nHi, can you confirm the renewal terms (12 months, same rate) by Friday? Contract: renewal.pdf")],
        "Hi Naomi, confirmed: 12 months at the same rate. Please send the final copy for signature.",
        source="email")
    add("alert1", "urgent", "daemon", "alerts", 14,
        "Poll lag: last successful sync 14 min ago",
        [("daemon", 14, "Poll lag detected: last successful sync was 14 minutes ago. Check sync.log for retries.")],
        actions=ACK, source="alert")
    add("cal1", "fyi", "Calendar", "Weekly planning", 40,
        "Moved to Thu 11:00 (was Wed 15:00)",
        [("Calendar", 40, "Weekly planning moved: Wed 15:00 -> Thu 11:00. Organizer: Dana Levi.")],
        actions=ACK, source="calendar")
    add("rec1", "fyi", "Recorder", "meeting notes", 150,
        "Design sync notes are ready",
        [("Recorder", 150, "Notes and action items for the design sync are ready.")],
        actions=ACK, source="recording")
    return out


# Arrivals injected by the demo's fake poll (urgent items).
ARRIVALS = [
    ("Kai Brandt", "#incident-room", "Database failover is stuck, need you in the room",
     "*Failover is stuck* at step 3. Can you join the bridge now?", "Joining now."),
    ("Rina Cohen", "DM", "דחוף: הלקוח ביטל את הפגישה",
     "דחוף: הלקוח ביטל את הפגישה של מחר, צריך להחליט מה עושים.",
     "קיבלתי. בוא נדבר בעשר דקות."),
    ("Omar Haddad", "#launch-sync", "Legal sign-off blocked on your approval",
     "Legal is waiting on your sign-off for the launch copy.",
     "Signing off in 10 minutes."),
]


def arrival(i, now=None):
    now = now or time.time()
    who, where, gist, text, draft = ARRIVALS[i % len(ARRIVALS)]
    return itm.make_item(
        id=f"demo:arr{i}", source="slack", section="urgent", urgency="urgent",
        sender=who, where=where, gist=gist, ts=now,
        link="https://example.invalid/item/arrival",
        thread=[{"who": who, "ts": now, "text": text}], draft=draft,
        actions=REPLY, meta={"demo": True})


class DemoSource:
    """Stands in for `sources.collect`: a fixed inbox plus one new urgent
    arrival per poll (up to nine), after a short fake latency."""

    def __init__(self, empty=False, latency=0.8, max_arrivals=len(ARRIVALS) * 3):
        self.empty, self.latency, self.max_arrivals = empty, latency, max_arrivals
        self.now = time.time()
        self.base = [] if empty else demo_items(self.now)
        self.arrivals = []
        self.polls = 0

    def __call__(self):
        if self.latency:
            time.sleep(self.latency)
        if self.polls > 0 and len(self.arrivals) < self.max_arrivals and not self.empty:
            self.arrivals.append(arrival(len(self.arrivals)))
        self.polls += 1
        return [SourceResult("demo", self.base + self.arrivals, False, 0.0, None)]
