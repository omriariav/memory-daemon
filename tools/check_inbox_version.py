#!/usr/bin/env python3
"""Fail a PR that changes workspace_daemon/inbox/ without a version or changelog change.

    python3 tools/check_inbox_version.py BASE_REF

Compares BASE_REF...HEAD (changes since the merge base). If any file under
workspace_daemon/inbox/ changed, then either `__version__` in
workspace_daemon/inbox/__init__.py changed or docs/inbox-changelog.md changed
(an entry under [Unreleased] is enough; the bump happens at release time).
See docs/inbox.md ("Versioning").
"""
import argparse
import subprocess
import sys

INBOX_DIR = "workspace_daemon/inbox/"
INIT = INBOX_DIR + "__init__.py"
LOG_PATH = "docs/inbox-changelog.md"


def git(args, cwd=None):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def check(base, cwd=None):
    """Return (ok, message)."""
    span = f"{base}...HEAD"
    changed = git(["diff", "--name-only", "--no-renames", span], cwd).split("\n")
    changed = [c for c in changed if c]
    inbox = [c for c in changed if c.startswith(INBOX_DIR)]
    if not inbox:
        return True, "no changes under workspace_daemon/inbox/"
    if LOG_PATH in changed:
        return True, f"{LOG_PATH} changed"
    if INIT in changed:
        diff = git(["diff", "-U0", span, "--", INIT], cwd)
        if any(line[:1] in "+-" and line[1:].lstrip().startswith("__version__")
               for line in diff.split("\n")
               if not line.startswith(("+++", "---"))):
            return True, "__version__ changed"
    return False, (
        f"{len(inbox)} file(s) under {INBOX_DIR} changed, but neither "
        f"__version__ ({INIT}) nor {LOG_PATH} did. Add an entry under "
        "[Unreleased] (or bump the version with a matching changelog "
        "heading); see docs/inbox.md, Versioning.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("base", help="base ref or SHA of the PR")
    args = ap.parse_args(argv)
    try:
        ok, msg = check(args.base)
    except RuntimeError as exc:
        print(f"check_inbox_version: {exc}", file=sys.stderr)
        return 2
    print(f"check_inbox_version: {'ok' if ok else 'FAIL'}: {msg}",
          file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
