"""The inbox's write actions: RSVP through the Claude bridge, save through a
configured command. No Textual import; every failure becomes an `Outcome`.

RSVP (`run_rsvp`). One headless Claude call whose only allowed tool is the
configured connector tool, with its arguments pinned to the event id, the
calendar id and the answer, and the call must carry exactly those arguments
and no others (`exact_args=True`: the bridge's PreToolUse hook denies a call
that differs by a byte or adds an argument, and its stream re-check fails the
run if one ran anyway). The bridge's guards (state/PAUSED, the daily budget) stop the call before it
starts. The prompt carries only the tool name from `inbox.yaml` and those
three values, never item text. A run is never retried: a write is not safe to
repeat blindly.

Send and react (inbox 0.6.0, `run_send`, `run_react`). The same pattern as
RSVP, for two Slack writes through connector tools named in `inbox.yaml`. The
destination is never taken from item text: it is the item's `meta.reply_to`
(`reply_target`), validated by format. The pins are exactly the destination and
the text (send) or the target and the emoji name (react), the call carries
nothing else (`exact_args=True`), and a run is never retried (`attempts=1`).
Success is a real non-error tool result for the pinned call; a timeout, or a
run cut off by a sleep, is reported as "may have been sent, check Slack".

Save (`run_save`). The configured argv runs without a shell, with the item as
JSON on stdin and nothing else (item text is never put in argv). It is bound
by the same process-group, timeout and output-cap handling as a command
source, except that a timeout asks the group to stop (SIGTERM, then SIGKILL
after SAVE_TERM_GRACE_S) so the command can clean up.
"""
import json
import re
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
SAVE_TERM_GRACE_S = 5
SEND_USAGE_TAG = "inbox-send"
REACT_USAGE_TAG = "inbox-react"
SEND_TEXT_MAX = 4000
# The `meta.reply_to` contract: a Slack conversation id (public/private
# channel C, DM D, group G) and message timestamps.
CHANNEL_ID = re.compile(r"\A[CDG][A-Z0-9]{8,12}\Z")
SLACK_TS = re.compile(r"\A\d{9,11}\.\d{6}\Z")
EMOJI_NAME = re.compile(r"\A[a-z0-9_+\-]{1,64}\Z")

ReplyTo = namedtuple("ReplyTo", "channel thread_ts message_ts")
# `route` is the real destination (channel id and thread timestamp), shown in
# the confirm modal next to the `where` label.
Sendable = namedtuple("Sendable", "ok text target kind error destination route",
                      defaults=("", None, "", "", "", ""))
# Control characters (tab and newline are fine) and the bidi overrides,
# embeddings and isolates: text that could hide what it says. LRM, RLM and
# ALM are kept for Hebrew and Arabic.
_BAD_TEXT_CHARS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


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
                timeout=cfg["timeout_s"], attempts=1, exact_args=True,
                max_calls=1)
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
            stdin_bytes=payload, term_grace_s=SAVE_TERM_GRACE_S)
    except (OSError, ValueError, TypeError) as exc:
        return Outcome(False, f"cannot run: {getattr(exc, 'strerror', None) or exc}")
    if status == "timeout":
        return Outcome(False, f"timed out after {cfg['timeout_s']:g}s; "
                              "the save may still complete")
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


# --- Slack send and react (inbox 0.6.0) -------------------------------------

NO_REPLY_TO = ("this item has no valid meta.reply_to (a Slack channel id and "
               "timestamps), so it cannot be sent to")


