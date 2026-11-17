"""Feed rules: decide what the Feed does with a row (no Textual import).

A rule is a mapping with match keys and one `action`:

    {routine: <name>, source: <inbox source>, kind: <event kind>,
     title_regex: <regex>, starts_within_h: <hours>, action: <action>}

All keys of one rule must match (a rule with no match key matches every row).
Rules are tried in order and the first match wins; a row that no rule matches
is shown. The actions:

- `show`   keep the row in the Feed (stops later rules from matching it)
- `digest` fold the routine's rows of one day into a single row
- `hide`   leave the row out of the Feed (it is counted as muted)
- `inbox`  promote the row into the Inbox view

Two lists feed the matcher: the rules of `inbox.yaml` (`feed.rules`, never
written by the inbox) and the rules made with `x` and `-` in the Feed, kept in
`inbox-state.json` (`store.InboxStore.feed_rules`). The UI rules are tried
first, so a mute you make by hand wins over a rule in the file; each can be
switched off or removed on the rules screen (`M`), and one made with a
duration expires by itself.
"""
import re
import secrets

ACTIONS = ("show", "digest", "hide", "inbox")
MATCH_KEYS = ("routine", "source", "kind", "title_regex", "starts_within_h")
RULE_KEYS = MATCH_KEYS + ("action",)
TEXT_MATCH_KEYS = ("routine", "source", "kind")
MAX_RULES = 100
TEXT_MAX = 100
REGEX_MAX = 200
HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY
# What `x` offers for how long a mute lasts: label -> seconds (None: always).
DURATIONS = (("For 1 day", DAY), ("For 1 week", WEEK), ("Always", None))
UI_MAX_UNTIL = 366 * DAY


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_rule(raw, label, problems):
    """One config rule as a clean dict, or None (the problems are appended)."""
    if not isinstance(raw, dict):
        problems.append(f"{label} must be a mapping")
        return None
    start = len(problems)
    for key in raw:
        if key not in RULE_KEYS:
            problems.append(f"{label}: unknown key '{key}'")
    action = raw.get("action")
    if action not in ACTIONS:
        problems.append(f"{label}.action must be one of {', '.join(ACTIONS)}")
    out = {"action": action}
    for key in TEXT_MATCH_KEYS:
        if key in raw:
            value = raw[key]
            if (not isinstance(value, str) or not value.strip()
                    or len(value) > TEXT_MAX):
                problems.append(f"{label}.{key} must be a non-empty string "
                                f"(up to {TEXT_MAX} characters)")
            else:
                out[key] = value.strip()
    if "title_regex" in raw:
        value = raw["title_regex"]
        try:
            if (not isinstance(value, str) or not value
                    or len(value) > REGEX_MAX):
                raise ValueError
            re.compile(value)
            out["title_regex"] = value
        except (ValueError, re.error):
            problems.append(f"{label}.title_regex must be a valid regular "
                            f"expression (up to {REGEX_MAX} characters)")
    if "starts_within_h" in raw:
        value = raw["starts_within_h"]
        if not _num(value) or not 0 < value <= 24 * 60:
            problems.append(f"{label}.starts_within_h must be a number of "
                            "hours (more than 0, up to 1440)")
        else:
            out["starts_within_h"] = float(value)
    return None if len(problems) > start else out


