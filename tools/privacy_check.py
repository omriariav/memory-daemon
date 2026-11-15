#!/usr/bin/env python3
"""Scan tracked files for private-looking data before it reaches a public repo.

Checks, per line of every tracked text file:
  * email addresses other than example.com / example.org / reserved TLDs
  * Slack-style user and channel ids (\\b[UCWG][A-Z0-9]{8,11}\\b)
  * secret patterns (cloud and chat tokens, private keys, quoted credentials)
  * every entry of an optional local denylist, which is never in the repo:
      ~/.config/memory-daemon/private-denylist.txt  (override: --denylist, or
      $MEMORY_DAEMON_DENYLIST). One entry per line, `#` comments; a plain
      entry is a case-insensitive substring, `re:<regex>` is a regex.

False positives go in tools/privacy_allowlist.txt, one entry per line, `#`
comments: `TOKEN` (exact flagged text, any file), `re:<regex>` (must match the
whole flagged text), or `<path glob> :: TOKEN-or-re:<regex>` to scope an entry
to matching files. Denylist hits cannot be allowlisted.

Output never prints a denylist entry, only file:line and its number, so the
log is safe to paste.

Usage:
    python3 tools/privacy_check.py            # all tracked files
    python3 tools/privacy_check.py --staged   # what is about to be committed
Pre-commit hook (once per clone):
    printf '#!/bin/sh\\nexec python3 tools/privacy_check.py --staged\\n' \\
        > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
Exit status: 0 clean, 1 findings, 2 usage or setup error.
"""
import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = Path(__file__).resolve().parent / "privacy_allowlist.txt"
DEFAULT_DENYLIST = Path("~/.config/memory-daemon/private-denylist.txt")
MAX_FILE_BYTES = 2 * 1024 * 1024

EMAIL = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@"
    r"((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b"
)
SAFE_DOMAINS = ("example.com", "example.org")
SAFE_TLDS = {"test", "example", "invalid", "localhost"}
SLACK_ID = re.compile(r"\b[UCWG][A-Z0-9]{8,11}\b")

SECRETS = [
    ("aws access key", re.compile(r"\bA(?:KIA|SIA)[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("anthropic or openai key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}")),
    ("quoted credential", re.compile(
        r"""(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b["']?\s*[:=]\s*"""
        r"""["'][A-Za-z0-9/+_=.-]{16,}["']""")),
]
# A secret-shaped string that is obviously a placeholder (a real key never
# spells out the alphabet or is all zeros).
PLACEHOLDER = re.compile(
    r"(?i)example|placeholder|replace-?me|your[_-]|x{4,}|changeme|\.\.\.|<"
    r"|abcdefghij|1234567890|0{8,}|dummy|fake|secret-diagnosis")


class Finding:
    def __init__(self, path, line, kind, text, secret=False):
        self.path, self.line, self.kind = path, line, kind
        self.text, self.secret = text, secret

    def __str__(self):
        shown = "<hidden>" if self.secret else self.text
        return f"{self.path}:{self.line}: {self.kind}: {shown}"


def load_entries(path):
    """Non-empty, non-comment lines of a list file, or [] if it is absent."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def email_ok(domain):
    domain = domain.lower()
    return (domain.rsplit(".", 1)[-1] in SAFE_TLDS
            or any(domain == d or domain.endswith("." + d) for d in SAFE_DOMAINS))


def _entry_matches(token, text):
    if token.startswith("re:"):
        return re.fullmatch(token[3:], text) is not None
    return token == text


def allowed(path, text, entries):
    for entry in entries:
        if " :: " in entry:
            glob, _, token = entry.partition(" :: ")
            if fnmatch.fnmatch(path, glob.strip()) and \
                    _entry_matches(token.strip(), text):
                return True
        elif _entry_matches(entry, text):
            return True
    return False


def compile_denylist(entries):
    out = []
    for n, entry in enumerate(entries, 1):
        if entry.startswith("re:"):
            try:
                out.append((n, re.compile(entry[3:], re.IGNORECASE)))
            except re.error as exc:
                raise ValueError(f"denylist entry {n}: bad regex ({exc})")
        else:
            out.append((n, re.compile(re.escape(entry), re.IGNORECASE)))
    return out


def scan_text(path, text, allow, deny):
    """Findings in one file's text."""
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for n, pattern in deny:
            if pattern.search(line):
                found.append(Finding(path, lineno, f"denylist entry {n}", "", True))
        for m in EMAIL.finditer(line):
            if not email_ok(m.group(1)) and not allowed(path, m.group(0), allow):
                found.append(Finding(path, lineno, "email", m.group(0)))
        for m in SLACK_ID.finditer(line):
            if not allowed(path, m.group(0), allow):
                found.append(Finding(path, lineno, "slack-style id", m.group(0)))
        for kind, pattern in SECRETS:
            for m in pattern.finditer(line):
                hit = m.group(0)
                if kind != "private key block" and PLACEHOLDER.search(hit):
                    continue
                if not allowed(path, hit, allow):
                    found.append(Finding(path, lineno, f"secret ({kind})", hit, True))
    return found


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          check=True).stdout


def tracked_files():
    return [p for p in git("ls-files", "-z").decode("utf-8", "replace").split("\0") if p]


def staged_files():
    out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def read_blob(path, staged):
    if staged:
        data = git("show", f":{path}")
    else:
        data = (ROOT / path).read_bytes()
    if len(data) > MAX_FILE_BYTES or b"\0" in data[:8192]:
        return None  # binary or huge: not scanned
    return data.decode("utf-8", "replace")


def run(paths, staged=False, allow=(), deny=()):
    findings = []
    own = os.path.relpath(ALLOWLIST, ROOT)  # it lists the flagged text itself
    for path in paths:
        if path == own:
            continue
        try:
            text = read_blob(path, staged)
        except (OSError, subprocess.CalledProcessError):
            continue  # deleted in the work tree, or a submodule
        if text is not None:
            findings.extend(scan_text(path, text, allow, deny))
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--staged", action="store_true",
                    help="scan the staged versions of the files being committed")
    ap.add_argument("--denylist", help="local denylist file (default: "
                    f"$MEMORY_DAEMON_DENYLIST or {DEFAULT_DENYLIST})")
    ap.add_argument("--allowlist", default=str(ALLOWLIST))
    args = ap.parse_args(argv)

    deny_path = Path(args.denylist or os.environ.get("MEMORY_DAEMON_DENYLIST")
                     or DEFAULT_DENYLIST).expanduser()
    try:
        deny = compile_denylist(load_entries(deny_path))
        paths = staged_files() if args.staged else tracked_files()
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        print(f"privacy_check: {exc}", file=sys.stderr)
        return 2
    allow = load_entries(args.allowlist)
    findings = run(paths, args.staged, allow, deny)
    for f in findings:
        print(f)
    state = "denylist active" if deny else "no local denylist"
    print(f"privacy_check: {len(paths)} files, {len(findings)} finding(s) ({state})")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
