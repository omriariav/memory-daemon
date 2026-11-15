"""Inbox settings: `<base>/inbox.yaml` (gitignored) with safe defaults.

A missing file means defaults (the built-in events source only). A broken
file raises SettingsError with every problem listed, so the CLI can print it
and exit instead of silently ignoring a typo.
"""
import re
from pathlib import Path

import yaml

from . import sources, themes

FILE_NAME = "inbox.yaml"
BIDI_MODES = ("logical", "visual", "auto")
URGENT_RULE_KEYS = ("source", "kind", "key_prefix", "title_regex",
                    "starts_within_h")
TOP_KEYS = ("poll_interval_s", "theme", "bidi_mode", "sources")
SOURCE_KEYS = {
    "events": ("name", "type", "window_h", "urgent"),
    "command": ("name", "type", "argv", "timeout_s"),
}
DEFAULTS = {
    "poll_interval_s": 60.0,
    "theme": themes.DEFAULT,
    "bidi_mode": "auto",
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
            built.append({"name": name, "type": "events",
                          "window_h": float(window), "urgent": rules})
        else:
            argv = src.get("argv")
            if (not isinstance(argv, list) or not argv
                    or not all(isinstance(a, str) and a for a in argv)):
                problems.append(f"{label}.argv must be a non-empty list of "
                                "strings (no shell)")
                continue
            timeout = src.get("timeout_s", 20)
            if not _num(timeout) or not 1 <= timeout <= 120:
                problems.append(f"{label}.timeout_s must be 1 to 120")
                timeout = 20
            built.append({"name": name, "type": "command",
                          "argv": list(argv), "timeout_s": float(timeout)})
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
