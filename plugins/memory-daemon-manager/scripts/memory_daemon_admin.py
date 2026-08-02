#!/usr/bin/env python3
"""Guarded local administration for memory-daemon routines and connector prompts.

Every mutation is a two-command transaction:

1. ``plan`` prints a unified diff and a token bound to the exact before/after
   bytes.
2. ``apply`` recomputes that plan, rejects stale tokens, changes one file
   atomically, and rolls it back if ``daemon.py validate`` fails.

The tool never runs a routine, changes the processed ledger, touches captured
notes, writes memory entries, or starts the scheduler.
"""

import argparse
import difflib
import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MIN_PROMPT_BODY_CHARS = 40


class AdminError(Exception):
    """A safe, user-actionable administration error."""


@dataclass(frozen=True)
class ChangePlan:
    operation: str
    target: Path
    display_target: str
    before: Optional[bytes]
    after: Optional[bytes]
    before_mode: Optional[int]
    create_mode: int

    @property
    def after_mode(self):
        if self.after is None:
            return None
        return self.before_mode if self.before is not None else self.create_mode

    @property
    def token(self):
        payload = {
            "operation": self.operation,
            "target": str(self.target),
            "before": _digest(self.before),
            "after": _digest(self.after),
            "before_mode": self.before_mode,
            "after_mode": self.after_mode,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def diff(self):
        before = (self.before or b"").decode("utf-8").splitlines(keepends=True)
        after = (self.after or b"").decode("utf-8").splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{self.display_target}",
                tofile=f"b/{self.display_target}",
            )
        )


def _digest(value):
    if value is None:
        return None
    return hashlib.sha256(value).hexdigest()


def _require_name(value, label):
    if not NAME_RE.fullmatch(value or ""):
        raise AdminError(
            f"{label} must use lowercase letters, digits, and hyphens "
            f"(got {value!r})"
        )
    return value


def _require_posix():
    if os.name != "posix":
        raise AdminError(
            "memory-daemon administration supports POSIX systems only "
            "(macOS and Linux); the daemon depends on POSIX file locking"
        )