def reply_to(meta):
    """The validated `meta.reply_to` as a `ReplyTo`, or None.

    `{channel, thread_ts?, message_ts?}`: `channel` is required and matches
    `^[CDG][A-Z0-9]{8,12}$`; `thread_ts` and `message_ts` are optional (absent
    or null) and match `^\\d{9,11}\\.\\d{6}$`. A present value in the wrong
    format makes the whole thing invalid; unknown keys are ignored and never
    used."""
    raw = meta.get("reply_to") if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        return None
    channel = raw.get("channel")
    if not isinstance(channel, str) or not CHANNEL_ID.match(channel):
        return None
    stamps = []
    for key in ("thread_ts", "message_ts"):
        value = raw.get(key)
        if value is None:
            stamps.append(None)
        elif isinstance(value, str) and SLACK_TS.match(value):
            stamps.append(value)
        else:
            return None
    return ReplyTo(channel, stamps[0], stamps[1])


def target_kind(target):
    """What a send to `target` is: a reply in a thread, a DM, or a post in a
    channel (outside a thread)."""
    if target.thread_ts:
        return "thread"
    return "DM" if target.channel.startswith("D") else "channel"


def send_text(draft):
    """`(text, error)` for a draft: only trailing whitespace is removed. The
    text is refused (`error`, text "") when it is empty, longer than
    SEND_TEXT_MAX characters (never cut: what is sent is what was shown) or
    holds a control or bidi-override character."""
    text = draft.rstrip() if isinstance(draft, str) else ""
    if not text:
        return "", "there is no text to send"
    if len(text) > SEND_TEXT_MAX:
        return "", (f"the text is {len(text)} characters; the limit is "
                    f"{SEND_TEXT_MAX}; edit it first")
    bad = _BAD_TEXT_CHARS.search(text)
    if bad:
        return "", (f"the text has a control or bidi-override character "
                    f"(U+{ord(bad.group(0)):04X}); remove it first")
    return text, ""


def send_plan(meta, draft, allow_channel_posts=False):
    """Everything a send needs, checked: the destination, the exact text and
    what to tell the user. `ok` False carries `error`.

    A reply to a channel or group (C, G) without a `thread_ts` would be a
    top-level post: it is refused unless `allow_channel_posts` (the setting
    `send.allow_channel_posts`). A DM (D) needs no thread."""
    target = reply_to(meta)
    if target is None:
        return Sendable(False, error=NO_REPLY_TO[0].upper() + NO_REPLY_TO[1:])
    if (not allow_channel_posts and not target.thread_ts
            and target.channel[0] in "CG"):
        return Sendable(False, error=(
            "This item would post to the channel, not a thread, so it is "
            "refused (send.allow_channel_posts: true allows channel posts)"))
    text, error = send_text(draft)
    if error:
        return Sendable(False, error=error[0].upper() + error[1:])
    route = target.channel + (f" · thread {target.thread_ts}"
                              if target.thread_ts else " · no thread")
    return Sendable(True, text, target, target_kind(target), route=route)


def send_pins(cfg, target, text):
    """`{tool: {param: value}}` for call_tools: exactly the channel and the
    text, plus the thread timestamp when the destination has one."""
    names = cfg["args"]
    pins = {names["channel"]: target.channel, names["text"]: text}
    if target.thread_ts:
        pins[names["thread_ts"]] = target.thread_ts
    return {cfg["tool"]: pins}


def react_target(meta):
    """`(channel, timestamp)` to react to, or None: `reply_to.message_ts`,
    else `reply_to.thread_ts`."""
    target = reply_to(meta)
    if target is None:
        return None
    ts = target.message_ts or target.thread_ts
    return (target.channel, ts) if ts else None


def valid_emoji(name):
    return isinstance(name, str) and bool(EMOJI_NAME.match(name))


def react_pins(cfg, channel, timestamp, emoji):
    names = cfg["args"]
    return {cfg["tool"]: {names["channel"]: channel,
                          names["timestamp"]: timestamp,
                          names["name"]: emoji}}


def pinned_prompt(cfg, pins):
    """One instruction: a single call with exactly the pinned values (the
    JSON keeps non-ASCII text as it is, so the model copies it verbatim)."""
    args = pins[cfg["tool"]]
    return (
        f"Call the tool {cfg['tool']} exactly once, with exactly these "
        "arguments and no others, each value copied verbatim:\n"
        f"{json.dumps(args, sort_keys=True, ensure_ascii=False)}\n"
        "Do not call any other tool, do not retry and do not change a "
        "value. When the call returns, reply with the single word done."
    )


