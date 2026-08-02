#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${MEMORY_DAEMON_DIR:-}" ]]; then
  REPO_DIR="$MEMORY_DAEMON_DIR"
else
  REPO_DIR="$SCRIPT_DIR"
fi

LABEL="${MEMORY_DAEMON_LAUNCHD_LABEL:-com.memory-daemon}"
DOMAIN="${MEMORY_DAEMON_LAUNCHD_DOMAIN:-gui/$(id -u)}"
PLIST="${MEMORY_DAEMON_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
LAUNCHCTL="${MEMORY_DAEMON_LAUNCHCTL:-launchctl}"
PYTHON="${MEMORY_DAEMON_PYTHON:-python3}"

if [[ ! -f "$REPO_DIR/daemon.py" ]]; then
  echo "memory-daemon not found at $REPO_DIR" >&2
  echo "Set MEMORY_DAEMON_DIR to the repository directory." >&2
  exit 1
fi

echo "Validating memory-daemon configuration..."
"$PYTHON" "$REPO_DIR/daemon.py" validate

TARGET="$DOMAIN/$LABEL"
if "$LAUNCHCTL" print "$TARGET" >/dev/null 2>&1; then
  echo "Scheduler already loaded: $LABEL"
  "$LAUNCHCTL" enable "$TARGET"
else
  if [[ ! -f "$PLIST" ]]; then
    echo "LaunchAgent plist not found at $PLIST" >&2
    echo "Render and configure it before running this helper." >&2
    exit 1
  fi
  echo "Loading scheduler: $LABEL"
  "$LAUNCHCTL" enable "$TARGET"
  "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST"
fi

echo "Starting one coordinator tick for all enabled routines that are due..."
"$LAUNCHCTL" kickstart "$TARGET"

echo
echo "Scheduler started. Current status:"
status_rc=0
"$REPO_DIR/memory-daemon-status.sh" || status_rc=$?
if (( status_rc != 0 )); then
  echo "Status currently reports attention; the triggered tick may still be running." >&2
  echo "Run memory-daemon-status.sh again after it finishes." >&2
fi
