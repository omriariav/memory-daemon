"""Headless Claude Code bridge: least-privilege `claude -p` with real tool results.

Two entry points share one runner. `call_tools` lets the model call a pinned set
of MCP connector tools and returns the tool results parsed from the stream, never
the model's own restatement (models hallucinate tool output as plain text).
`complete` is a no-tools completion used as an LLM provider.

Enforcement is layered: a PreToolUse hook (claude_bridge_hook.py) allows only the
allowlisted calls, risky built-ins are disallowed, and the stream is re-checked
here so any off-allowlist call, permission denial, or unknown built-in fails the
run closed. BridgeError messages carry only kinds, subtypes, exit codes, tool
names and counts; prompts, tool inputs, results and CLI output never appear in
errors or logs.
"""
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .shell import claude_bin, utc_now_iso

HOOK_PATH = Path(__file__).with_name("claude_bridge_hook.py")

# Built-ins removed so only ToolSearch, Read, Glob and Grep stay visible. The
# hook denies Read/Glob/Grep too; this list is the first layer, not the only one.
DEFAULT_DISALLOWED_TOOLS = (
    "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Agent,CronCreate,"
    "CronDelete,CronList,RemoteTrigger,DesignSync,PushNotification,Monitor,"
    "EnterWorktree,ExitWorktree,SendMessage,ListAgents,Workflow,PublishPlugin,"
    "ShareOnboardingGuide,ScheduleWakeup,ReportFindings,TaskStop,"
    # Generic MCP resource readers appear once connectors attach; they could
    # read any connector's resources, so they are removed too.
    "ListMcpResourcesTool,ReadMcpResourceDirTool,ReadMcpResourceTool"
)
# A built-in outside this set showing up in init means a new CLI version added
# one; refuse rather than run with an unreviewed capability.
ALLOWED_BUILTINS = {"ToolSearch", "Read", "Glob", "Grep"}
SEARCH_TOOL = "ToolSearch"
# claude.ai connectors attach a few seconds after the CLI starts. A prompt sent
# before that often finds no deferred tools and the model gives up, so the
# prompt is held back this long (stream-json input keeps stdin open meanwhile).
CONNECT_DELAY_SECONDS = 6.0


class BridgeError(RuntimeError):
    pass


class BridgeDryRun(BridgeError):
    """Raised instead of spawning claude when a caller asks for dry_run."""


def _runs_dir(base_dir):
    return Path(base_dir) / "state" / "claude-runs"


def _usage_log(base_dir):
    return Path(base_dir) / "state" / "claude-usage.jsonl"


def _hook_command(allowlist_path):
    return shlex.join([sys.executable, "-I", str(HOOK_PATH), str(allowlist_path)])


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


def _build_command(model, settings_path, allowed, max_turns, max_budget_usd,
                   disallowed):
    return [
        claude_bin(), "-p",
        "--model", model,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--setting-sources", "",
        "--settings", str(settings_path),
        "--disallowedTools", disallowed,
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--max-turns", str(max_turns),
        "--max-budget-usd", str(max_budget_usd),
        "--allowedTools", ",".join(allowed),
    ]


def _spawn(cmd, prompt, cwd, timeout, connect_delay):
    """Run claude with the prompt on stdin; kill its whole process group on timeout.

    The prompt is one stream-json user message, written after `connect_delay`
    seconds so connector tools are discoverable when the model starts.
    """
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=cwd, start_new_session=True,
    )
    message = json.dumps(
        {"type": "user", "message": {"role": "user", "content": prompt}}
    )
    try:
        if connect_delay:
            time.sleep(connect_delay)
        try:
            proc.stdin.write(message + "\n")
        except BrokenPipeError:
            pass  # exited early; the exit code and stream report why
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        raise BridgeError(f"claude run timed out after {timeout}s") from None
    return proc.returncode, stdout


