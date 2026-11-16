"""Inbox settings: `<base>/inbox.yaml` (gitignored) with safe defaults.

A missing file means defaults (the built-in events source only). A broken
file raises SettingsError with every problem listed, so the CLI can print it
and exit instead of silently ignoring a typo.
"""
import json
import os
import re
from pathlib import Path

import yaml

from . import invites, sources, themes

FILE_NAME = "inbox.yaml"
BIDI_MODES = ("logical", "visual", "auto")
THREAD_ORDERS = ("newest", "oldest")
URGENT_RULE_KEYS = ("source", "kind", "key_prefix", "title_regex",
                    "starts_within_h")
TOP_KEYS = ("poll_interval_s", "watch_interval_s", "theme", "bidi_mode", "thread_order",
            "gmail_account",
            "alert_urgent_max_age_h", "sources", "rsvp", "save", "context",
            "summary", "actions", "send", "react", "polish")
RSVP_KEYS = ("tool", "args", "timeout_s", "model")
RSVP_ARG_KEYS = ("event_id", "calendar_id", "response")
SAVE_KEYS = ("argv", "timeout_s", "mark_done")
SEND_KEYS = ("tool", "args", "timeout_s", "model", "precheck",
             "allow_channel_posts")
PRECHECK_KEYS = ("argv", "timeout_s")
SEND_ARG_KEYS = ("channel", "thread_ts", "text")
REACT_KEYS = ("tool", "args", "emojis", "timeout_s", "model", "mark_done",
              "allow_suggested")
REACT_ARG_KEYS = ("channel", "timestamp", "name")
POLISH_KEYS = ("mcp_config", "tone_tool", "tone_args", "model", "timeout_s",
               "max_budget_usd", "cache_h")
ACTION_KEYS = ("key", "label", "argv", "timeout_s", "mark_done")
CONTEXT_KEYS = ("argv", "timeout_s")
SUMMARY_KEYS = ("model", "timeout_s", "max_budget_usd")
# Every letter the inbox binds to a key of its own (app.InboxApp.BINDINGS; a
# test keeps this list equal to it). An `actions:` key must be another letter.
RESERVED_KEYS = frozenset("jkgGfaedsnorLbyYRXTmcCiIASquvEP")
ACTION_KEY_CHOICES = tuple(sorted(
    c for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if c not in RESERVED_KEYS))