def _repo(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AdminError("--repo must be an absolute path")
    path = path.resolve()
    if not (path / "daemon.py").is_file() or not (path / "routines").is_dir():
        raise AdminError(f"{path} is not a memory-daemon checkout")
    return path


def _store(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AdminError("--store must be an absolute path")
    path = path.resolve()
    if not path.is_dir():
        raise AdminError(f"memory store does not exist: {path}")
    return path


def _read_utf8(path):
    try:
        return path.read_bytes()
    except OSError as exc:
        raise AdminError(f"cannot read {path}: {exc}") from exc


def _target_state(path):
    """Return regular-file bytes and mode, or (None, None) when absent."""
    if not os.path.lexists(path):
        return None, None
    if path.is_symlink() or not path.is_file():
        raise AdminError(f"refusing to mutate non-regular target: {path}")
    return _read_utf8(path), stat.S_IMODE(path.stat().st_mode)


def _yaml_mapping(raw, source):
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AdminError(f"{source} is not valid UTF-8 YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise AdminError(f"{source} must contain a YAML mapping")
    return data


def _routine_files(repo):
    return sorted(
        path for path in (repo / "routines").glob("*.yaml")
        if not path.name.startswith("_")
    )


def _discover_routines(repo):
    found = []
    ids = {}
    for path in _routine_files(repo):
        data = _yaml_mapping(_read_utf8(path), path)
        rid = data.get("id", path.stem)
        if not isinstance(rid, str) or not NAME_RE.fullmatch(rid):
            raise AdminError(
                f"{path.name}: routine id must use lowercase letters, digits, "
                f"and hyphens (got {rid!r})"
            )
        if rid in ids:
            raise AdminError(
                f"duplicate routine id {rid!r} in {ids[rid].name} and {path.name}"
            )
        ids[rid] = path
        found.append((path, data, rid))
    return found


def _routine_source_summary(source):
    kind = source.get("kind", "unknown")
    summary = {"kind": kind}
    if isinstance(source.get("handler"), str):
        summary["handler"] = source["handler"]
    if kind in {"gmail", "drive_docs"}:
        summary["query_configured"] = bool(source.get("query"))
    if kind == "slack":
        summary["channel_count"] = len(source.get("channels") or [])
        summary["include_mentions"] = bool(source.get("include_mentions"))
    if kind == "gchat":
        summary["space_count"] = len(source.get("spaces") or [])
    if source.get("hours") is not None:
        summary["hours"] = source["hours"]
    if source.get("max_results") is not None:
        summary["max_results"] = source["max_results"]
    actions = source.get("actions")
    if isinstance(actions, list):
        summary["actions"] = actions
    return summary


def _routine_summary(repo, path, routine, rid):
    source_list = routine.get("sources")
    if not isinstance(source_list, list):
        source_list = [routine.get("source")] if isinstance(routine.get("source"), dict) else []
    routine_actions = routine.get("actions")
    sinks = []
    if isinstance(routine.get("output"), dict):
        sinks.append("vault")
    if isinstance(routine.get("memory"), dict):
        sinks.append("memory")
    routing = routine.get("routing") if isinstance(routine.get("routing"), dict) else {}
    analyze = routine.get("analyze") if isinstance(routine.get("analyze"), dict) else {}
    return {
        "id": rid,
        "file": str(path.relative_to(repo)),
        "enabled": routine.get("enabled", True),
        "cadence": (routine.get("schedule") or {}).get("every", "4h")
        if isinstance(routine.get("schedule") or {}, dict)
        else "invalid",
        "routing": {
            "fallback": bool(routing.get("fallback", False)),
            "priority": routing.get("priority", 100),
        },
        "sources": [
            _routine_source_summary(source)
            for source in source_list
            if isinstance(source, dict)
        ],
        "routine_actions": routine_actions if isinstance(routine_actions, list) else [],
        "sinks": sinks,
        "prompt_source": (
            "inline" if analyze.get("instruction")
            else f"connector:{analyze.get('instruction_from_connector')}"
            if analyze.get("instruction_from_connector")
            else "missing"
        ),
    }


def routine_list(args):
    repo = _repo(args.repo)
    rows = [
        _routine_summary(repo, path, routine, rid)
        for path, routine, rid in _discover_routines(repo)
    ]
    _print_json({"routines": rows})


def _find_routine(repo, rid):
    matches = [
        (path, routine)
        for path, routine, found_id in _discover_routines(repo)
        if found_id == rid
    ]
    if not matches:
        raise AdminError(f"no routine with id {rid!r}")
    if len(matches) != 1:
        raise AdminError(f"routine id {rid!r} is not unique")
    return matches[0]


def routine_inspect(args):
    repo = _repo(args.repo)
    rid = _require_name(args.id, "routine id")
    path, routine = _find_routine(repo, rid)
    _print_json(_routine_summary(repo, path, routine, rid))


def _load_daemon_config(repo):
    repo_text = str(repo)
    if repo_text not in sys.path:
        sys.path.insert(0, repo_text)
    try:
        return importlib.import_module("workspace_daemon.config")
    except Exception as exc:
        raise AdminError(f"cannot load daemon configuration code from {repo}: {exc}") from exc


def _validate_routine_candidate(repo, rid, raw, target):
    data = _yaml_mapping(raw, target)
    candidate_id = data.get("id")
    if not isinstance(candidate_id, str) or not NAME_RE.fullmatch(candidate_id):
        raise AdminError(
            "candidate routine id must use lowercase letters, digits, and hyphens "
            f"(got {candidate_id!r})"
        )
    if candidate_id != rid:
        raise AdminError(
            f"candidate routine id {candidate_id!r} does not match target {rid!r}"
        )
    data["_source_file"] = str(target)
    config = _load_daemon_config(repo)
    problems = config.validate(data)
    if problems:
        raise AdminError("candidate routine is invalid:\n- " + "\n- ".join(problems))


def _routine_plan(args):
    repo = _repo(args.repo)
    rid = _require_name(args.id, "routine id")
    operation = args.operation
    existing = {
        found_id: (path, routine)
        for path, routine, found_id in _discover_routines(repo)
    }

    if operation == "add":
        if rid in existing:
            raise AdminError(f"routine {rid!r} already exists")
        target = repo / "routines" / f"{rid}.yaml"
        if os.path.lexists(target):
            raise AdminError(
                f"refusing to add routine {rid!r}: target path already exists "
                f"({target.name})"
            )
        before = None
        before_mode = None
    else:
        if rid not in existing:
            raise AdminError(f"no routine with id {rid!r}")
        target = existing[rid][0]
        before, before_mode = _target_state(target)

    after = None
    if operation != "remove":
        if not args.candidate:
            raise AdminError(f"--candidate is required for routine {operation}")
        candidate_path = Path(args.candidate).expanduser()
        after = _read_utf8(candidate_path)
        _validate_routine_candidate(repo, rid, after, target)
    elif args.candidate:
        raise AdminError("--candidate is not used for routine remove")

    return ChangePlan(
        operation=operation,
        target=target,
        display_target=str(target.relative_to(repo)),
        before=before,
        after=after,
        before_mode=before_mode,
        create_mode=0o644,
    )


def routine_plan(args):
    _print_plan(_routine_plan(args))


def routine_apply(args):
    plan = _routine_plan(args)
    repo = _repo(args.repo)
    _require_apply_token(plan, args.token)
    _require_remove_confirmation(plan, args.id, args.confirm_target)
    _apply_and_validate(plan, repo)


def _prompt_paths(store, name):
    return (
        store / "memory" / "connectors" / f"{name}.md",
        store / "connectors" / f"{name}.md",
    )


def _prompt_names(store):
    names = set()
    for directory in (store / "memory" / "connectors", store / "connectors"):
        if not directory.is_dir():
            continue
        names.update(path.stem for path in directory.glob("*.md") if NAME_RE.fullmatch(path.stem))
    return sorted(names)


def _prompt_summary(store, name):
    override, template = _prompt_paths(store, name)
    resolved = override if override.is_file() else template if template.is_file() else None
    return {
        "name": name,
        "override": override.is_file(),
        "template": template.is_file(),
        "resolved_origin": (
            "override" if resolved == override
            else "template" if resolved == template
            else "missing"
        ),
        "resolved_path": str(resolved.relative_to(store)) if resolved else None,
        "body_chars": len(_prompt_body(_read_utf8(resolved))) if resolved else 0,
    }


def prompt_list(args):
    store = _store(args.store)
    _print_json({"prompts": [_prompt_summary(store, name) for name in _prompt_names(store)]})


def prompt_inspect(args):
    store = _store(args.store)
    name = _require_name(args.name, "connector name")
    summary = _prompt_summary(store, name)
    if summary["resolved_origin"] == "missing":
        raise AdminError(f"no connector prompt named {name!r}")
    _print_json(summary)


def _prompt_body(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdminError(f"connector prompt is not valid UTF-8: {exc}") from exc
    if not text.startswith("---"):
        return text.strip()
    end = text.find("\n---", 3)
    return (text if end == -1 else text[end + 4 :]).strip()


def _validate_prompt_candidate(raw):
    body = _prompt_body(raw)
    if len(body) < MIN_PROMPT_BODY_CHARS:
        raise AdminError(
            f"connector prompt body is too short ({len(body)} chars); "
            f"write at least {MIN_PROMPT_BODY_CHARS} characters of source-wide guidance"
        )


def _prompt_plan(args):
    repo = _repo(args.repo)
    store = _store(args.store)
    name = _require_name(args.name, "connector name")
    operation = args.operation
    target, _ = _prompt_paths(store, name)
    exists = os.path.lexists(target)

    if operation == "add" and exists:
        raise AdminError(f"connector override {name!r} already exists; use edit")
    if operation in {"edit", "remove"} and not exists:
        raise AdminError(
            f"connector override {name!r} does not exist"
            + ("; use add to create an override of the template" if operation == "edit" else "")
        )

    before, before_mode = _target_state(target) if exists else (None, None)
    after = None
    if operation != "remove":
        if not args.candidate:
            raise AdminError(f"--candidate is required for prompt {operation}")
        after = _read_utf8(Path(args.candidate).expanduser())
        _validate_prompt_candidate(after)
    elif args.candidate:
        raise AdminError("--candidate is not used for prompt remove")

    return ChangePlan(
        operation=operation,
        target=target,
        display_target=str(target.relative_to(store)),
        before=before,
        after=after,
        before_mode=before_mode,
        create_mode=0o600,
    ), repo


def prompt_plan(args):
    plan, _ = _prompt_plan(args)
    _print_plan(plan)


def prompt_apply(args):
    plan, repo = _prompt_plan(args)
    _require_apply_token(plan, args.token)
    _require_remove_confirmation(plan, args.name, args.confirm_target)
    _apply_and_validate(plan, repo)


def _require_apply_token(plan, supplied):
    if supplied != plan.token:
        raise AdminError(
            "plan token does not match the current file and candidate; "
            "run plan again and review the new diff"
        )


def _require_remove_confirmation(plan, expected, supplied):
    if plan.operation == "remove" and supplied != expected:
        raise AdminError(
            f"remove requires --confirm-target {expected!r} for the exact target"
        )


def _atomic_write(path, content, mode, expected_content, expected_mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        # Recheck after the temporary file is fully durable, immediately before
        # replacement. This keeps slow writes from widening the stale-plan race.
        actual_content, actual_mode = _target_state(path)
        if actual_content != expected_content or actual_mode != expected_mode:
            raise AdminError(f"target changed concurrently: {path}")
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_create(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as exc:
        raise AdminError(f"target appeared concurrently: {path}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(path.parent)


def _fsync_directory(path):
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def _set_plan_state(plan, content, mode, expected_content, expected_mode):
    actual_content, actual_mode = _target_state(plan.target)
    if actual_content != expected_content or actual_mode != expected_mode:
        raise AdminError(
            f"target changed concurrently: {plan.display_target}; "
            "the newer content was preserved"
        )

    if content is None:
        plan.target.unlink()
        _fsync_directory(plan.target.parent)
        return
    if expected_content is None:
        _atomic_create(plan.target, content, mode)
    else:
        _atomic_write(
            plan.target,
            content,
            mode=mode,
            expected_content=expected_content,
            expected_mode=expected_mode,
        )


def _preserve_conflict_copy(plan):
    if plan.before is None:
        return None
    plan.target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{plan.target.name}.rollback-conflict-",
        dir=str(plan.target.parent),
    )
    path = Path(name)
    with os.fdopen(fd, "wb") as handle:
        handle.write(plan.before)
        handle.flush()
        os.fsync(handle.fileno())
    # mkstemp deliberately keeps this recovery copy private (0600), even when
    # the original routine was world-readable.
    _fsync_directory(path.parent)
    return path


def _validate_checkout(repo):
    try:
        result = subprocess.run(
            [sys.executable, str(repo / "daemon.py"), "validate"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdminError(f"could not run daemon validator: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "validator failed").strip()
        raise AdminError(f"daemon validation failed:\n{detail}")


def _apply_and_validate(plan, repo):
    _set_plan_state(
        plan,
        plan.after,
        plan.after_mode,
        expected_content=plan.before,
        expected_mode=plan.before_mode,
    )
    try:
        _validate_checkout(repo)
    except Exception as validation_error:
        try:
            _set_plan_state(
                plan,
                plan.before,
                plan.before_mode,
                expected_content=plan.after,
                expected_mode=plan.after_mode,
            )
        except Exception as rollback_error:
            conflict = _preserve_conflict_copy(plan)
            recovery = (
                f"; original saved to {conflict}"
                if conflict is not None
                else "; there was no original file to save"
            )
            raise AdminError(
                f"{validation_error}\nrollback stopped because {rollback_error}"
                f"{recovery}"
            ) from validation_error
        raise
    _print_json(
        {
            "status": "applied",
            "operation": plan.operation,
            "target": plan.display_target,
            "validation": "ok",
            "preserved": ["processed ledger", "captured notes", "memory entries"],
        }
    )


def _print_plan(plan):
    print(f"operation: {plan.operation}")
    print(f"target: {plan.display_target}")
    print(f"plan-token: {plan.token}")
    print("diff:")
    print(plan.diff() or "(no changes)")


def _print_json(value):
    print(json.dumps(value, indent=2, sort_keys=True))


def _add_routine_commands(subparsers):
    routine = subparsers.add_parser("routine", help="manage routine YAML files")
    commands = routine.add_subparsers(dest="routine_command", required=True)

    list_parser = commands.add_parser("list", help="list redacted routine summaries")
    list_parser.add_argument("--repo", required=True)
    list_parser.set_defaults(func=routine_list)

    inspect = commands.add_parser("inspect", help="inspect one redacted routine summary")
    inspect.add_argument("--repo", required=True)
    inspect.add_argument("--id", required=True)
    inspect.set_defaults(func=routine_inspect)

    for command, func in (("plan", routine_plan), ("apply", routine_apply)):
        parser = commands.add_parser(command, help=f"{command} one routine change")
        parser.add_argument("--repo", required=True)
        parser.add_argument("--operation", choices=("add", "edit", "remove"), required=True)
        parser.add_argument("--id", required=True)
        parser.add_argument("--candidate")
        if command == "apply":
            parser.add_argument("--token", required=True)
            parser.add_argument("--confirm-target")
        parser.set_defaults(func=func)


def _add_prompt_commands(subparsers):
    prompt = subparsers.add_parser("prompt", help="manage private connector overrides")
    commands = prompt.add_subparsers(dest="prompt_command", required=True)

    list_parser = commands.add_parser("list", help="list prompt origins without bodies")
    list_parser.add_argument("--store", required=True)
    list_parser.set_defaults(func=prompt_list)

    inspect = commands.add_parser("inspect", help="inspect prompt metadata without its body")
    inspect.add_argument("--store", required=True)
    inspect.add_argument("--name", required=True)
    inspect.set_defaults(func=prompt_inspect)

    for command, func in (("plan", prompt_plan), ("apply", prompt_apply)):
        parser = commands.add_parser(command, help=f"{command} one prompt override change")
        parser.add_argument("--repo", required=True)
        parser.add_argument("--store", required=True)
        parser.add_argument("--operation", choices=("add", "edit", "remove"), required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--candidate")
        if command == "apply":
            parser.add_argument("--token", required=True)
            parser.add_argument("--confirm-target")
        parser.set_defaults(func=func)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="resource", required=True)
    _add_routine_commands(subparsers)
    _add_prompt_commands(subparsers)
    args = parser.parse_args(argv)
    try:
        _require_posix()
        args.func(args)
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