def _parse_stream(stdout):
    """Return (events, ok); ok is False if any line is not a JSON object."""
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return events, False
        if not isinstance(event, dict):
            return events, False
        events.append(event)
    return events, True


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def _content_blocks(event):
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _log_usage(base_dir, model, names, attempt, outcome, started, result):
    """Best-effort metadata line; never the prompt or any tool output."""
    usage = (result or {}).get("usage")
    usage = usage if isinstance(usage, dict) else {}
    entry = {
        "ts": utc_now_iso(),
        "model": model,
        "tools": sorted(names),
        "attempt": attempt,
        "outcome": outcome,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "total_cost_usd": (result or {}).get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }
    try:
        path = _usage_log(base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        return


def _attempt(prompt, tools, *, model, base_dir, max_turns, max_budget_usd,
             timeout, attempt, disallowed, connect_delay):
    """One claude run. Returns (results, final result event); raises BridgeError."""
    started = time.monotonic()
    result_event = None
    outcome = "error"
    runs = _runs_dir(base_dir)
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
    try:
        os.chmod(run_dir, 0o700)
        allowlist = run_dir / "allowlist.json"
        settings = run_dir / "settings.json"
        _write_private(allowlist, json.dumps({"tools": tools}))
        _write_private(settings, json.dumps({"hooks": {"PreToolUse": [{
            "matcher": ".*",
            "hooks": [{"type": "command", "command": _hook_command(allowlist)}],
        }]}}))
        allowed = ([SEARCH_TOOL] + sorted(tools)) if tools else []
        cmd = _build_command(
            model, settings, allowed, max_turns, max_budget_usd, disallowed
        )
        returncode, stdout = _spawn(cmd, prompt, run_dir, timeout, connect_delay)
        events, parsed_ok = _parse_stream(stdout)
        result_event = next(
            (e for e in reversed(events) if e.get("type") == "result"), None
        )
        if returncode != 0:
            raise BridgeError(f"claude exited with code {returncode}")
        if not parsed_ok:
            raise BridgeError("unparseable stream line from claude")
        if result_event is None:
            raise BridgeError("claude stream had no final result event")
        subtype = result_event.get("subtype")
        if subtype != "success" or result_event.get("is_error"):
            raise BridgeError(f"claude run failed: result subtype {subtype}")
        if result_event.get("permission_denials"):
            denied = len(result_event["permission_denials"])
            raise BridgeError(f"claude run had {denied} permission denial(s)")

        issued = {}  # tool_use id -> (name, input), only for calls actually made
        results = []
        for event in events:
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                extra = sorted(
                    t for t in event.get("tools") or []
                    if isinstance(t, str) and not t.startswith("mcp__")
                    and t not in ALLOWED_BUILTINS
                )
                if extra:
                    raise BridgeError(
                        f"unexpected built-in tool(s) visible: {', '.join(extra)}"
                    )
            elif kind == "assistant":
                for block in _content_blocks(event):
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name")
                    if name == SEARCH_TOOL and tools:
                        pass
                    elif name not in tools:
                        raise BridgeError(f"tool call outside allowlist: {name}")
                    tool_input = block.get("input")
                    issued[block.get("id")] = (
                        name, tool_input if isinstance(tool_input, dict) else {}
                    )
            elif kind == "user":
                for block in _content_blocks(event):
                    if block.get("type") != "tool_result":
                        continue
                    name, tool_input = issued.get(
                        block.get("tool_use_id"), (None, None)
                    )
                    if name is None or name == SEARCH_TOOL:
                        continue
                    if block.get("is_error"):
                        raise BridgeError(f"tool returned an error: {name}")
                    results.append({
                        "name": name,
                        "input": tool_input,
                        "text": _result_text(block.get("content")),
                        "is_error": False,
                    })
        outcome = "ok" if all(any(r["name"] == n for r in results) for n in tools) \
            else "missing_results"
        return results, result_event
    except BridgeError as exc:
        outcome = "error: " + str(exc)
        raise
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        _log_usage(base_dir, model, tools, attempt, outcome, started, result_event)


def call_tools(prompt, tools, *, model="haiku", base_dir, max_turns=6,
               max_budget_usd=0.25, timeout=180, attempts=3, dry_run=False,
               disallowed_tools=DEFAULT_DISALLOWED_TOOLS,
               connect_delay=CONNECT_DELAY_SECONDS):
    """Let headless Claude call the allowlisted MCP tools; return their real results.

    `tools` maps exact MCP tool name -> dict of pinned argument values (exact
    equality; {} pins nothing). Returns [{"name", "input", "text", "is_error"}]
    for every real tool_result of an allowlisted tool, in order. Results are
    usually human-readable text, not JSON; callers parse.

    dry_run: the runner already skips LLM/connector work under --dry-run, so
    this refuses loudly with BridgeDryRun (a BridgeError) instead of returning
    an empty list a caller could mistake for "connector had nothing".
    """
    if not tools:
        raise BridgeError("call_tools needs at least one tool")
    if dry_run:
        raise BridgeDryRun("dry-run: claude bridge not invoked")
    instruction = (
        f'First call ToolSearch with query "select:{",".join(tools)}".'
    )
    full_prompt = f"{instruction}\n\n{prompt}"
    for attempt in range(1, attempts + 1):
        results, _ = _attempt(
            full_prompt, dict(tools), model=model, base_dir=base_dir,
            max_turns=max_turns, max_budget_usd=max_budget_usd,
            timeout=timeout, attempt=attempt, disallowed=disallowed_tools,
            connect_delay=connect_delay,
        )
        if all(any(r["name"] == name for r in results) for name in tools):
            return results
    raise BridgeError(
        f"required tool results missing after {attempts} attempt(s): "
        f"{', '.join(tools)}"
    )


def complete(prompt, *, model, base_dir, max_turns=1, max_budget_usd=0.25,
             timeout=300, disallowed_tools=DEFAULT_DISALLOWED_TOOLS):
    """No-tools completion: returns the model's final text, fails on any tool use."""
    _, result_event = _attempt(
        prompt, {}, model=model, base_dir=base_dir, max_turns=max_turns,
        max_budget_usd=max_budget_usd, timeout=timeout, attempt=1,
        disallowed=disallowed_tools, connect_delay=0,
    )
    text = result_event.get("result")
    if not isinstance(text, str) or not text.strip():
        raise BridgeError("claude run returned no text")
    return text
