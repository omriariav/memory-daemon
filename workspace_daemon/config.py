"""Routine discovery, loading, and validation."""
from pathlib import Path

import yaml

from .actions import VALID_ACTIONS  # single source of truth for action names

REQUIRED_TOP_LEVEL = ["id", "source", "analyze", "output"]


class RoutineError(Exception):
    pass


def routines_dir(base_dir):
    return Path(base_dir) / "routines"


def discover(base_dir, routine_id=None):
    """Load every routines/*.yaml (files starting with _ are templates, skipped).

    A malformed file raises RoutineError naming the file, never a bare YAML traceback.
    """
    found = []
    for path in sorted(routines_dir(base_dir).glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise RoutineError(f"{path.name}: invalid YAML — {exc}") from exc
        if not data:
            continue
        if not isinstance(data, dict):
            raise RoutineError(f"{path.name}: top level must be a mapping, got {type(data).__name__}")
        data["_source_file"] = str(path)
        data.setdefault("id", path.stem)
        found.append(data)

    seen = {}
    for routine in found:
        rid = routine["id"]
        if rid in seen:
            raise RoutineError(
                f"duplicate routine id '{rid}' in {Path(seen[rid]).name} "
                f"and {Path(routine['_source_file']).name}"
            )
        seen[rid] = routine["_source_file"]

    if routine_id:
        found = [r for r in found if r["id"] == routine_id]
        if not found:
            raise RoutineError(f"no routine with id '{routine_id}'")
    return found


def validate(routine):
    """Return a list of human-readable problems; empty means valid."""
    problems = []
    rid = routine.get("id", "<missing id>")

    for key in REQUIRED_TOP_LEVEL:
        if key not in routine:
            problems.append(f"{rid}: missing required top-level key '{key}'")

    source = routine.get("source", {})
    if source.get("kind") != "gmail":
        problems.append(f"{rid}: source.kind must be 'gmail' (got {source.get('kind')!r})")
    if not source.get("query"):
        problems.append(f"{rid}: source.query is required")

    max_results = source.get("max_results", 20)
    if not isinstance(max_results, int) or max_results < 1:
        problems.append(f"{rid}: source.max_results must be a positive integer")

    analyze = routine.get("analyze", {})
    for key in ("provider", "model", "instruction"):
        if not analyze.get(key):
            problems.append(f"{rid}: analyze.{key} is required")
    tokens = analyze.get("max_output_tokens", 4096)
    if not isinstance(tokens, int) or tokens < 1:
        problems.append(f"{rid}: analyze.max_output_tokens must be a positive integer")
    elif tokens < 2048:
        problems.append(
            f"{rid}: analyze.max_output_tokens={tokens} is too low — reasoning models "
            f"truncate mid-sentence below ~2048; use 4096"
        )
    domains = analyze.get("focus_domains")
    if domains is not None and not isinstance(domains, list):
        problems.append(f"{rid}: analyze.focus_domains must be a list")

    output = routine.get("output", {})
    if not output.get("vault_dir"):
        problems.append(f"{rid}: output.vault_dir is required")
    elif not str(output["vault_dir"]).startswith("/"):
        problems.append(f"{rid}: output.vault_dir must be an absolute path")
    if not output.get("slug_prefix"):
        problems.append(f"{rid}: output.slug_prefix is required")

    for action in routine.get("actions", []):
        if action not in VALID_ACTIONS:
            problems.append(
                f"{rid}: unknown action '{action}' (valid: {', '.join(sorted(VALID_ACTIONS))})"
            )

    if "apply_label" in routine.get("actions", []) and not analyze.get("pick_label"):
        problems.append(
            f"{rid}: action 'apply_label' requires analyze.pick_label: true"
        )

    return problems