MAX_ACTIONS = 8
ACTION_LABEL_MAX = 40
# A connector tool as Claude Code names it. The mcp__ prefix keeps a built-in
# (Bash, Write, ...) from ever being configured as the RSVP tool.
TOOL_NAME = re.compile(r"^mcp__[A-Za-z0-9_.-]{1,120}$")
PARAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
# No leading "-": a model name must never read as a command-line flag.
MODEL_NAME = re.compile(r"^[A-Za-z0-9_.:\[\]][A-Za-z0-9_.:\[\]-]{0,63}$")
DEFAULT_RSVP_TIMEOUT_S = 60
DEFAULT_SEND_TIMEOUT_S = 90
DEFAULT_PRECHECK_TIMEOUT_S = 10
DEFAULT_REACT_TIMEOUT_S = 60
DEFAULT_REACT_EMOJIS = ("eyes", "white_check_mark", "+1", "pray")
MAX_REACT_EMOJIS = 9          # the picker's quick keys are 1-9
# A Slack emoji name as the react call may carry it (no colons, no skin tone).
EMOJI_NAME = re.compile(r"\A[a-z0-9_+\-]{1,64}\Z")
DEFAULT_POLISH_TIMEOUT_S = 120
DEFAULT_POLISH_BUDGET_USD = 0.05
DEFAULT_POLISH_CACHE_H = 24
MAX_TONE_ARGS_CHARS = 4000
DEFAULT_SAVE_TIMEOUT_S = 30
DEFAULT_CONTEXT_TIMEOUT_S = 20
DEFAULT_ACTION_TIMEOUT_S = 60
DEFAULT_SUMMARY_TIMEOUT_S = 90
DEFAULT_SUMMARY_BUDGET_USD = 0.05
SOURCE_KEYS = {
    "events": ("name", "type", "window_h", "urgent", "invites_days",
               "saved_days", "email_require_label"),
    "command": ("name", "type", "argv", "timeout_s", "supports_all",
                "watch"),
}
DEFAULT_TIMEOUT_S = 20
MAX_WATCH = 50
DEFAULTS = {
    "poll_interval_s": 600.0,
    "watch_interval_s": 3.0,
    "theme": themes.DEFAULT,
    "bidi_mode": "auto",
    "thread_order": "newest",
    "gmail_account": 0,
    "alert_urgent_max_age_h": sources.DEFAULT_ALERT_MAX_AGE_H,
    "rsvp": None,   # no default: RSVP stays off until configured
    "save": None,   # no default: save stays off until configured
    "context": None,   # no default: the Context pane needs a command
    "summary": None,   # no default: the AI summary stays off until configured
    "actions": [],     # no default: no custom action until configured
    "send": None,      # no default: replies and nudges are demo-only until set
    "react": None,     # no default: no emoji reaction until configured
    "polish": None,    # no default: no draft polishing until configured
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
    if not _num(poll) or not 30 <= poll <= 3600:
        problems.append("poll_interval_s must be a number from 30 to 3600")
    else:
        out["poll_interval_s"] = float(poll)

    watch_s = raw.get("watch_interval_s", DEFAULTS["watch_interval_s"])
    if not _num(watch_s) or not 1 <= watch_s <= 30:
        problems.append("watch_interval_s must be a number from 1 to 30")
    else:
        out["watch_interval_s"] = float(watch_s)

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

    order = raw.get("thread_order", DEFAULTS["thread_order"])
    if order not in THREAD_ORDERS:
        problems.append(
            f"thread_order must be one of {', '.join(THREAD_ORDERS)}")
    else:
        out["thread_order"] = order

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

    out["rsvp"] = _check_rsvp(raw.get("rsvp"), problems)
    out["save"] = _check_save(raw.get("save"), problems)
    out["context"] = _check_context(raw.get("context"), problems)
    out["summary"] = _check_summary(raw.get("summary"), problems)
    out["actions"] = _check_actions(raw.get("actions"), problems)
    out["send"] = _check_send(raw.get("send"), problems)
    out["react"] = _check_react(raw.get("react"), problems)
    out["polish"] = _check_polish(raw.get("polish"), problems)

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
            watch = src.get("watch", [])
            if (not isinstance(watch, list) or len(watch) > MAX_WATCH
                    or not all(isinstance(w, str) and w.strip()
                               and not re.search(r"[\x00-\x1f\x7f]", w)
                               for w in watch)):
                problems.append(f"{label}.watch must be a list of up to "
                                f"{MAX_WATCH} paths (strings)")
                watch = []
            built.append({"name": name, "type": "command",
                          "argv": list(argv), "timeout_s": float(timeout),
                          "supports_all": supports_all,
                          "watch": [w.strip() for w in watch]})
    out["sources"] = built
    if problems:
        raise SettingsError(problems)
    return out


def _check_rsvp(raw, problems):
    """The `rsvp:` block as a clean dict, or None (absent or invalid)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("rsvp must be a mapping")
        return None
    start = len(problems)
    for key in raw:
        if key not in RSVP_KEYS:
            problems.append(f"rsvp: unknown key '{key}'")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not TOOL_NAME.match(tool):
        problems.append("rsvp.tool must be a connector tool name "
                        "(mcp__<server>__<tool>, letters, digits, _ . -)")
    args = raw.get("args")
    names = {}
    if not isinstance(args, dict):
        problems.append("rsvp.args must be a mapping of event_id, "
                        "calendar_id and response to the tool's parameter names")
    else:
        for key in args:
            if key not in RSVP_ARG_KEYS:
                problems.append(f"rsvp.args: unknown key '{key}'")
        for key in RSVP_ARG_KEYS:
            val = args.get(key)
            if not isinstance(val, str) or not PARAM_NAME.match(val):
                problems.append(f"rsvp.args.{key} must be a parameter name "
                                "(letters, digits, _ . -)")
            else:
                names[key] = val
        if len(set(names.values())) != len(names):
            problems.append("rsvp.args must name three different parameters")
    timeout = raw.get("timeout_s", DEFAULT_RSVP_TIMEOUT_S)
    # The bridge waits CONNECT_DELAY_SECONDS (6) for connectors inside the
    # timeout, so anything much lower cannot finish.
    if not _num(timeout) or not 30 <= timeout <= 300:
        problems.append("rsvp.timeout_s must be 30 to 300")
    model = raw.get("model", "haiku")
    if not isinstance(model, str) or not MODEL_NAME.match(model):
        problems.append("rsvp.model must be a model name (default haiku)")
    if len(problems) > start:
        return None
    return {"tool": tool, "args": names, "timeout_s": float(timeout),
            "model": model}


def _check_save(raw, problems):
    """The `save:` block as a clean dict, or None (absent or invalid)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("save must be a mapping")
        return None
    start = len(problems)
    for key in raw:
        if key not in SAVE_KEYS:
            problems.append(f"save: unknown key '{key}'")
    argv = raw.get("argv")
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(a, str) and a for a in argv)):
        problems.append("save.argv must be a non-empty list of strings "
                        "(no shell)")
    timeout = raw.get("timeout_s", DEFAULT_SAVE_TIMEOUT_S)
    if not _num(timeout) or not 5 <= timeout <= 120:
        problems.append("save.timeout_s must be 5 to 120")
    mark_done = raw.get("mark_done", True)
    if not isinstance(mark_done, bool):
        problems.append("save.mark_done must be true or false")
    if len(problems) > start:
        return None
    return {"argv": list(argv), "timeout_s": float(timeout),
            "mark_done": mark_done}


