"""Read-only, tolerant peeks at the daemon's own routine configuration.

The inbox never changes routines. It only needs two facts that the daemon
keeps there: which calendar the calendar cache reads (and where its JSON twin
lives), and which Gmail routines require a label in their search query. A
missing, oversized, unparseable or oddly shaped file is skipped; nothing here
raises. Routine files are private config: their contents are never logged.
"""
import re
from pathlib import Path

import yaml

MAX_FILES = 200
MAX_FILE_BYTES = 1024 * 1024
DEFAULT_CALENDAR_ID = "primary"
# A positive `label:` term. The lookbehind rejects `-label:x` (a negation) and
# anything glued to a word, e.g. `xlabel:x`.
_LABEL_TERM = re.compile(r'(?<![-\w])label:("[^"]*"|\S+)', re.IGNORECASE)
# OR, braces and parentheses make a query a union or a grouping, where one
# `label:` term no longer guarantees the label on every listed thread.
_NOT_A_CONJUNCTION = re.compile(r"\bOR\b|[{}()]")


def routines(base_dir):
    """Every parseable `<base>/routines/*.yaml` mapping (templates skipped),
    each with a string `id` (the file stem when the file has none)."""
    out = []
    try:
        paths = sorted(p for p in (Path(base_dir) / "routines").glob("*.yaml")
                       if not p.name.startswith("_"))[:MAX_FILES]
    except OSError:
        return out
    for path in paths:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        rid = data.get("id")
        data = dict(data, id=rid if isinstance(rid, str) and rid else path.stem)
        out.append(data)
    return out


def _source_blocks(routine):
    blocks = routine.get("sources")
    if isinstance(blocks, list):
        return [b for b in blocks if isinstance(b, dict)]
    block = routine.get("source")
    return [block] if isinstance(block, dict) else []


def calendar_cache(base_dir):
    """`{"calendar_id", "json"}` of the calendar-cache routine, or defaults.

    `json` is the path of the cache's JSON twin (the routine's `output` with
    a `.json` suffix) or None. The first routine with
    `maintenance.kind: calendar_cache` wins.
    """
    found = {"calendar_id": DEFAULT_CALENDAR_ID, "json": None}
    for routine in routines(base_dir):
        cfg = routine.get("maintenance")
        if not isinstance(cfg, dict) or cfg.get("kind") != "calendar_cache":
            continue
        cal = cfg.get("calendar_id")
        if isinstance(cal, str) and cal.strip():
            found["calendar_id"] = cal.strip()
        output = cfg.get("output")
        if isinstance(output, str) and output.strip():
            try:
                found["json"] = Path(output).expanduser().with_suffix(".json")
            except (RuntimeError, ValueError):
                pass
        break
    return found


def query_requires_label(query, label):
    """True when the search `query` is a plain conjunction that includes a
    positive `label:<label>` term (case-insensitive; a name with spaces may
    be written with hyphens, as Gmail does)."""
    if not isinstance(query, str) or not isinstance(label, str):
        return False
    label = label.strip()
    if not label or _NOT_A_CONJUNCTION.search(query):
        return False
    wanted = {label.casefold(), label.casefold().replace(" ", "-")}
    for match in _LABEL_TERM.finditer(query):
        value = match.group(1).strip('"').strip().casefold()
        if value in wanted:
            return True
    return False


def label_routines(base_dir, label):
    """Ids of the Gmail routines whose search query requires `label`.

    Every thread such a routine captured matched that query, so it carried the
    label when it was captured. This is the only place the daemon records the
    label: the event stream itself does not.
    """
    ids = set()
    for routine in routines(base_dir):
        for block in _source_blocks(routine):
            if (block.get("kind") == "gmail_mcp"
                    and query_requires_label(block.get("query"), label)):
                ids.add(routine["id"])
    return ids
