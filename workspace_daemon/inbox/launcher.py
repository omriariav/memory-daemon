"""The inbox's generic `task_launcher:` hook (0.13.3): start a task session
from a row. No Textual import; every failure becomes a plain result.

One configured command, two calls (JSON on stdin, one JSON line on stdout, no
shell, item text never in argv):

  draft    {"v": 1, "action": "draft", "item": {...}, "fields": {...}}
           -> {"v": 1, "ok": true, "fields": {...}, "templates": [...],
               "message": ""}                 read-only; may take a while
  launch   {"v": 1, "action": "launch", "item": {...}, "fields": {...}}
           -> {"v": 1, "ok": true|false, "name": "<card name>",
               "message": "<one line>"}       the only call that starts a task

`item` is the same export the `actions:` protocol sends. `fields` is
`{name, role, model, effort, goal, acceptance: [..], persona, template}`; an
empty `model` or `effort` means "the launcher's default". Everything a command
prints is untrusted display text: it is validated and cleaned here before any
widget sees it, and cut to fixed caps. Unknown keys are ignored. The command
runs under the capped runner (own process group, SIGTERM then SIGKILL after
TERM_GRACE_S on a timeout, a cap on the raw output). `draft` can be cancelled;
`launch` never is, and is never retried.

Also here, with no I/O: `TaskForm`, the state of the `t` modal (local
defaults, what the user has edited, what a draft may still fill in), and the
badge mapping from an agent row (`meta.source_key`) to the row it came from.
"""
import json
import re
from collections import namedtuple
from dataclasses import dataclass, field

from . import items as itm
from . import sources
from . import tasks as tasksmod

PROTOCOL_VERSION = 1
NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,40}\Z")
NAME_MAX = 41
GOAL_MAX = 300
ACCEPT_MAX = 5
ACCEPT_LINE_MAX = 200
PERSONA_MAX = 400
TEMPLATE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}\Z")
MAX_TEMPLATES = 12
MESSAGE_MAX = 300
LABEL_MAX = 40
SOURCE_KEY_MAX = 300
MAX_RAW_BYTES = 256 * 1024
TERM_GRACE_S = 5
FIELD_NAMES = ("name", "role", "model", "effort", "goal", "acceptance",
               "persona", "template")

# `extra`: draft fields the modal does not show (`{key: scalar | [str]}`),
# kept and sent back unchanged in the launch call's `fields`.
Draft = namedtuple("Draft", "ok fields templates message extra",
                   defaults=({},))
EXTRA_KEY = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
MAX_EXTRA = 8
EXTRA_STR_MAX = 2000
EXTRA_LIST_MAX = 20
EXTRA_ITEM_MAX = 500
# `unclear`: the launch may have started (a timeout, a crash, a reply the
# inbox cannot read); only an explicit `ok: false` is a failure.
Launched = namedtuple("Launched", "ok name message unclear",
                      defaults=(False,))
UNCLEAR = "launch result unclear; it may have started (check the Agents view)"

# Which role a row starts with, by what the row is. A name is used only when
# the configured `roles:` list has it, else the first configured role.
ROLE_BY_KIND = {"pr": "reviewer", "decision": "analyst", "other": "researcher"}
GOAL_BY_KIND = {"pr": "Review: {title}",
                "decision": "Recommend a decision on: {title}",
                "other": "Look into: {title}"}
_PR_LINK = re.compile(
    r"https?://[^\s/]+/[^\s]*?/(?:pulls?|pull-requests|merge_requests)/\d+")


# --- the protocol ----------------------------------------------------------

def request(action, item, fields):
    """The stdin of one call. `fields` may carry extra keys (see
    `clean_extra`); they go along unchanged, after the known ones."""
    out = dict(clean_extra(fields))
    out.update({
        "name": fields.get("name", ""), "role": fields.get("role", ""),
        "model": fields.get("model", ""),
        "effort": fields.get("effort", ""),
        "goal": fields.get("goal", ""),
        "acceptance": list(fields.get("acceptance") or []),
        "persona": fields.get("persona", ""),
        "template": fields.get("template", "")})
    return {"v": PROTOCOL_VERSION, "action": action, "item": item,
            "fields": out}


