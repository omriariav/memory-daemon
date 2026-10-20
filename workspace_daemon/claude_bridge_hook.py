"""PreToolUse hook enforcing the headless Claude bridge allowlist.

Runs as a standalone script (`python -I claude_bridge_hook.py <allowlist.json>`)
so it must stay stdlib-only with no package imports. Claude Code pipes the tool
call as JSON on stdin; this prints a PreToolUse permission decision. Anything
unexpected denies: a hook that errors open would defeat the allowlist.
"""
import json
import sys

SELECT_PREFIX = "select:"


def decide(payload, tools):
    """Return (allowed, reason) for one tool call against {name: pins}.

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
        if key not in tool_input or tool_input[key] != value:
            return False, f"tool {name} argument {key} does not match its pin"
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
            tools = json.load(handle)["tools"]
        allowed, reason = decide(json.load(stdin), tools)
    except Exception:  # fail closed on anything, including a bad allowlist
        _emit(False, "bridge hook error")
        return 2
    _emit(allowed, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin))