def _check_context(raw, problems):
    """The `context:` block as a clean dict, or None (absent or invalid)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("context must be a mapping")
        return None
    start = len(problems)
    for key in raw:
        if key not in CONTEXT_KEYS:
            problems.append(f"context: unknown key '{key}'")
    argv = raw.get("argv")
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(a, str) and a for a in argv)):
        problems.append("context.argv must be a non-empty list of strings "
                        "(no shell)")
    timeout = raw.get("timeout_s", DEFAULT_CONTEXT_TIMEOUT_S)
    if not _num(timeout) or not 2 <= timeout <= 60:
        problems.append("context.timeout_s must be 2 to 60")
    if len(problems) > start:
        return None
    return {"argv": list(argv), "timeout_s": float(timeout)}


def _check_summary(raw, problems):
    """The `summary:` block as a clean dict, or None (absent or invalid).
    An empty mapping (`summary: {}`) turns it on with every default."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("summary must be a mapping")
        return None
    start = len(problems)
    for key in raw:
        if key not in SUMMARY_KEYS:
            problems.append(f"summary: unknown key '{key}'")
    model = raw.get("model", "haiku")
    if not isinstance(model, str) or not MODEL_NAME.match(model):
        problems.append("summary.model must be a model name (default haiku)")
    timeout = raw.get("timeout_s", DEFAULT_SUMMARY_TIMEOUT_S)
    if not _num(timeout) or not 30 <= timeout <= 300:
        problems.append("summary.timeout_s must be 30 to 300")
    budget = raw.get("max_budget_usd", DEFAULT_SUMMARY_BUDGET_USD)
    if not _num(budget) or not 0.005 <= budget <= 2:
        problems.append("summary.max_budget_usd must be a number from "
                        "0.005 to 2")
    if len(problems) > start:
        return None
    return {"model": model, "timeout_s": float(timeout),
            "max_budget_usd": float(budget)}


def _check_tool_args(raw, label, keys, problems):
    """`{key: parameter name}` for a pinned connector call, or None. Every
    one of `keys` is required; the names must differ and nothing else may
    be there."""
    if not isinstance(raw, dict):
        problems.append(f"{label} must be a mapping of "
                        f"{', '.join(keys)} to the tool's parameter names")
        return None
    start = len(problems)
    names = {}
    for key in raw:
        if key not in keys:
            problems.append(f"{label}: unknown key '{key}'")
    for key in keys:
        val = raw.get(key)
        if not isinstance(val, str) or not PARAM_NAME.match(val):
            problems.append(f"{label}.{key} must be a parameter name "
                            "(letters, digits, _ . -)")
        else:
            names[key] = val
    if len(set(names.values())) != len(names):
        problems.append(f"{label} must name {len(keys)} different parameters")
    return names if len(problems) == start else None


