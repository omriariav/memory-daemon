#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${MEMORY_DAEMON_DIR:-}" ]]; then
  REPO_DIR="$MEMORY_DAEMON_DIR"
else
  REPO_DIR="$SCRIPT_DIR"
fi

LABEL="${MEMORY_DAEMON_LAUNCHD_LABEL:-com.memory-daemon}"
MAINTENANCE_LABEL="${MEMORY_DAEMON_MAINTENANCE_LAUNCHD_LABEL:-com.memory-daemon-maintenance}"
CALENDAR_LABEL="${MEMORY_DAEMON_CALENDAR_LAUNCHD_LABEL:-com.memory-daemon-calendar}"
DOMAIN="${MEMORY_DAEMON_LAUNCHD_DOMAIN:-gui/$(id -u)}"
PLIST="${MEMORY_DAEMON_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
MAINTENANCE_PLIST="${MEMORY_DAEMON_MAINTENANCE_PLIST:-$HOME/Library/LaunchAgents/$MAINTENANCE_LABEL.plist}"
CALENDAR_PLIST="${MEMORY_DAEMON_CALENDAR_PLIST:-$HOME/Library/LaunchAgents/$CALENDAR_LABEL.plist}"
LAUNCHCTL="${MEMORY_DAEMON_LAUNCHCTL:-launchctl}"
PYTHON="${MEMORY_DAEMON_PYTHON:-python3}"

if [[ ! -f "$REPO_DIR/daemon.py" ]]; then
  echo "memory-daemon not found at $REPO_DIR" >&2
  echo "Set MEMORY_DAEMON_DIR to the repository directory." >&2
  exit 1
fi

echo "Validating memory-daemon configuration..."
"$PYTHON" "$REPO_DIR/daemon.py" validate

load_scheduler() {
  local label="$1"
  local plist="$2"
  local target="$DOMAIN/$label"
  if [[ ! -f "$plist" ]]; then
    echo "LaunchAgent plist not found at $plist" >&2
    return 1
  fi
  if "$LAUNCHCTL" print "$target" >/dev/null 2>&1; then
    # An already-loaded LaunchAgent keeps its old ProgramArguments and
    # environment even when the plist on disk changes. run.sh is the explicit
    # activation command, so atomically replace the loaded definition before
    # starting its next tick.
    echo "Reloading scheduler from plist: $label"
    "$LAUNCHCTL" bootout "$target"
    "$LAUNCHCTL" enable "$target"
    "$LAUNCHCTL" bootstrap "$DOMAIN" "$plist"
  else
    echo "Loading scheduler: $label"
    "$LAUNCHCTL" enable "$target"
    "$LAUNCHCTL" bootstrap "$DOMAIN" "$plist"
  fi
}

load_scheduler "$LABEL" "$PLIST"
load_scheduler "$MAINTENANCE_LABEL" "$MAINTENANCE_PLIST"
# The calendar job is optional: only load it once its plist has been rendered.
CALENDAR_ENABLED=0
if [[ -f "$CALENDAR_PLIST" ]]; then
  CALENDAR_ENABLED=1
  load_scheduler "$CALENDAR_LABEL" "$CALENDAR_PLIST"
fi

TARGET="$DOMAIN/$LABEL"
MAINTENANCE_TARGET="$DOMAIN/$MAINTENANCE_LABEL"
CALENDAR_TARGET="$DOMAIN/$CALENDAR_LABEL"

echo "Starting one capture coordinator tick for due routines..."
"$LAUNCHCTL" kickstart "$TARGET"
echo "Starting one maintenance coordinator tick for due routines..."
"$LAUNCHCTL" kickstart "$MAINTENANCE_TARGET"
if (( CALENDAR_ENABLED )); then
  echo "Starting one calendar coordinator tick for due routines..."
  "$LAUNCHCTL" kickstart "$CALENDAR_TARGET"
fi

echo
echo "Scheduler started. Current status:"
status_rc=0
"$REPO_DIR/memory-daemon-status.sh" || status_rc=$?
if (( status_rc != 0 )); then
  echo "Status currently reports attention; the triggered tick may still be running." >&2
  echo "Run memory-daemon-status.sh again after it finishes." >&2
fi
