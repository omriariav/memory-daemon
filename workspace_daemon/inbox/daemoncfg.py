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
# One query token. A positive `label:` term comes first (a quoted value right
# after it is kept whole); then any word with a quoted phrase attached
# (`subject:"label:x"`, `-label:"x"`) is one inert token; then a bare word.
_TOKEN = re.compile(
    r'(?P<label>(?<![-\w])label:(?:"[^"]*"|\S+))|(?P<quoted>[^\s"]*"[^"]*"?)|\S+',
    re.IGNORECASE)
# OR, NOT, `|`, braces and parentheses make a query a union, a negation or a
# grouping, where one `label:` term no longer guarantees the label on every
# listed thread.
_OPERATOR = re.compile(r"^(?:OR|NOT)$|[|{}()]", re.IGNORECASE)


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
            with open(path, "rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                continue
            data = yaml.safe_load(raw.decode("utf-8"))
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
    be written with hyphens, as Gmail does).

    Quoted phrases are inert (`subject:"label:x"` is not a label term) and
    any OR, NOT, `|`, brace or parenthesis rejects the whole query.
    """
    if not isinstance(query, str) or not isinstance(label, str):
        return False
    label = label.strip()
    if not label:
        return False
    wanted = {label.casefold(), label.casefold().replace(" ", "-")}
    found = False
    for match in _TOKEN.finditer(query):
        token = match.group(0)
        if '"' not in token and _OPERATOR.search(token):
            return False
        if match.group("label"):
            value = token[len("label:"):].strip('"').strip().casefold()
            found = found or value in wanted
    return found


def label_routines(base_dir, label):
    """Ids of the Gmail routines whose search queries require `label`.

    A routine counts only when it has a `gmail_mcp` block and EVERY Gmail
    block it has (`gmail` or `gmail_mcp`) requires the label: events carry
    the routine id, not the block, so one unlabelled block would let its
    threads through. Every thread such a routine captured matched a query
    that required the label, so it carried the label when it was captured.
    This reads the routine's CURRENT query, not the one in force when an
    older event was captured. The event stream itself does not record labels.
    """
    ids = set()
    for routine in routines(base_dir):
        blocks = [b for b in _source_blocks(routine)
                  if b.get("kind") in ("gmail", "gmail_mcp")]
        if (any(b.get("kind") == "gmail_mcp" for b in blocks)
                and all(query_requires_label(b.get("query"), label)
                        for b in blocks)):
            ids.add(routine["id"])
    return ids