_POSITIVE_TS = re.compile(r"\b\d{9,11}\.\d{6}\b")
_ERRORISH = re.compile(
    r"\b(errors?|failed|failure|denied|forbidden|unauthori[sz]ed|exception"
    r"|traceback|not[_ ]in[_ ]channel|channel[_ ]not[_ ]found|invalid[_ ]auth"
    r"|rate[_ ]?limited|missing[_ ]scope)\b", re.IGNORECASE)


def reply_state(text):
    """How much a write tool's reply confirms: `failed` (a JSON object with an
    error or ok/success false), `unconfirmed` (plain text that reads like an
    error and carries no Slack timestamp, the one generic sign of a posted
    message) or `ok`. Config-agnostic: a JSON object without a failure
    marker, and plain text without error words, stay `ok`."""
    if _tool_failure(text):
        return "failed"
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        doc = None
    if isinstance(doc, dict):
        return "ok"
    if (isinstance(text, str) and _ERRORISH.search(text)
            and not _POSITIVE_TS.search(text)):
        return "unconfirmed"
    return "ok"


def _run_pinned(cfg, base_dir, pins, tag, call_tools, what, failed):
    """One pinned write through the bridge. Never raises; returns an Outcome.

    `what` names the write ("message", "reaction") for the messages. The
    reasons carry only fixed kinds, never prompts or tool output. A failure
    that leaves it unknown whether the write happened (a timeout, a run cut
    off by a sleep, a crashed run) says "check Slack"."""
    call = call_tools or claude_bridge.call_tools
    tool = cfg["tool"]
    try:
        with claude_bridge.usage_scope(tag):
            results = call(
                pinned_prompt(cfg, pins), pins,
                model=cfg.get("model", "haiku"), base_dir=base_dir,
                max_turns=MAX_TURNS, max_budget_usd=MAX_BUDGET_USD,
                timeout=cfg["timeout_s"], attempts=1, exact_args=True,
                accept_partial=True, max_calls=1)
    except claude_bridge.GUARD_STOPS + (claude_bridge.BridgeDryRun,) as exc:
        return Outcome(False, "not sent: " + itm.truncate(
            itm.clean_line(exc), DETAIL_MAX))
    except claude_bridge.BridgeOutage:
        return Outcome(False, f"the run was cut off (sleep or outage); the "
                              f"{what} may have been sent, check Slack")
    except claude_bridge.BridgeError as exc:
        text = itm.truncate(itm.clean_line(exc), DETAIL_MAX)
        if "timed out" in text:
            return Outcome(False, f"timed out after {cfg['timeout_s']:g}s; "
                                  f"the {what} may have been sent, check Slack")
        if text.startswith("tool returned an error"):
            return Outcome(False, f"not sent: {text}")
        return Outcome(False, f"{text}; the {what} may have been sent, "
                              "check Slack")
    except Exception as exc:  # a missing claude binary, a bad config
        return Outcome(False, f"cannot run: {type(exc).__name__}")
    rows = [r for r in results
            if r.get("name") == tool and not r.get("is_error")
            and r.get("input") == pins[tool]]
    if not rows:
        return Outcome(False, f"no confirmed result for the pinned call; the "
                              f"{what} may not have been sent, check Slack")
    states = [reply_state(r.get("text")) for r in rows]
    if "ok" in states:
        # A real send happened: never say it did not.
        if any(s != "ok" for s in states):
            return Outcome(True, f"the {what} was sent at least once; "
                                 "check Slack")
        if len(rows) > 1:
            return Outcome(True, f"the tool ran {len(rows)} times; check Slack "
                                 f"for a duplicate {what}")
        return Outcome(True, "")
    if "failed" in states:
        return Outcome(False, f"the Slack tool reported a failure; {failed}")
    return Outcome(False, f"the tool's reply looks like an error; the {what} "
                          "may not have been sent; check Slack")


