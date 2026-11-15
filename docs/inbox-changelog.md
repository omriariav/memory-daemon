# Inbox changelog

Changes to `daemon.py inbox`, its item format and its source protocol. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
versions follow [Semantic Versioning](https://semver.org). The policy is in
[inbox.md](inbox.md#versioning).

## [Unreleased]

## [0.1.0] - 2026-11-15

First versioned release: the inbox as merged in PR #128, plus the copy keys
and stale-alert handling from PR #130.

### Added

- Unified keyboard-first terminal inbox (`daemon.py inbox`, optional Textual
  extra) merging several sources into one queue: Urgent, Needs reply, Waiting,
  FYI, with a thread pane, draft pane, fuzzy filter and source filter.
- Item format v1 and the source-command protocol: any program that prints
  JSON for `<argv> list` can feed the inbox.
- Built-in `events` source over `state/events.jsonl` (mail, calendar changes,
  recordings, daemon alerts), read-only and without taking its lock.
- Local `done`, `snooze` and `ack` marks in `state/inbox-state.json`
  (mode 0600; snoozed items return when due; writes are serialised by a
  state-file lock). Nothing is sent in this version.
- `o` opens an item's link; only plain `http` and `https` links are opened.
- Run options: `--demo` (invented data, nothing read or written), `--empty`
  (with `--demo`, start with an empty inbox) and `--base-dir DIR` (read state
  and `inbox.yaml` from another directory).
- Settings in `inbox.yaml`: `poll_interval_s` (5 to 3600) and `gmail_account`
  (the `/mail/u/N/` index in Gmail links).
- Source safety: command output is capped (4 MB) and polls never overlap (a
  poll still running when the next is due is skipped).
- Keys, command palette (`ctrl+p`), help overlay (`?`), audit log (`L`),
  undo (`u`, 5 s window) and themes.
- Hebrew/RTL display with `logical` and `visual` bidi modes, terminal
  auto-detection, `$INBOX_BIDI` override and a runtime toggle (`b`).
- Copy keys: `y` copies the item, `Y` copies its section, and the palette
  offers "Copy section as Markdown" (OSC 52 clipboard, logical text only).
- Stale and resolved daemon-alert handling: alerts resolve from the daemon's
  active-alert state, recovered alerts are hidden by default, and `R` shows or
  hides them.
- `daemon.py inbox --version`, the version in the header and the help overlay,
  and this changelog.