def check_rules(raw, label, problems):
    """The `feed.rules` list as clean dicts (problems are appended)."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_RULES:
        problems.append(f"{label} must be a list of up to {MAX_RULES} rules")
        return []
    out = []
    for n, rule in enumerate(raw):
        clean = check_rule(rule, f"{label}[{n}]", problems)
        if clean is not None:
            out.append(clean)
    return out


# --- facts about a row ------------------------------------------------------

def facts(item):
    """`(routine, source, kind, title, starts)` of an `models.Item`."""
    meta = item.meta if isinstance(item.meta, dict) else {}
    kind = meta.get("kind")
    title = meta.get("title")
    starts = meta.get("starts")
    return (item.routine, item.source,
            kind if isinstance(kind, str) else "",
            title if isinstance(title, str) and title else item.gist,
            float(starts) if _num(starts) else None)


def matches(rule, item, now):
    """True when every match key of `rule` holds for the row."""
    routine, source, kind, title, starts = facts(item)
    if "routine" in rule and rule["routine"] != routine:
        return False
    if "source" in rule and rule["source"] != source:
        return False
    if "kind" in rule and rule["kind"] != kind:
        return False
    if "title_regex" in rule:
        try:
            if not re.search(rule["title_regex"], title, re.IGNORECASE):
                return False
        except re.error:
            return False
    if "starts_within_h" in rule:
        if starts is None:
            return False
        ahead = starts - now
        if not 0 <= ahead <= float(rule["starts_within_h"]) * HOUR:
            return False
    return True


def rule_label(rule):
    """A short human label of what a rule matches: `calendar · rsvp`."""
    parts = [rule[k] for k in TEXT_MATCH_KEYS if k in rule]
    if "title_regex" in rule:
        parts.append(f"titles ~ {rule['title_regex']}")
    if "starts_within_h" in rule:
        parts.append(f"starting within {rule['starts_within_h']:g}h")
    return " · ".join(parts) or "every row"


def active(ui_rules, now):
    """The UI rules in force: switched on and not expired."""
    return [r for r in ui_rules
            if r.get("enabled", True)
            and (r.get("until") is None or r["until"] > now)]


def ordered(ui_rules, config_rules, now):
    """The rules in the order they are tried: the UI rules in force (newest
    first), then the rules of the file."""
    mine = sorted(active(ui_rules, now), key=lambda r: -r.get("created", 0))
    return mine + list(config_rules)


def classify(item, rules, now):
    """`(action, rule)` for a row: the first rule that matches wins; a row no
    rule matches is shown (`("show", None)`)."""
    for rule in rules:
        if matches(rule, item, now):
            return rule["action"], rule
    return "show", None


# --- UI rules (kept in inbox-state.json) ------------------------------------

def clean_ui_rule(raw):
    """A stored UI rule as a clean dict, or None when it is malformed."""
    if not isinstance(raw, dict):
        return None
    problems = []
    body = {k: v for k, v in raw.items() if k in RULE_KEYS}
    clean = check_rule(body, "rule", problems)
    if clean is None:
        return None
    rid = raw.get("id")
    if not isinstance(rid, str) or not re.fullmatch(r"[a-z0-9]{4,16}", rid):
        return None
    clean["id"] = rid
    clean["enabled"] = raw.get("enabled") is not False
    until = raw.get("until")
    clean["until"] = (float(until) if _num(until) and 0 < until else None)
    created = raw.get("created")
    clean["created"] = float(created) if _num(created) and created > 0 else 0.0
    return clean


def clean_ui_rules(raw):
    """The stored list as clean dicts (malformed entries dropped, at most
    MAX_RULES, ids unique)."""
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for entry in raw:
        rule = clean_ui_rule(entry)
        if rule is not None and rule["id"] not in seen:
            seen.add(rule["id"])
            out.append(rule)
    return out[:MAX_RULES]


def title_pattern(title):
    """A regex for `titles like this`: the first words of the title (numbers
    and dates generalised), anchored at the start."""
    words = re.findall(r"\S+", title or "")[:3]
    parts = []
    for word in words:
        word = re.sub(r"\d+", "\0", word)
        parts.append(r"\d+".join(re.escape(p) for p in word.split("\0")))
    return ("^" + r"\s+".join(parts))[:REGEX_MAX] if parts else ""


def new_ui_rule(item, scope, action, duration_s, now):
    """A UI rule for a mute (`hide`) or `less like this` (`digest`) made on a
    Feed row. `scope`: `kind` (this kind from this routine), `routine` or
    `title` (titles like this one, from this routine). `duration_s` None is
    forever. Returns a clean rule dict, or None when the row has nothing to
    match on for that scope."""
    routine, _source, kind, title, _starts = facts(item)
    rule = {"action": action}
    if scope in ("kind", "routine", "title") and routine:
        rule["routine"] = routine
    if scope == "kind":
        if not kind:
            return None
        rule["kind"] = kind
    elif scope == "title":
        pattern = title_pattern(title)
        if not pattern:
            return None
        rule["title_regex"] = pattern
    elif scope != "routine":
        return None
    if "routine" not in rule and "title_regex" not in rule:
        return None
    rule["id"] = secrets.token_hex(3)
    rule["enabled"] = True
    rule["until"] = (None if duration_s is None
                     else min(now + float(duration_s), now + UI_MAX_UNTIL))
    rule["created"] = float(now)
    return clean_ui_rule(rule)
