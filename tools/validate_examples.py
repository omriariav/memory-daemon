#!/usr/bin/env python3
"""Validate routines/_template.yaml and routines/_example-*.yaml.

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
        problems = config.validate(data)
        print(f"{'✗' if problems else '✓'} {path.name}")
        for problem in problems:
            print(f"    {problem}")
        failed += bool(problems)

    print(f"\n{len(paths) - failed}/{len(paths)} valid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
