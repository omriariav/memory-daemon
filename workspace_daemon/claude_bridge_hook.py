"""PreToolUse hook enforcing the headless Claude bridge allowlist.

Runs as a standalone script (`python -I claude_bridge_hook.py <allowlist.json>`)
so it must stay stdlib-only with no package imports. Claude Code pipes the tool
call as JSON on stdin; this prints a PreToolUse permission decision. Anything
unexpected denies: a hook that errors open would defeat the allowlist.
"""
import hashlib
import json
import os
import sys

SELECT_PREFIX = "select:"


def _same(left, right):
    """Exact JSON equality: True != 1 and 1 != 1.0 (Python == would pass both)."""
    try:
        return (json.dumps(left, sort_keys=True)
                == json.dumps(right, sort_keys=True))
    except (TypeError, ValueError):
        return False


def decide(payload, tools, exact_args=False):
    """Return (allowed, reason) for one tool call against {name: pins}.

    By default a call must carry every pinned argument with its exact value;
    extra arguments are allowed. With `exact_args` the call must carry exactly
    the pinned arguments and nothing else (for writes, where an extra argument
    such as a notification flag would change what the call does).

    Raises ValueError on malformed input; main() turns that into a deny.
    """
    if not isinstance(payload, dict) or not isinstance(tools, dict):
        raise ValueError("malformed hook input")
    name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(name, str) or not isinstance(tool_input, dict):
        raise ValueError("malformed hook input")

    if name == "ToolSearch":
        query = tool_input.get("query")
        if not isinstance(query, str) or not query.startswith(SELECT_PREFIX):
            return False, "ToolSearch is limited to select: queries"
        wanted = [part.strip() for part in query[len(SELECT_PREFIX):].split(",")]
        if not wanted or any(not part or part not in tools for part in wanted):
            return False, "ToolSearch selected a tool outside the allowlist"
        return True, "allowlisted tool search"

    pins = tools.get(name)
    if pins is None:
        return False, f"tool {name} is not allowlisted"
    if not isinstance(pins, dict):
        raise ValueError("malformed allowlist")
    for key, value in pins.items():
        if key not in tool_input or not _same(tool_input[key], value):
            return False, f"tool {name} argument {key} does not match its pin"
    if exact_args and set(tool_input) != set(pins):
        return False, f"tool {name} has arguments outside its pins"
    return True, "allowlisted tool call"


def _claim_call(run_dir, name, limit):
    """True when this call of `name` is within `limit` allowed calls in this
    run. Each allowed call claims one marker file with O_EXCL in the 0700 run
    directory, so separate hook processes (parallel tool calls) cannot both
    take the last slot. Raises OSError on anything unexpected (the caller
    denies)."""
    claims = os.path.join(run_dir, "calls")
    os.makedirs(claims, mode=0o700, exist_ok=True)
    tag = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    for slot in range(limit):
        try:
            fd = os.open(os.path.join(claims, f"{tag}-{slot}"),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return True
    return False


def _emit(allowed, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason": reason,
    }}))


def main(argv, stdin):
    try:
        with open(argv[1], encoding="utf-8") as handle:
            doc = json.load(handle)
        tools = doc["tools"]
        exact_args = doc.get("exact_args", False) is True
        max_calls = doc.get("max_calls")
        if max_calls is not None and (not isinstance(max_calls, int)
                                      or isinstance(max_calls, bool)
                                      or max_calls < 1):
            raise ValueError("malformed allowlist")
        payload = json.load(stdin)
        allowed, reason = decide(payload, tools, exact_args)
        if (allowed and max_calls is not None
                and payload["tool_name"] in tools
                and not _claim_call(os.path.dirname(os.path.abspath(argv[1])),
                                    payload["tool_name"], max_calls)):
            allowed, reason = False, (
                f"tool {payload['tool_name']} may be called at most "
                f"{max_calls} time(s) in this run")
        _emit(allowed, reason)
        return 0
    except Exception:  # fail closed on anything, including a bad allowlist
        try:
            _emit(False, "bridge hook error")
        except Exception:
            pass  # exit 2 still blocks the call
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin))
