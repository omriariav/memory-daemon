#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${MEMORY_DAEMON_DIR:-}" ]]; then
  REPO_DIR="$MEMORY_DAEMON_DIR"
elif [[ -f "$SCRIPT_DIR/daemon.py" ]]; then
  REPO_DIR="$SCRIPT_DIR"
else
  REPO_DIR="$HOME/Code/memory-daemon"
fi

if [[ ! -f "$REPO_DIR/daemon.py" ]]; then
  echo "memory-daemon not found at $REPO_DIR" >&2
  echo "Set MEMORY_DAEMON_DIR to the repository directory." >&2
  exit 1
fi

exec python3 "$REPO_DIR/daemon.py" status "$@"
