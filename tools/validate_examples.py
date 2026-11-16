#!/usr/bin/env python3
"""Validate routines/_template.yaml, routines/_example-*.yaml and
inbox.example.yaml.

The loader deliberately skips files starting with "_", and real routines are
gitignored as personal config — so without this nothing shipped in the repo is
ever schema-checked, and the documented examples drift the moment validation
changes.

Run: python3 tools/validate_examples.py
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_daemon import config  # noqa: E402
from workspace_daemon.inbox import settings as inbox_settings  # noqa: E402


# Top-level blocks the example ships commented out (they are off by default).
COMMENTED_BLOCKS = ("rsvp", "save", "context", "summary", "actions", "send",
                    "react", "polish", "copy", "team", "slack_open")


def uncomment_blocks(text, keys=COMMENTED_BLOCKS):
    """The example's text with each `# <key>:` block (the line and the `#   `
    indented lines under it) switched on, so the commented examples are
    checked like the live ones. Raises ValueError when a block is missing."""
    lines, out, found = text.splitlines(), [], set()
    i = 0
    while i < len(lines):
        key = next((k for k in keys if lines[i].startswith(f"# {k}:")), None)
        if key is None:
            out.append(lines[i])
            i += 1
            continue
        found.add(key)
        out.append(lines[i][2:])
        i += 1
        while i < len(lines) and lines[i].startswith("#   "):
            out.append(lines[i][2:])
            i += 1
    missing = [k for k in keys if k not in found]
    if missing:
        raise ValueError("inbox.example.yaml has no commented block for: "
                         + ", ".join(missing))
    return "\n".join(out) + "\n"


def _inbox_problems(path):
    try:
        inbox_settings.load(ROOT, path)
        # The same file with the commented rsvp:, save:, context:, summary:, actions:,
        # send:, react:, polish:, copy:, team: and slack_open: blocks switched on.
        raw = yaml.safe_load(uncomment_blocks(
            Path(path).read_text(encoding="utf-8")))
        settings = inbox_settings.validate(raw)
        for key in COMMENTED_BLOCKS:
            if settings.get(key) in (None, False):
                return [f"the commented {key}: example did not validate"]
    except inbox_settings.SettingsError as exc:
        return exc.problems
    except (ValueError, yaml.YAMLError) as exc:
        return [str(exc)]
    return []


def main():
    paths = sorted(config.routines_dir(ROOT).glob("_*.yaml"))
    if not paths:
        print("no template or example routines found", file=sys.stderr)
        return 1

    failed = 0
    for path in paths:
        data = yaml.safe_load(path.read_text())
        if not data:
            print(f"✗ {path.name}: empty")
            failed += 1
            continue
        data.setdefault("id", path.stem.lstrip("_"))
        # Examples may name a private watch file that does not exist here.
        config.expand_watch_files(data, missing_ok=True)
        problems = config.validate(data)
        print(f"{'✗' if problems else '✓'} {path.name}")
        for problem in problems:
            print(f"    {problem}")
        failed += bool(problems)

    total = len(paths) + 1
    example = ROOT / "inbox.example.yaml"
    problems = (["file is missing"] if not example.exists()
                else _inbox_problems(example))
    print(f"{'✗' if problems else '✓'} inbox.example.yaml")
    for problem in problems:
        print(f"    {problem}")
    failed += bool(problems)

    print(f"\n{total - failed}/{total} valid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
