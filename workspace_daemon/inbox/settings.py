"""Inbox settings: `<base>/inbox.yaml` (gitignored) with safe defaults.

A missing file means defaults (the built-in events source only). A broken
file raises SettingsError with every problem listed, so the CLI can print it
and exit instead of silently ignoring a typo.
"""
import re
from pathlib import Path

import yaml

from . import invites, sources, themes

FILE_NAME = "inbox.yaml"
BIDI_MODES = ("logical", "visual", "auto")
URGENT_RULE_KEYS = ("source", "kind", "key_prefix", "title_regex",
                    "starts_within_h")
TOP_KEYS = ("poll_interval_s", "theme", "bidi_mode", "gmail_account",
            "alert_urgent_max_age_h", "sources")
SOURCE_KEYS = {
    "events": ("name", "type", "window_h", "urgent", "invites_days",
               "saved_days", "email_require_label"),
    "command": ("name", "type", "argv", "timeout_s", "supports_all"),
}
DEFAULT_TIMEOUT_S = 20
DEFAULTS = {
    "poll_interval_s": 60.0,
    "theme": themes.DEFAULT,
    "bidi_mode": "auto",
    "gmail_account": 0,
    "alert_urgent_max_age_h": sources.DEFAULT_ALERT_MAX_AGE_H,
}