def run_send(cfg, base_dir, target, text, call_tools=None):
    """Post `text` to the destination `target` (a `ReplyTo`). Never raises;
    returns an Outcome (`detail` "" on a plain success)."""
    if not isinstance(target, ReplyTo) or not text or not text.strip():
        return Outcome(False, "nothing to send")
    pins = send_pins(cfg, target, text)
    return _run_pinned(cfg, base_dir, pins, SEND_USAGE_TAG, call_tools,
                       "message", "not sent")


def run_react(cfg, base_dir, channel, timestamp, emoji, call_tools=None):
    """Add the reaction `emoji` to the message at `timestamp`. Never raises;
    returns an Outcome."""
    if not (isinstance(channel, str) and CHANNEL_ID.match(channel)
            and isinstance(timestamp, str) and SLACK_TS.match(timestamp)
            and valid_emoji(emoji)):
        return Outcome(False, "invalid reaction target")
    pins = react_pins(cfg, channel, timestamp, emoji)
    return _run_pinned(cfg, base_dir, pins, REACT_USAGE_TAG, call_tools,
                       "reaction", "no reaction added")


# --- the optional send precheck ---------------------------------------------

PRECHECK_TERM_GRACE_S = 2
PRECHECK_WARN_MAX = 200
PRECHECK_MAX_BYTES = 64 * 1024
PRECHECK_FAILED = "couldn't check for a newer reply"
# warn: a line to show prominently in the confirm modal ("" = none);
# failed: the check itself did not work (the send is never blocked by it).
Precheck = namedtuple("Precheck", "warn failed", defaults=("", False))


def precheck_request(item, text):
    """The stdin of the precheck command: the item and the exact text."""
    return {"v": 1, "action": "precheck", "item": item, "text": text}


def parse_precheck(out):
    """The command's stdout as a `Precheck`: `{"v": 1, "ok": bool, "warn":
    str|null}`. `ok` true with a `warn` shows it; `ok` true without one
    shows nothing; anything else (not JSON, wrong `v`, `ok` not true) counts
    as a failed check."""
    try:
        doc = json.loads(out)
    except (TypeError, ValueError):
        return Precheck(failed=True)
    if (not isinstance(doc, dict) or doc.get("v") != 1
            or isinstance(doc.get("v"), bool) or doc.get("ok") is not True):
        return Precheck(failed=True)
    warn = doc.get("warn")
    if warn is None or warn is False:
        return Precheck()
    if not isinstance(warn, str):
        return Precheck(failed=True)
    return Precheck(itm.truncate(itm.clean_line(warn), PRECHECK_WARN_MAX))


def run_precheck(cfg, item, text, cancel=None):
    """Run the `send.precheck` command for one v1 item dict and the exact
    text (both on stdin only; nothing in argv). Never raises; any failure or
    timeout is `Precheck(failed=True)`. `cancel` (a `threading.Event`) stops
    the command."""
    pre = cfg.get("precheck")
    if pre is None:
        return Precheck()
    try:
        payload = json.dumps(precheck_request(item, text)).encode("utf-8")
        status, code, out, _err = sources._run_capped(
            sources._expand(pre["argv"]), pre["timeout_s"],
            stdin_bytes=payload, term_grace_s=PRECHECK_TERM_GRACE_S,
            max_bytes=PRECHECK_MAX_BYTES, cancel=cancel)
    except (OSError, ValueError, TypeError):
        return Precheck(failed=True)
    if status != "ok" or code != 0:
        return Precheck(failed=True)
    return parse_precheck(out.decode("utf-8", "replace"))


def suggested_reaction(meta):
    """`meta.suggested_reaction` when it is a valid emoji name, else None."""
    name = meta.get("suggested_reaction") if isinstance(meta, dict) else None
    return name if valid_emoji(name) else None