def _check_send(raw, problems):
    """The `send:` block as a clean dict, or None (absent or invalid)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("send must be a mapping")
        return None
    start = len(problems)
    for key in raw:
        if key not in SEND_KEYS:
            problems.append(f"send: unknown key '{key}'")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not TOOL_NAME.match(tool):
        problems.append("send.tool must be a connector tool name "
                        "(mcp__<server>__<tool>, letters, digits, _ . -)")
    names = _check_tool_args(raw.get("args"), "send.args", SEND_ARG_KEYS,
                             problems)
    timeout = raw.get("timeout_s", DEFAULT_SEND_TIMEOUT_S)
    if not _num(timeout) or not 30 <= timeout <= 300:
        problems.append("send.timeout_s must be 30 to 300")
    model = raw.get("model", "haiku")
    if not isinstance(model, str) or not MODEL_NAME.match(model):
        problems.append("send.model must be a model name (default haiku)")
    precheck = _check_precheck(raw.get("precheck"), problems)
    allow_channel = raw.get("allow_channel_posts", False)
    if not isinstance(allow_channel, bool):
        problems.append("send.allow_channel_posts must be true or false")
    if len(problems) > start:
        return None
    return {"tool": tool, "args": names, "timeout_s": float(timeout),
            "model": model, "precheck": precheck,
            "allow_channel_posts": allow_channel}


def _check_precheck(raw, problems):
    """The optional `send.precheck:` block as a clean dict, or None."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("send.precheck must be a mapping")
        return None
    start = len(problems)
    for key in raw:
        if key not in PRECHECK_KEYS:
            problems.append(f"send.precheck: unknown key '{key}'")
    argv = raw.get("argv")
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(a, str) and a for a in argv)):
        problems.append("send.precheck.argv must be a non-empty list of "
                        "strings (no shell)")
    timeout = raw.get("timeout_s", DEFAULT_PRECHECK_TIMEOUT_S)
    if not _num(timeout) or not 1 <= timeout <= 30:
        problems.append("send.precheck.timeout_s must be 1 to 30")
    if len(problems) > start:
        return None
    return {"argv": list(argv), "timeout_s": float(timeout)}


def _check_react(raw, problems):
    """The `react:` block as a clean dict, or None (absent or invalid)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("react must be a mapping")
        return None
    start = len(problems)
    for key in raw:
        if key not in REACT_KEYS:
            problems.append(f"react: unknown key '{key}'")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not TOOL_NAME.match(tool):
        problems.append("react.tool must be a connector tool name "
                        "(mcp__<server>__<tool>, letters, digits, _ . -)")
    names = _check_tool_args(raw.get("args"), "react.args", REACT_ARG_KEYS,
                             problems)
    emojis = raw.get("emojis", list(DEFAULT_REACT_EMOJIS))
    if (not isinstance(emojis, list) or not 1 <= len(emojis) <= MAX_REACT_EMOJIS
            or not all(isinstance(e, str) and EMOJI_NAME.match(e)
                       for e in emojis)):
        problems.append(
            f"react.emojis must be a list of 1 to {MAX_REACT_EMOJIS} emoji "
            "names (a-z, 0-9, _ + -, no colons, up to 64 characters)")
    elif len(set(emojis)) != len(emojis):
        problems.append("react.emojis has a name twice")
    timeout = raw.get("timeout_s", DEFAULT_REACT_TIMEOUT_S)
    if not _num(timeout) or not 30 <= timeout <= 300:
        problems.append("react.timeout_s must be 30 to 300")
    model = raw.get("model", "haiku")
    if not isinstance(model, str) or not MODEL_NAME.match(model):
        problems.append("react.model must be a model name (default haiku)")
    mark_done = raw.get("mark_done", True)
    if not isinstance(mark_done, bool):
        problems.append("react.mark_done must be true or false")
    allow_suggested = raw.get("allow_suggested", False)
    if not isinstance(allow_suggested, bool):
        problems.append("react.allow_suggested must be true or false")
    if len(problems) > start:
        return None
    return {"tool": tool, "args": names, "emojis": list(emojis),
            "timeout_s": float(timeout), "model": model,
            "mark_done": mark_done, "allow_suggested": allow_suggested}


def _check_polish(raw, problems):
    """The `polish:` block as a clean dict, or None (absent or invalid)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("polish must be a mapping")
        return None
    start = len(problems)
    for key in raw:
        if key not in POLISH_KEYS:
            problems.append(f"polish: unknown key '{key}'")
    path = raw.get("mcp_config")
    if (not isinstance(path, str) or not path.strip() or "\0" in path
            or not os.path.isabs(os.path.expanduser(path.strip()))):
        problems.append("polish.mcp_config must be an absolute path to a "
                        "JSON file of MCP servers (a leading ~ is fine)")
        path = None
    else:
        path = os.path.expanduser(path.strip())
    tool = raw.get("tone_tool")
    if not isinstance(tool, str) or not TOOL_NAME.match(tool):
        problems.append("polish.tone_tool must be a connector tool name "
                        "(mcp__<server>__<tool>, letters, digits, _ . -)")
    args = raw.get("tone_args", {})
    try:
        size = len(json.dumps(args, sort_keys=True)) \
            if isinstance(args, dict) else None
    except (TypeError, ValueError):
        size = None
    if (size is None or size > MAX_TONE_ARGS_CHARS
            or not all(isinstance(k, str) and PARAM_NAME.match(k)
                       for k in args)):
        problems.append(
            "polish.tone_args must be a mapping of parameter names to JSON "
            f"values (up to {MAX_TONE_ARGS_CHARS} characters)")
        args = {}
    model = raw.get("model", "sonnet")
    if not isinstance(model, str) or not MODEL_NAME.match(model):
        problems.append("polish.model must be a model name (default sonnet)")
    timeout = raw.get("timeout_s", DEFAULT_POLISH_TIMEOUT_S)
    if not _num(timeout) or not 30 <= timeout <= 300:
        problems.append("polish.timeout_s must be 30 to 300")
    budget = raw.get("max_budget_usd", DEFAULT_POLISH_BUDGET_USD)
    if not _num(budget) or not 0.005 <= budget <= 2:
        problems.append("polish.max_budget_usd must be a number from "
                        "0.005 to 2")
    cache_h = raw.get("cache_h", DEFAULT_POLISH_CACHE_H)
    if not _num(cache_h) or not 0 <= cache_h <= 720:
        problems.append("polish.cache_h must be a number from 0 (no cache) "
                        "to 720")
    if len(problems) > start:
        return None
    return {"mcp_config": path, "tone_tool": tool, "tone_args": dict(args),
            "model": model, "timeout_s": float(timeout),
            "max_budget_usd": float(budget), "cache_h": float(cache_h)}


