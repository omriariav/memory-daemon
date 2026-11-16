"""The inbox's write actions: RSVP through the Claude bridge, save through a
configured command. No Textual import; every failure becomes an `Outcome`.

RSVP (`run_rsvp`). One headless Claude call whose only allowed tool is the
configured connector tool, with its arguments pinned to the event id, the
calendar id and the answer. The bridge's PreToolUse hook denies any call that
differs by a byte, its stream re-check fails the run if one slipped through,
and its guards (state/PAUSED, the daily budget) stop the call before it
starts. The prompt carries only the tool name from `inbox.yaml` and those
three values, never item text. A run is never retried: a write is not safe to
repeat blindly.

Save (`run_save`). The configured argv runs without a shell, with the item as
JSON on stdin and nothing else (item text is never put in argv). It is bound
by the same process-group, timeout and output-cap handling as a command
source.
"""
import json
from collections import namedtuple

from .. import claude_bridge
from . import items as itm
from . import sources

Outcome = namedtuple("Outcome", "ok detail memory_id duplicate candidate",
                     defaults=(None, False, None))

ANSWERS = {"accept": "accepted", "decline": "declined",
           "tentative": "tentative"}
VERBS = {"accepted": "Accepted", "declined": "Declined",
         "tentative": "Answered tentative"}
USAGE_TAG = "inbox-rsvp"
MAX_TURNS = 4
MAX_BUDGET_USD = 0.10
DETAIL_MAX = 200


def rsvp_pins(cfg, event_id, calendar_id, response):
    """`{tool: {param: value}}` for call_tools: the exact allowlist."""
    names = cfg["args"]
    return {cfg["tool"]: {names["event_id"]: event_id,
                          names["calendar_id"]: calendar_id,
                          names["response"]: response}}


def rsvp_prompt(cfg, pins):
    """One instruction: a single call with exactly the pinned values."""
    args = pins[cfg["tool"]]
    return (
        f"Call the tool {cfg['tool']} exactly once, with exactly these "
        f"arguments and no others:\n{json.dumps(args, sort_keys=True)}\n"
        "Do not call any other tool, do not retry and do not change a "
        "value. When the call returns, reply with the single word done."
    )


def _tool_failure(text):
    """True when the tool's own answer says it failed (a JSON object with an
    error, or ok/success false). Plain text is taken as success: the bridge
    already failed the run on an error result."""
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(doc, dict):
        return False
    return bool(doc.get("error")) or doc.get("ok") is False \
        or doc.get("success") is False


def run_rsvp(cfg, base_dir, event_id, calendar_id, response, call_tools=None):
    """Answer one invite. Never raises; returns an Outcome."""
    if response not in ANSWERS.values():
        return Outcome(False, "unknown answer")
    call = call_tools or claude_bridge.call_tools
    try:
        pins = rsvp_pins(cfg, event_id, calendar_id, response)
        with claude_bridge.usage_scope(USAGE_TAG):
            results = call(
                rsvp_prompt(cfg, pins), pins,
                model=cfg.get("model", "haiku"), base_dir=base_dir,
                max_turns=MAX_TURNS, max_budget_usd=MAX_BUDGET_USD,
                timeout=cfg["timeout_s"], attempts=1)
    except claude_bridge.BridgeError as exc:
        return Outcome(False, itm.truncate(itm.clean_line(exc), DETAIL_MAX))
    except Exception as exc:  # a missing claude binary, a bad config
        return Outcome(False, f"cannot run: {type(exc).__name__}")
    tool = cfg["tool"]
    answered = next(
        (r for r in results
         if r.get("name") == tool and not r.get("is_error")
         and r.get("input") == pins[tool]), None)
    if answered is None:
        return Outcome(False, "no confirmed result for the pinned call")
    if _tool_failure(answered.get("text")):
        return Outcome(False, "the calendar tool reported a failure")
    return Outcome(True, VERBS[response])


def run_save(cfg, item, force=False):
    """Run the save command on one v1 item dict. Never raises.

    `ok: false` with `duplicate: true` is not an error: the command found a
    likely duplicate. The Outcome then has ok False, duplicate True and the
    command's `candidate`, so the UI can offer "save anyway", which calls
    again with `force=True` (the same payload plus `"force": true`)."""
    try:
        payload = json.dumps(itm.save_request(item, force)).encode("utf-8")
        status, code, out, err = sources._run_capped(
            sources._expand(cfg["argv"]), cfg["timeout_s"],
            stdin_bytes=payload)
    except (OSError, ValueError, TypeError) as exc:
        return Outcome(False, f"cannot run: {getattr(exc, 'strerror', None) or exc}")
    if status == "timeout":
        return Outcome(False, f"timed out after {cfg['timeout_s']:g}s")
    if status == "overflow":
        return Outcome(False, "response is too large")
    if code != 0:
        detail = sources._stderr_tail(err)
        return Outcome(False, f"exit {code}" + (f": {detail}" if detail else ""))
    try:
        res = itm.parse_save_response(out.decode("utf-8", "replace"))
    except itm.ItemError as exc:
        return Outcome(False, f"bad response: {exc}")
    detail = res["message"] or (
        "Already in memory" if res["duplicate"] else
        "Saved" if res["ok"] else "the save command reported a failure")
    return Outcome(res["ok"], detail, res["memory_id"], res["duplicate"],
                   res["candidate"])
