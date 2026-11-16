"""PreToolUse hook enforcing the headless Claude bridge allowlist.

Runs as a standalone script (`python -I claude_bridge_hook.py <allowlist.json>`)
so it must stay stdlib-only with no package imports. Claude Code pipes the tool
call as JSON on stdin; this prints a PreToolUse permission decision. Anything
unexpected denies: a hook that errors open would defeat the allowlist.
"""
import json
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
        allowed, reason = decide(json.load(stdin), tools, exact_args)
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
