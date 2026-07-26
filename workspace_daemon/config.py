"""Routine discovery, loading, and validation."""
import re
from pathlib import Path
from string import Formatter

import yaml

from .actions import VALID_ACTIONS  # single source of truth for action names
from .notes import FILENAME_FIELDS

REQUIRED_TOP_LEVEL = ["id", "source", "analyze", "output"]
VALID_SOURCE_KINDS = {"gmail", "drive_docs"}


def analyze_cfg(routine):
    cfg = routine.get("analyze")
    return cfg if isinstance(cfg, dict) else {}


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
    kind = source.get("kind")
    if kind not in VALID_SOURCE_KINDS:
        problems.append(
            f"{rid}: source.kind must be one of {', '.join(sorted(VALID_SOURCE_KINDS))} "
            f"(got {kind!r})"
        )
    if not source.get("query"):
        problems.append(f"{rid}: source.query is required")

    expand = source.get("expand")
    if expand is not None:
        if kind != "gmail":
            problems.append(f"{rid}: source.expand is only supported for source.kind 'gmail'")
        if expand.get("kind") != "drive_doc":
            problems.append(
                f"{rid}: source.expand.kind must be 'drive_doc' (got {expand.get('kind')!r})"
            )
        pattern = expand.get("title_from_subject")
        if not pattern:
            problems.append(f"{rid}: source.expand.title_from_subject is required")
        else:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                problems.append(f"{rid}: source.expand.title_from_subject is not valid regex — {exc}")
            else:
                if "title" not in compiled.groupindex:
                    problems.append(
                        f"{rid}: source.expand.title_from_subject must contain a named "
                        f"group (?P<title>...) — that is what gets matched against Drive"
                    )
        tabs = expand.get("tabs")
        if tabs is not None and (not isinstance(tabs, list) or not tabs):
            problems.append(f"{rid}: source.expand.tabs must be a non-empty list of tab titles")
        on_missing = expand.get("on_missing", "body")
        if on_missing not in ("body", "error"):
            problems.append(
                f"{rid}: source.expand.on_missing must be 'body' or 'error' (got {on_missing!r})"
            )

    if kind == "drive_docs":
        tabs = source.get("tabs")
        if tabs is not None and (not isinstance(tabs, list) or not tabs):
            problems.append(f"{rid}: source.tabs must be a non-empty list of tab titles")
        # Gmail triage has no meaning for a Drive file, and the label catalog
        # the model would pick from is the mailbox's.
        if routine.get("actions"):
            problems.append(
                f"{rid}: source.kind 'drive_docs' does not support actions "
                f"(got {routine['actions']}) — use actions: []"
            )
        if analyze_cfg(routine).get("pick_label"):
            problems.append(
                f"{rid}: analyze.pick_label requires source.kind 'gmail' — "
                f"there is no Gmail message to label"
            )

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

    template = output.get("filename_template")
    if template is not None:
        allowed = FILENAME_FIELDS
        try:
            fields = {f for _, f, _, _ in Formatter().parse(template) if f}
        except ValueError as exc:
            problems.append(f"{rid}: output.filename_template is malformed — {exc}")
        else:
            unknown = fields - allowed
            if unknown:
                problems.append(
                    f"{rid}: output.filename_template has unknown placeholder(s) "
                    f"{', '.join('{%s}' % u for u in sorted(unknown))} "
                    f"(valid: {', '.join('{%s}' % a for a in sorted(allowed))})"
                )
            if not fields:
                problems.append(
                    f"{rid}: output.filename_template has no placeholders — "
                    f"every note would overwrite the same filename"
                )

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
