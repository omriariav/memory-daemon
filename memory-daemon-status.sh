#!/usr/bin/env bash
# Daemon status and controls.
#   memory-daemon-status.sh            terminal UI on a TTY, else plain status
#   memory-daemon-status.sh status     plain status (extra args pass through)
#   memory-daemon-status.sh tui        terminal UI
#   memory-daemon-status.sh pause|resume
#   memory-daemon-status.sh state      one-line summary
#   memory-daemon-status.sh run <id>   run one routine now
#   memory-daemon-status.sh logs [-f] [-n N]   tail logs/run.log
#   anything else                      passed to `daemon.py status`
#
# Target: unless MEMORY_DAEMON_DIR / MEMORY_DAEMON_PYTHON are set, the checkout
# and interpreter are taken from the installed launchd job (the default label's
# plist), so the controls act on what the scheduler actually runs.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Interpreter and daemon.py of the installed scheduler job, if any (stdlib only).
PLIST="$HOME/Library/LaunchAgents/${MEMORY_DAEMON_LAUNCHD_LABEL:-com.memory-daemon}.plist"
PLIST_PYTHON=""
PLIST_SCRIPT=""
if [[ -f "$PLIST" ]] && command -v python3 >/dev/null 2>&1; then
  {
    read -r PLIST_PYTHON || true
    read -r PLIST_SCRIPT || true
  } < <(python3 - "$PLIST" 2>/dev/null <<'PY' || true
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as handle:
        args = plistlib.load(handle).get("ProgramArguments") or []
except Exception:
    args = []
for i, arg in enumerate(args):
    if str(arg).endswith("daemon.py") and i >= 1:
        print(args[i - 1])
        print(arg)
        break
PY
  )
fi

if [[ -n "${MEMORY_DAEMON_DIR:-}" ]]; then
  REPO_DIR="$MEMORY_DAEMON_DIR"
elif [[ -n "$PLIST_SCRIPT" && -f "$PLIST_SCRIPT" ]]; then
  REPO_DIR="$(dirname -- "$PLIST_SCRIPT")"
elif [[ -f "$SCRIPT_DIR/daemon.py" ]]; then
  REPO_DIR="$SCRIPT_DIR"
elif [[ -d "$HOME/code/memory-daemon" ]]; then
  REPO_DIR="$HOME/code/memory-daemon"
elif [[ -d "$HOME/Code/memory-daemon" ]]; then
  REPO_DIR="$HOME/Code/memory-daemon"
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
elif [[ -n "$PLIST_PYTHON" && -x "$PLIST_PYTHON" ]]; then
  PYTHON="$PLIST_PYTHON"
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
    LOG="$REPO_DIR/logs/run.log"
    if [[ ! -f "$LOG" ]]; then
      echo "no log yet at $LOG (the daemon has not run here)" >&2
      exit 1
    fi
    # Default to 50 lines unless the caller gave their own -n/--lines.
    has_n=0
    for arg in "$@"; do
      case "$arg" in -n|-n*|--lines|--lines=*) has_n=1 ;; esac
    done
    if [[ $has_n -eq 1 ]]; then
      exec tail "$@" "$LOG"
    fi
    exec tail -n 50 "$@" "$LOG"
    ;;
  *) daemon status "$@" ;;
esac