def _check_actions(raw, problems):
    """The `actions:` list as clean dicts (empty when absent or invalid)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append("actions must be a list")
        return []
    if len(raw) > MAX_ACTIONS:
        problems.append(f"actions has more than {MAX_ACTIONS} entries")
        return []
    clean, keys, labels = [], set(), set()
    for i, entry in enumerate(raw):
        where = f"actions[{i}]"
        start = len(problems)
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a mapping")
            continue
        for k in entry:
            if k not in ACTION_KEYS:
                problems.append(f"{where}: unknown key '{k}'")
        key = entry.get("key")
        if not isinstance(key, str) or len(key) != 1 \
                or not key.isascii() or not key.isalpha():
            problems.append(f"{where}.key must be one letter (a-z, A-Z)")
        elif key in RESERVED_KEYS:
            problems.append(
                f"{where}.key '{key}' is already used by the inbox; free "
                f"keys are {''.join(ACTION_KEY_CHOICES)}")
        elif key in keys:
            problems.append(f"{where}.key '{key}' is used twice")
        else:
            keys.add(key)
        label = entry.get("label")
        if (not isinstance(label, str) or not label.strip()
                or len(label.strip()) > ACTION_LABEL_MAX
                or re.search(r"[\x00-\x1f\x7f]", label)):
            problems.append(f"{where}.label must be one line of 1 to "
                            f"{ACTION_LABEL_MAX} characters")
        elif label.strip() in labels:
            problems.append(f"{where}.label is used twice")
        else:
            labels.add(label.strip())
        argv = entry.get("argv")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(a, str) and a for a in argv)):
            problems.append(f"{where}.argv must be a non-empty list of "
                            "strings (no shell)")
        timeout = entry.get("timeout_s", DEFAULT_ACTION_TIMEOUT_S)
        if not _num(timeout) or not 5 <= timeout <= 300:
            problems.append(f"{where}.timeout_s must be 5 to 300")
        mark_done = entry.get("mark_done", True)
        if not isinstance(mark_done, bool):
            problems.append(f"{where}.mark_done must be true or false")
        if len(problems) == start:
            clean.append({"key": key, "label": label.strip(),
                          "argv": list(argv), "timeout_s": float(timeout),
                          "mark_done": mark_done})
    return clean


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