def clean_extra(raw):
    """The keys of `raw` that are not modal fields, when their values are
    safe to pass through: a key of `[a-z][a-z0-9_]{0,31}`, a value that is a
    JSON scalar (a string of up to EXTRA_STR_MAX characters, a finite number,
    a boolean, null) or a list of up to EXTRA_LIST_MAX strings of up to
    EXTRA_ITEM_MAX. At most MAX_EXTRA keys; anything else is dropped. Values
    are kept exactly as given."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if (len(out) >= MAX_EXTRA or not isinstance(key, str)
                or key in FIELD_NAMES or not EXTRA_KEY.match(key)):
            continue
        if isinstance(value, str):
            ok = len(value) <= EXTRA_STR_MAX
        elif isinstance(value, list):
            ok = (len(value) <= EXTRA_LIST_MAX and all(
                isinstance(v, str) and len(v) <= EXTRA_ITEM_MAX
                for v in value))
        else:
            ok = value is None or isinstance(value, bool) or (
                isinstance(value, (int, float)) and itm._is_number(value))
        if ok:
            out[key] = list(value) if isinstance(value, list) else value
    return out


def _load(text):
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise itm.ItemError(f"response is not JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise itm.ItemError("response is not a JSON object")
    if "v" in doc and (doc["v"] != PROTOCOL_VERSION
                       or isinstance(doc["v"], bool)):
        raise itm.ItemError(f"response 'v' must be {PROTOCOL_VERSION}")
    return doc


def _ok(doc):
    ok = doc.get("ok")
    if not isinstance(ok, bool):
        raise itm.ItemError("response needs ok (bool)")
    return ok


def _message(doc):
    message = doc.get("message")
    if message is not None and not isinstance(message, str):
        raise itm.ItemError("response message must be a string or null")
    return first_line(message) if message else ""


def first_line(text):
    """The first non-empty line of `text`, cleaned, for a toast."""
    for line in str(text or "").splitlines():
        line = itm.clean_line(line)
        if line:
            return itm.truncate(line, MESSAGE_MAX)
    return ""


def _clean_fields(raw, cfg, taken=()):
    """The fields of a draft reply that are usable, as `{name: value}`. A
    field that is missing, the wrong type or not allowed (a role or model the
    config does not list, a bad name) is left out, so the local value stays."""
    out = {}
    if not isinstance(raw, dict):
        return out
    name = raw.get("name")
    if isinstance(name, str) and NAME_RE.match(name) and name not in taken:
        out["name"] = name
    for key, allowed in (("role", cfg["roles"]), ("model", cfg["models"]),
                         ("effort", cfg["efforts"])):
        value = raw.get(key)
        if isinstance(value, str) and (
                value in allowed or (value == "" and key != "role")):
            out[key] = value
    goal = raw.get("goal")
    if isinstance(goal, str):
        goal = itm.truncate(itm.clean_line(goal), GOAL_MAX)
        if goal:
            out["goal"] = goal
    accept = raw.get("acceptance")
    if isinstance(accept, list):
        lines = [itm.truncate(itm.clean_line(a), ACCEPT_LINE_MAX)
                 for a in accept if isinstance(a, str)]
        lines = [a for a in lines if a][:ACCEPT_MAX]
        if lines:
            out["acceptance"] = lines
    persona = raw.get("persona")
    if isinstance(persona, str):
        persona = itm.truncate(itm.clean_text(persona).strip(), PERSONA_MAX)
        if persona:
            out["persona"] = persona
    template = raw.get("template")
    if isinstance(template, str) and TEMPLATE_RE.match(template):
        out["template"] = template
    return out


def parse_draft(text, cfg, taken=()):
    """`{"ok": bool, "fields"?: {...}, "templates"?: [str], "message"?: str}`
    -> `Draft` (fields cleaned, see `_clean_fields`). Raises `ItemError` on a
    reply that is not JSON, is not an object, or has no bool `ok`."""
    doc = _load(text)
    ok = _ok(doc)
    message = _message(doc)
    if not ok:
        return Draft(False, {}, [], message, {})
    templates = []
    raw = doc.get("templates")
    if isinstance(raw, list):
        for t in raw:
            if (isinstance(t, str) and TEMPLATE_RE.match(t)
                    and t not in templates):
                templates.append(t)
    return Draft(True, _clean_fields(doc.get("fields"), cfg, taken),
                 templates[:MAX_TEMPLATES], message,
                 clean_extra(doc.get("fields")))


def parse_launch(text):
    """`{"ok": bool, "name"?: str, "message"?: str}` -> `Launched`. Once `ok`
    is a bool the rest is read leniently: a bad `v`, a `message` that is not a
    string and a `name` that is not a valid card name are dropped, never
    raised (the task may already be running). Raises `ItemError` only for a
    reply that is not a JSON object or has no bool `ok`."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise itm.ItemError(f"response is not JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise itm.ItemError("response is not a JSON object")
    ok = _ok(doc)
    name = doc.get("name")
    if not (isinstance(name, str) and NAME_RE.match(name)):
        name = ""
    message = doc.get("message")
    return Launched(ok, name, first_line(message)
                    if isinstance(message, str) else "")


