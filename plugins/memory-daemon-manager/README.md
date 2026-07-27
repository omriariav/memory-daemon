# Memory Daemon Manager

A dual-compatible Codex and Claude Code plugin for safely administering a local
memory-daemon checkout.

The administration helper supports macOS and Linux. Windows is not supported
because memory-daemon relies on POSIX file locking and its scheduler targets
macOS launchd.

It provides two shared skills:

- `manage-memory-daemon-routines`: list, inspect, add, edit, enable, disable,
  and remove routine YAML.
- `manage-memory-connector-prompts`: list, inspect, create, edit, and remove
  private source-wide connector prompt overrides.

Every write is planned first. The helper binds a `plan-token` to the exact
before and after content, rejects stale plans, applies one file atomically,
runs `daemon.py validate`, and rolls back when validation fails. It never runs
a routine, starts the scheduler, or deletes captured history.

## Install

### Codex

```sh
codex plugin marketplace add omriariav/memory-daemon
codex plugin add memory-daemon-manager@memory-daemon
```

Start a new Codex thread after installation so the skills are discovered.

### Claude Code

```sh
claude plugin marketplace add omriariav/memory-daemon
claude plugin install memory-daemon-manager@memory-daemon
```

Run `/reload-plugins` or start a new Claude Code session.

## Upgrade

For Codex:

```sh
codex plugin marketplace upgrade memory-daemon
codex plugin add memory-daemon-manager@memory-daemon
```

For Claude Code:

```sh
claude plugin marketplace update memory-daemon
claude plugin update memory-daemon-manager@memory-daemon
```

## Uninstall

For Codex:

```sh
codex plugin remove memory-daemon-manager@memory-daemon
codex plugin marketplace remove memory-daemon
```

For Claude Code:

```sh
claude plugin uninstall memory-daemon-manager@memory-daemon
claude plugin marketplace remove memory-daemon
```

Uninstalling the plugin does not change routine files, connector prompts,
ledger state, captured notes, memory entries, or scheduler state.

## Recovery

- If daemon validation fails during apply, the helper restores the previous
  file automatically.
- If a plan becomes stale, rerun `plan` and review the new diff; no write has
  occurred.
- After a successful change, use the reviewed diff or the owning repository's
  version history to reverse it.
- Removing a routine affects only its YAML. Removing a connector prompt affects
  only the private override; a generic template remains available when present.
