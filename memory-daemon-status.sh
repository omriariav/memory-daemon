#!/usr/bin/env bash
# Daemon status and controls.
#   memory-daemon-status.sh            terminal UI on a TTY, else plain status
#   memory-daemon-status.sh status     plain status (extra args pass through)
#   memory-daemon-status.sh tui        terminal UI
#   memory-daemon-status.sh pause|resume
#   memory-daemon-status.sh state      one-line summary
#   memory-daemon-status.sh run <id>   run one routine now
#   memory-daemon-status.sh logs [-f]  tail logs/run.log
#   anything else                      passed to `daemon.py status`
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${MEMORY_DAEMON_DIR:-}" ]]; then
  REPO_DIR="$MEMORY_DAEMON_DIR"
elif [[ -f "$SCRIPT_DIR/daemon.py" ]]; then
  REPO_DIR="$SCRIPT_DIR"
elif [[ -d "$HOME/code/memory-daemon" ]]; then
  REPO_DIR="$HOME/code/memory-daemon"
else
  REPO_DIR=""
fi

if [[ -z "$REPO_DIR" || ! -f "$REPO_DIR/daemon.py" ]]; then
  echo "memory-daemon not found${REPO_DIR:+ at $REPO_DIR}" >&2
  echo "Set MEMORY_DAEMON_DIR to the repository directory." >&2
  exit 1
fi

if [[ -n "${MEMORY_DAEMON_PYTHON:-}" ]]; then
  PYTHON="$MEMORY_DAEMON_PYTHON"
elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
  PYTHON="$REPO_DIR/.venv/bin/python"
else
  PYTHON="python3"
fi

daemon() { exec "$PYTHON" "$REPO_DIR/daemon.py" "$@"; }

if [[ $# -eq 0 ]]; then
  if [[ -t 1 && -t 0 ]]; then
    daemon tui
  fi
  daemon status
fi

case "$1" in
  tui) shift; daemon tui "$@" ;;
  status) shift; daemon status "$@" ;;
  pause) shift; daemon ctl pause "$@" ;;
  resume) shift; daemon ctl resume "$@" ;;
  state) shift; daemon ctl state "$@" ;;
  run)
    shift
    if [[ $# -ge 1 && "$1" != -* ]]; then
      id="$1"; shift
      daemon run --routine "$id" "$@"
    fi
    daemon run "$@"
    ;;
  logs)
    shift
    exec tail -n 50 "$@" "$REPO_DIR/logs/run.log"
    ;;
  *) daemon status "$@" ;;
esac