def _call(cfg, doc, timeout_s, cancel=None):
    """Run the command once. Returns `(None, text)` or `(reason, None)`."""
    try:
        payload = json.dumps(doc).encode("utf-8")
        status, code, out, err = sources._run_capped(
            sources._expand(cfg["argv"]), timeout_s, stdin_bytes=payload,
            term_grace_s=TERM_GRACE_S, max_bytes=MAX_RAW_BYTES, cancel=cancel)
    except (OSError, ValueError, TypeError) as exc:
        return ("cannot run: "
                f"{getattr(exc, 'strerror', None) or exc}"), None
    if status == "cancelled":
        return "cancelled", None
    if status == "timeout":
        return f"timed out after {timeout_s:g}s", None
    if status == "overflow":
        return "response is too large", None
    if code != 0:
        detail = sources._stderr_tail(err)
        return f"exit {code}" + (f": {detail}" if detail else ""), None
    return None, out.decode("utf-8", "replace")


def run_draft(cfg, item, fields, taken=(), cancel=None):
    """Ask the command to improve the fields. Never raises. `Draft.ok` False
    means the local defaults stay (`message` says why)."""
    reason, text = _call(cfg, request("draft", item, fields),
                         cfg["draft_timeout_s"], cancel)
    if reason:
        return Draft(False, {}, [], reason)
    try:
        res = parse_draft(text, cfg, taken)
    except itm.ItemError as exc:
        return Draft(False, {}, [], f"bad response: {exc}")
    if not res.ok:
        return res._replace(message=res.message
                            or "the launcher could not draft")
    return res


def run_launch(cfg, item, fields):
    """Start the task. Never raises, never retried, never cancelled: a
    timeout asks the command to stop (SIGTERM, SIGKILL after TERM_GRACE_S).
    Only a command that could not start, or an explicit `ok: false`, is a
    failure; a timeout, a non-zero exit, output that is not JSON or a reply
    with no bool `ok` is `unclear` (the task may have started)."""
    reason, text = _call(cfg, request("launch", item, fields),
                         cfg["timeout_s"])
    if reason:
        if reason.startswith("cannot run"):      # nothing was started
            return Launched(False, "", reason)
        return Launched(False, "", f"{UNCLEAR} ({reason})", True)
    try:
        res = parse_launch(text)
    except itm.ItemError as exc:
        return Launched(False, "", f"{UNCLEAR} ({exc})", True)
    if res.ok:
        return res._replace(message=res.message or "Task started")
    return res._replace(message=res.message
                        or "the launcher reported a failure")


# --- the form --------------------------------------------------------------

def slug(text):
    """A card name from a title: lowercase a-z, 0-9 and single dashes, at most
    NAME_MAX characters, `task` when nothing is left (a title with no ASCII
    letters)."""
    base = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    base = base[:NAME_MAX].strip("-")
    return base or "task"


def unique_name(base, taken):
    """`base`, or `base-2`, `base-3`, ... when it is in `taken`."""
    taken = set(taken)
    if base not in taken:
        return base
    n = 2
    while True:
        tail = f"-{n}"
        cand = base[:NAME_MAX - len(tail)].rstrip("-") + tail
        if cand not in taken:
            return cand
        n += 1


def row_kind(item):
    """`decision`, `pr` or `other`: what the default role and goal follow."""
    if tasksmod.is_task(item) and tasksmod.task_type(item) == tasksmod.DECISION:
        return "decision"
    texts = [item.link or "", item.gist or ""]
    texts += [m.text or "" for m in (item.messages or [])[-3:]]
    if any(_PR_LINK.search(t) for t in texts):
        return "pr"
    return "other"


def default_role(kind, roles):
    want = ROLE_BY_KIND.get(kind, ROLE_BY_KIND["other"])
    return want if want in roles else (roles[0] if roles else "")