class SettingsError(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def default_settings():
    return validate({})


def load(base_dir, path=None):
    path = Path(path) if path else Path(base_dir) / FILE_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_settings()
    except OSError as exc:
        raise SettingsError([f"cannot read {path}: {exc.strerror or exc}"])
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SettingsError([f"{path.name}: invalid YAML: {exc}"])
    return validate(raw if raw is not None else {})


def validate(raw):
    """Normalised settings from a parsed document, or SettingsError."""
    if not isinstance(raw, dict):
        raise SettingsError(["inbox.yaml must be a mapping"])
    problems = []
    for key in raw:
        if key not in TOP_KEYS:
            problems.append(f"unknown key '{key}'")
    out = dict(DEFAULTS)

    poll = raw.get("poll_interval_s", DEFAULTS["poll_interval_s"])
    if not _num(poll) or not 5 <= poll <= 3600:
        problems.append("poll_interval_s must be a number from 5 to 3600")
    else:
        out["poll_interval_s"] = float(poll)

    theme = raw.get("theme", DEFAULTS["theme"])
    if theme not in themes.NAMES:
        problems.append(f"theme must be one of {', '.join(themes.NAMES)}")
    else:
        out["theme"] = theme

    bidi = raw.get("bidi_mode", DEFAULTS["bidi_mode"])
    if bidi not in BIDI_MODES:
        problems.append(f"bidi_mode must be one of {', '.join(BIDI_MODES)}")
    else:
        out["bidi_mode"] = bidi

    account = raw.get("gmail_account", DEFAULTS["gmail_account"])
    if not isinstance(account, int) or isinstance(account, bool) or account < 0:
        problems.append("gmail_account must be an integer >= 0")
    else:
        out["gmail_account"] = account

    max_age = raw.get("alert_urgent_max_age_h",
                      DEFAULTS["alert_urgent_max_age_h"])
    if not _num(max_age) or not 1 <= max_age <= 168:
        problems.append("alert_urgent_max_age_h must be a number from 1 to 168")
    else:
        out["alert_urgent_max_age_h"] = float(max_age)

    raw_sources = raw.get("sources")
    if raw_sources is None:
        raw_sources = [{"name": "events", "type": "events"}]
    if not isinstance(raw_sources, list) or not raw_sources:
        problems.append("sources must be a non-empty list")
        raw_sources = []
    names, built = set(), []
    for i, src in enumerate(raw_sources):
        label = f"sources[{i}]"
        if not isinstance(src, dict):
            problems.append(f"{label} must be a mapping")
            continue
        stype = src.get("type")
        if stype not in SOURCE_KEYS:
            problems.append(f"{label}.type must be 'events' or 'command'")
            continue
        for key in src:
            if key not in SOURCE_KEYS[stype]:
                problems.append(f"{label}: unknown key '{key}' for {stype}")
        name = src.get("name", stype)
        if not isinstance(name, str) or not re.match(r"^[\w.-]{1,40}$", name):
            problems.append(f"{label}.name must be 1-40 chars of [\\w.-]")
            continue
        if name in names:
            problems.append(f"{label}.name '{name}' is used twice")
        names.add(name)
        if stype == "events":
            window = src.get("window_h", sources.DEFAULT_WINDOW_H)
            if not _num(window) or not 1 <= window <= 24 * 30:
                problems.append(f"{label}.window_h must be 1 to 720 hours")
                window = sources.DEFAULT_WINDOW_H
            rules = src.get("urgent")
            rules = (list(sources.DEFAULT_URGENT_RULES) if rules is None
                     else _check_rules(rules, f"{label}.urgent", problems))
            invites_days = src.get("invites_days", invites.DEFAULT_DAYS)
            if not _num(invites_days) or not 0 <= invites_days <= 60:
                problems.append(f"{label}.invites_days must be 0 (off) to 60")
                invites_days = invites.DEFAULT_DAYS
            saved_days = src.get("saved_days", sources.DEFAULT_SAVED_DAYS)
            if not _num(saved_days) or not 1 <= saved_days <= 30:
                problems.append(f"{label}.saved_days must be 1 to 30")
                saved_days = sources.DEFAULT_SAVED_DAYS
            require = src.get("email_require_label")
            if require is not None and (
                    not isinstance(require, str) or not require.strip()
                    or len(require) > 100
                    or re.search(r"[\x00-\x1f\x7f]", require)):
                problems.append(f"{label}.email_require_label must be null or "
                                "a label name (one line, up to 100 chars)")
                require = None
            built.append({"name": name, "type": "events",
                          "window_h": float(window), "urgent": rules,
                          "invites_days": float(invites_days),
                          "saved_days": float(saved_days),
                          "email_require_label": (
                              require.strip() if require else None)})
        else:
            argv = src.get("argv")
            if (not isinstance(argv, list) or not argv
                    or not all(isinstance(a, str) and a for a in argv)):
                problems.append(f"{label}.argv must be a non-empty list of "
                                "strings (no shell)")
                continue
            timeout = src.get("timeout_s")
            if timeout is None:  # default, kept under the poll interval
                timeout = min(DEFAULT_TIMEOUT_S, out["poll_interval_s"] - 1)
            if not _num(timeout) or not 1 <= timeout <= 120:
                problems.append(f"{label}.timeout_s must be 1 to 120")
                timeout = DEFAULT_TIMEOUT_S
            elif timeout >= out["poll_interval_s"]:
                # A slow source would still be running when the next poll
                # is due, and polls do not overlap.
                problems.append(
                    f"{label}.timeout_s ({timeout:g}) must be below "
                    f"poll_interval_s ({out['poll_interval_s']:g})")
            supports_all = src.get("supports_all", False)
            if not isinstance(supports_all, bool):
                problems.append(f"{label}.supports_all must be true or false")
                supports_all = False
            built.append({"name": name, "type": "command",
                          "argv": list(argv), "timeout_s": float(timeout),
                          "supports_all": supports_all})
    out["sources"] = built
    if problems:
        raise SettingsError(problems)
    return out


def _check_rules(rules, label, problems):
    if not isinstance(rules, list):
        problems.append(f"{label} must be a list")
        return []
    clean = []
    for i, rule in enumerate(rules):
        where = f"{label}[{i}]"
        if not isinstance(rule, dict) or not rule:
            problems.append(f"{where} must be a non-empty mapping")
            continue
        ok = True
        for key, val in rule.items():
            if key not in URGENT_RULE_KEYS:
                problems.append(f"{where}: unknown key '{key}'")
                ok = False
            elif key == "starts_within_h":
                if not _num(val) or val <= 0:
                    problems.append(f"{where}.starts_within_h must be > 0")
                    ok = False
            elif not isinstance(val, str) or not val:
                problems.append(f"{where}.{key} must be a string")
                ok = False
            elif key == "title_regex":
                try:
                    re.compile(val)
                except re.error as exc:
                    problems.append(f"{where}.title_regex: {exc}")
                    ok = False
        if ok:
            clean.append(dict(rule))
    return clean
