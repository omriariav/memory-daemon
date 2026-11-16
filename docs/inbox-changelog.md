# Inbox changelog

Changes to `daemon.py inbox`, its item format and its source protocol. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
versions follow [Semantic Versioning](https://semver.org). The policy is in
[inbox.md](inbox.md#versioning).

## [Unreleased]

## [0.5.0] - 2026-11-16

Custom actions, and a handled item leaves the queue. Everything new is off
until configured in `inbox.yaml` (a save now marks the item done unless told
not to).

### Added

- Generic `actions:` hook in `inbox.yaml` (no default; up to 8 entries): `key`,
  `label`, `argv`, `timeout_s` (5 to 300, default 60, per call) and `mark_done`
  (default `true`). The key must be a letter the inbox does not use; the free
  ones are `h l p t w x z` and `B D E F H J K M N O P Q U V W Z`, and
  validation refuses any other, and a key or label used twice. The command is
  called with JSON on stdin only (item text never in argv) and answers one JSON
  line: `choices` (read-only; at most 50 `{id, label, detail}`, cleaned like
  context text), `prepare` (read-only, may take about 20 s: `{ok, rows:
  [[label, value]], state, message, duplicate}`, `state` an opaque string of at
  most 64 KB) and `run` (`{ok, message}`; the only call that writes). The flow:
  a "looking up" modal (`esc` cancels and stops the command), a picker (shown
  even for one choice), the same modal while `prepare` runs, the confirm modal
  with the prepared rows (Cancel is focused), the 5 s undo window (`u` undoes,
  quitting inside it cancels), then `run` once in a worker thread, never
  retried, with a row in the audit log. A `duplicate: true` answer is checked
  before `ok` and shown as information ("Already there"), not an error; any
  other `ok: false` shows its message and stops. A running action counts in
  the quit guard like a write. The commands run under the capped runner with the
  `SIGTERM` grace of `save`. Results reach the UI through the non-blocking
  `_post`. Footer, help overlay and palette list the configured actions;
  `--demo` has a fake one.
- `save: {mark_done: true}` (default) and a per-action `mark_done`: a
  successful save, including "already in memory" and a forced "Save anyway",
  and a successful action run mark the item done the way `d` does (a local
  mark, an audit row, and the 5 s window in which `u` brings the row back). A
  failed save or run, and the "possible duplicate" question, leave the row.
  `mark_done: false` keeps the old behaviour (the row stays, marked saved). The
  Saved view (`S`) still lists the item: it reads capture events, not the queue.
- `docs/inbox.md`: "Actions protocol" with the contract (commands must be safe
  to call repeatedly, `choices` and `prepare` read-only, `run` idempotent where
  possible) and "Custom actions"; a commented `actions:` example in
  `inbox.example.yaml`.

- Thread order: the thread pane now lists the **newest message first** (it was
  oldest first), scrolled to the top, with a dim "thread start" marker under
  the first message of a thread of two or more. `v` toggles to oldest first and
  back (the queue is not touched); a new `thread_order: newest | oldest`
  setting (default `newest`) is the starting order, and the toggle is saved in
  `state/inbox-state.json` (`prefs`), so a restart keeps it (the saved choice
  wins over the setting once made). The item header, `meta.thread_note` and the
  RSVP hint stay on top. `v` is now a built-in key, so it is not available to
  `actions:`. Footer, help overlay and command palette list it.

### Changed

- A successful save no longer leaves the row in the queue (see `mark_done`
  above; set `save: {mark_done: false}` to keep it).
- `save.mark_done` is part of the validated `save` block.
- A mark applied after a background save or action never moves the cursor off
  the item the user is on.
- A queue highlight message that arrives after the screen is gone no longer
  raises `NoMatches` while the app exits.

## [0.4.0] - 2026-11-16

A Context pane and an AI summary on demand. Both are read-only, off until
configured in `inbox.yaml`, and neither confirms or writes anything outside the
inbox's own state files.

### Added

- Context pane (`c`): a generic `context:` hook in `inbox.yaml` (no default):
  `argv` and `timeout_s` (2 to 60, default 20). The command gets `{"v": 1,
  "action": "context", "item": <item>}` on stdin and prints one JSON object,
  `{"v": 1, "ok", "sections": [{"title", "lines"}], "message"}`. It runs under
  the same capped runner as `save` (own process group, `SIGTERM` then `SIGKILL`
  5 s later on a timeout, item text only on stdin). Everything it prints is
  cleaned and capped: at most 6 sections, 40 lines each, 300 characters a line,
  20000 characters in all, 256 KB of raw output. `ok: false` shows its message
  in the pane. The command must be read-only and exit within the timeout or
  stop its children on `SIGTERM`. Without `context:`, `c` says "Context not
  configured".
- While the pane is open, selecting an item fetches its context in a
  background thread after about 400 ms; a newer selection supersedes an older
  one (a late result is cached, not drawn). At most 2 commands run at once and
  a fetch still waiting for a slot for an item that is no longer selected never
  starts. Results are cached in memory per (item id, `ts`) for 10 minutes
  (errors for 30 s); when the shown item's entry expires its old rows stay
  drawn, marked stale, and one refetch starts by itself. The pane has loading,
  error and ready states, shows the age of a cached result, and is redrawn only
  when its content or an age label changes. `C`, or `r` while the pane has
  focus, fetches again. Quitting stops a running context command with `SIGTERM`
  (then `SIGKILL` after the grace period).
- AI summary (`i`, and `I` to ask again): a new `summary:` setting (no default;
  `summary: {}` uses the defaults): `model` (default `haiku`), `timeout_s` (30
  to 300, default 90), `max_budget_usd` (0.005 to 2, default 0.05). One
  `claude_bridge.complete` call with no tools at all (empty allowlist, every
  built-in disallowed, an empty hook allowlist, any tool use fails the run),
  over the item, its thread and the context sections if loaded. The bridge's
  pause file and daily budget apply; spend is logged as `inbox-summary`. Item
  text is data: it sits between marker lines with a per-call random token,
  after rules that say to ignore instructions found in it, and never reaches
  argv. At most 6 bullet lines (what is asked, the decision or next step,
  deadlines). Cached on disk in `state/inbox-summary/cache.json` (0600, in a
  0700 directory of its own, 200 most recently used entries), keyed by a hash of
  the item id, `ts`, thread and model; `i` on a cached item shows it without a
  call (even while another summary runs), `I` forces a new one. The summary is
  the top section of the Context pane. Quitting while one runs asks first and
  then waits for it (at most `timeout_s`), so the bridge logs the spend. Without `summary:` the keys
  say "Summary not configured".
- Footer, help overlay and command palette list the new keys; `--demo` has fake
  context and summary backends (no command and no model is run).
- `docs/inbox.md` sections "Context protocol", "Context pane" and "AI
  summary", and commented `context:` and `summary:` examples in
  `inbox.example.yaml`.

### Changed

- `r` inside the Context pane re-fetches that item's context instead of polling
  every source; everywhere else it polls as before.
- The right column is thread, context (when open), draft; `tab` cycles through
  the pane too.
- Internal: the capped command runner takes an optional per-call output cap
  (`max_bytes`) and an optional `cancel` event; every existing caller keeps the
  4 MB default and no cancellation.
- A model name (`summary.model`, `rsvp.model`) may no longer start with `-`.
- `c` closes an open Context pane even when only `summary:` is configured.
- The context and summary threads hand their result to the UI without blocking
  (a call that waited for a UI loop that had already gone would never return and,
  as these threads are not daemons, would keep the process from exiting).

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
  default). The bridge's guards apply (`state/PAUSED`, the daily budget). The
  prompt carries only the tool name and those three values. The result is
  checked, never the model's words: a failure keeps the row and toasts the
  reason, a success hides it (`rsvp` in `inbox-state.json`) and logs it in the
  audit log. A recurring series answers its next instance only, and the modal
  says so; the mark hides that event id only, so the next instance shows again.
  If a calendar snapshot taken 10 minutes or more after the answer still lists
  the event as unanswered, the row returns with a "RSVP not confirmed by
  calendar" hint. New `rsvp:` setting (no default): `tool`, `args` (the tool's
  parameter names for `event_id`, `calendar_id` and `response`), `timeout_s`
  (30 to 300), optional `model`. Without it `X` and `T` say "RSVP not
  configured" and `a` keeps acknowledging an invite as before.
- Save to memory (`m`, any item): a modal shows the source, sender, gist and
  link; after the undo window the configured `save` command runs with `{"v": 1,
  "action": "save", "item": <item>}` on stdin and answers `{"ok", "message",
  "memory_id", "duplicate"}`. The item stays listed and gets a `◆` (local
  `saved` mark). New `save:` setting (no default): `argv`, `timeout_s` (5 to
  120). Without it `m` says "Save not configured". Same process-group, timeout
  and output-cap handling as command sources, except a timeout sends `SIGTERM`
  and kills 5 s later, and says "the save may still complete"; item text
  reaches the command only as JSON on stdin. A command may answer `ok: false`
  with `duplicate: true` (and a `candidate`; `memory_id` is null, the existing
  memory is `candidate.id`): the inbox then asks "Save anyway" (Cancel focused)
  and, once confirmed, resends the same payload with `"force": true`. `ok:
  true` with `duplicate: true` is a success, shown as "Already in memory". The
  duplicate question is a toast ("press m to review"), not a modal that could
  stack on another one. A second `m` while a save is pending says "save already
  in progress". The protocol is in docs/inbox.md.
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