@dataclass
class TaskForm:
    """What the `t` modal edits. The local defaults come first; a draft only
    fills a field the user has not touched. No I/O."""

    name: str = ""
    role: str = ""
    model: str = ""            # "" = the launcher's default
    effort: str = ""
    goal: str = ""
    acceptance: list = field(default_factory=list)
    persona: str = ""
    template: str = ""
    roles: list = field(default_factory=list)
    models: list = field(default_factory=list)
    efforts: list = field(default_factory=list)
    templates: list = field(default_factory=list)
    taken: frozenset = frozenset()
    touched: set = field(default_factory=set)
    extra: dict = field(default_factory=dict)   # draft fields not shown here

    @classmethod
    def local(cls, item, cfg, taken=()):
        """The form of a row with no model call: a slug name, a role by the
        row's kind, a goal from a template."""
        kind = row_kind(item)
        title = itm.truncate(itm.clean_line(item.gist or item.who), 120)
        taken = frozenset(taken)
        return cls(
            name=unique_name(slug(title), taken),
            role=default_role(kind, cfg["roles"]),
            goal=itm.truncate(GOAL_BY_KIND[kind].format(title=title),
                              GOAL_MAX),
            roles=list(cfg["roles"]), models=list(cfg["models"]),
            efforts=list(cfg["efforts"]), taken=taken)

    def edit(self, key, value):
        """The user typed or picked `value` for `key`: it is theirs now."""
        setattr(self, key, value)
        self.touched.add(key)

    def cycle(self, key, step=1):
        """Move a chip (`model`, `effort`, `template`) to the next choice;
        the empty choice (the launcher's default) is the first stop."""
        pool = {"model": self.models, "effort": self.efforts,
                "template": self.templates}[key]
        stops = [""] + list(pool)
        cur = getattr(self, key)
        at = stops.index(cur) if cur in stops else 0
        self.edit(key, stops[(at + step) % len(stops)])

    def pick_role(self, n):
        """Role chip `n` (1-based). False when there is no such chip."""
        if not 1 <= n <= len(self.roles):
            return False
        self.edit("role", self.roles[n - 1])
        return True

    def apply_draft(self, draft):
        """Fill the fields the user has not touched from a `Draft`. Returns
        the names of the fields that changed."""
        if not draft.ok:
            return []
        for t in draft.templates:
            if t not in self.templates:
                self.templates.append(t)
        self.extra.update(draft.extra or {})
        done = []
        for key, value in draft.fields.items():
            if key in self.touched or getattr(self, key) == value:
                continue
            if key == "template" and value not in self.templates:
                self.templates.append(value)
            setattr(self, key, value)
            done.append(key)
        return done

    def problem(self):
        """Why the form cannot be launched, or ''."""
        if not NAME_RE.match(self.name or ""):
            return ("the name is a-z, 0-9 and dashes, 1 to "
                    f"{NAME_MAX} characters, starting with a letter or digit")
        if self.name in self.taken:
            return f"the name {self.name} is taken"
        if self.role not in self.roles:
            return "pick a role"
        if not (self.goal or "").strip():
            return "a goal is required"
        return ""

    def fields(self):
        return {**self.extra, "name": self.name, "role": self.role, "model": self.model,
                "effort": self.effort, "goal": self.goal.strip(),
                "acceptance": list(self.acceptance),
                "persona": self.persona, "template": self.template}


# --- the badge -------------------------------------------------------------

Card = namedtuple("Card", "key name role state completed link")
COMPLETED_STATES = ("completed", "done")


def card_of(item):
    """The card an agent row (`meta.kind: agent`) stands for, or None when it
    names no row (`meta.source_key`). `name`, `role` and `state` come from
    `meta` (the row's sender, and the bucket word, stand in when a source
    leaves them out)."""
    meta = item.meta if isinstance(item.meta, dict) else {}
    if meta.get("kind") != "agent":
        return None
    sk = meta.get("source_key")
    if not isinstance(sk, str) or not sk or len(sk) > SOURCE_KEY_MAX:
        return None

    def text(key, fallback=""):
        v = meta.get(key)
        v = itm.clean_line(v) if isinstance(v, str) else ""
        return itm.truncate(v or itm.clean_line(fallback), LABEL_MAX)

    bucket = meta.get("bucket")
    state = text("state").replace("_", " ")
    completed = (meta.get("state") in COMPLETED_STATES
                 or bucket == "done")
    if not state:
        state = {"needs_you": "needs you", "working": "working",
                 "done": "done"}.get(bucket, "")
    return Card(sk, text("name", item.who), text("role"), state, completed,
                item.link if itm.valid_link(item.link) else None)


def index_cards(rows):
    """`{source_key: (agent row, Card)}` for the agent rows in `rows`. When a
    row has several cards the newest open one wins, else the newest."""
    best = {}
    for it in rows:
        card = card_of(it)
        if card is None:
            continue
        rank = (not card.completed, it.ts)
        if card.key not in best or rank > best[card.key][0]:
            best[card.key] = (rank, it, card)
    return {k: (it, card) for k, (_r, it, card) in best.items()}


def badge_text(card):
    """`▶ <name> · <role> · <state>` (a part a source left out is skipped)."""
    parts = [p for p in (card.name, card.role, card.state) if p]
    return "▶ " + " · ".join(parts)
