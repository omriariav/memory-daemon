# Inbox changelog

Changes to `daemon.py inbox`, its item format and its source protocol. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
versions follow [Semantic Versioning](https://semver.org). The policy is in
[inbox.md](inbox.md#versioning).

## [Unreleased]

## [0.3.0] - 2026-11-16

The first write actions. Both are off until configured in `inbox.yaml`, both
confirm first and then wait out the 5 s undo window, and quitting inside the
window cancels them.

### Added

- RSVP on pending invites: on an invite row `a` accepts, `X` declines and `T`
  answers tentative. A modal shows the event, the time and the exact answer
  (Cancel is focused); after the undo window a worker runs one headless Claude
  call through the daemon's bridge whose only allowed tool is the configured
  calendar RSVP tool, called with exactly three arguments, pinned to the event
  id, the calendar id and the answer (any extra argument is denied: the bridge
  gains an opt-in `exact_args` mode, and every existing caller keeps the
  default). The bridge's guards apply (`state/PAUSED`, the daily budget). The prompt carries only the tool name and those three values. The
  result is checked, never the model's words: a failure keeps the row and
  toasts the reason, a success hides it (`rsvp` in `inbox-state.json`) and
  logs it in the audit log. A recurring series answers its next instance only,
  and the modal says so; the mark hides that event id only, so the next
  instance shows again. If a calendar snapshot taken 10 minutes or more after
  the answer still lists the event as unanswered, the row returns with a "RSVP
  not confirmed by calendar" hint. New `rsvp:` setting (no default): `tool`, `args`
  (the tool's parameter names for `event_id`, `calendar_id` and `response`),
  `timeout_s` (30 to 300), optional `model`. Without it `X` and `T` say "RSVP not
  configured" and `a` keeps acknowledging an invite as before.
- Save to memory (`m`, any item): a modal shows the source, sender, gist and
  link; after the undo window the configured `save` command runs with
  `{"v": 1, "action": "save", "item": <item>}` on stdin and answers
  `{"ok", "message", "memory_id", "duplicate"}`. The item stays listed and
  gets a `◆` (local `saved` mark). New `save:` setting (no default): `argv`,
  `timeout_s` (5 to 120). Without it `m` says "Save not configured". Same
  process-group, timeout and output-cap handling as command sources, except a
  timeout sends `SIGTERM` and kills 5 s later, and says "the save may still
  complete"; item text reaches the command only as JSON on stdin. A command may answer `ok: false` with
  `duplicate: true` (and a `candidate`; `memory_id` is null, the existing
  memory is `candidate.id`): the inbox then asks "Save anyway"
  (Cancel focused) and, once confirmed, resends the same payload with
  `"force": true`. `ok: true` with `duplicate: true` is a success, shown as
  "Already in memory". The duplicate question is a toast ("press m to review"),
  not a modal that could stack on another one. A second `m` while a save is
  pending says "save already in progress". The protocol is in docs/inbox.md.
- Optional `meta.thread_note` (string): the thread pane shows it as one dim
  line above the thread (cleaned to one line, cut at 200 characters); with an
  empty thread it is all the pane shows. Documented in the item format.
- Footer, help overlay and command palette list the new keys.
- `--demo` has an invite row and fake RSVP and save backends.

### Changed

- Pending-invite `meta` also carries `title`, `when` (display text) and
  `cache_fetched` (the snapshot's fetch time).
- `quit` cancels a queued RSVP or save, like a queued send. While a write is
  running the first `q` warns ("N write(s) still running; q again to quit
  (they finish first)") and the exit message says how many are still finishing.
- `inbox-state.json` marks may hold `rsvp` (a word, with `rsvp_event` and
  `rsvp_at`) and `saved` (a time); older files read unchanged.
- The bridge's `call_tools` takes `exact_args` (default False, unchanged); its
  allowlist file gains an `exact_args` flag.

## [0.2.0] - 2026-11-15

Read-side features only: nothing new is written or sent. RSVP and save
actions are planned for 0.3.0; pending invites already carry the event and
calendar ids such an action needs.

### Added

- Show all (`A`): a `command` source that declares `supports_all: true` is
  called as `<argv> list --all` while the toggle is on. Sources that do not
  declare it never receive `--all`. The header shows `all`; off by default,
  per session, never saved. In the footer, help overlay and palette.
- Pending calendar invites: upcoming events whose RSVP is `needsAction` appear
  under Needs reply (source `calendar`) with the title, the time in local time
  and the organizer, read from the calendar cache's snapshot
  (`state/calendar-snapshot.json`) without touching the network. Declined,
  accepted, tentative and cancelled events never show. `meta` carries
  `event_id` and `calendar_id`; the row's time is the event's start (the
  cache has no arrival time), so invites list soonest first and show `in 2h`.
  A `New invite` row from the events stream is dropped when its series has a
  pending row. Actions: `ack` (hide locally) and `open`
  (the meeting link, when the cache has a safe http(s) one). New setting
  `invites_days` on the events source (default 7, 0 turns it off).
- Mail label filter: new events-source setting `email_require_label`
  (default off). When set, only mail captured by a Gmail routine whose search
  query requires that label becomes an email item. A routine counts only when
  every one of its Gmail blocks requires the label; quoted phrases, `OR`,
  `NOT`, `|`, braces and parentheses never count as a label term. It uses the
  routine's current query. See docs/inbox.md for what this does and does not
  prove.
- Saved view (`S`): a read-only list of what the daemon saved to memory
  recently (capture events of kind `new` or `update`), with the source glyph,
  title, time and kind; `o` opens a link, `esc` returns. New setting
  `saved_days` on the events source (default 2). Saved rows are not inbox
  items and cannot be acted on.
- Change-driven refresh: every `watch_interval_s` (default 3, 1 to 30) the
  inbox stat-checks the files each source depends on and re-runs only a source
  whose file changed (mtime and size; a file that appears counts), debounced
  to one refresh per source per 2 s and never overlapping. The built-in
  sources know their files (events, the alert state, the calendar cache, and
  the Saved view while it is open); a `command` source declares
  `watch: [paths]` (relative to the base dir, absolute or `~`) and without it
  is refreshed by the fallback timer and `r` only. A fallback refresh that
  lands on a running one is queued, not dropped. The header's `updated Ns ago`
  treats a watched source with unchanged files as fresh, and the 30 s timer
  re-applies the clock (ended invites, the window, alert age, urgent-soon
  rules) without reading any file. Watched paths are stat-ed on the UI thread:
  keep them off network mounts. The file signature includes the inode, so an
  atomic replace that keeps the mtime and size is still seen.

### Changed

- `poll_interval_s` is now the fallback full refresh: default 600 (was 60),
  range 30 to 3600 (was 5 to 3600). A config that set a value below 30 must
  raise it.
- The header shows `updated Ns ago` for the least recently refreshed source
  instead of the next-poll countdown.

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
