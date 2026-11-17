# Inbox changelog

Changes to `daemon.py inbox`, its item format and its source protocol. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
versions follow [Semantic Versioning](https://semver.org). The policy is in
[inbox.md](inbox.md#versioning).

## [Unreleased]

## [0.8.3] - 2026-11-17

### Fixed

- **An active alert no longer drops out of the inbox.** Only events inside
  `window_h` were read, and an alert fires one event when it becomes active,
  so a condition active for days was not listed. Every key that
  `state/alerts.json` lists as active is now always shown, using its newest
  alert event whatever its age; with no event on file, a row is made from the
  key and its time. The age rule still applies (urgent while younger than
  `alert_urgent_max_age_h`, `fyi` after). Resolved keys behave as before.
- **Alert rows have a thread.** The detail pane shows the routine, the issues,
  when the alert became active, the newest `ERROR` or `FATAL` line of that
  routine from `logs/run.log` (the last 256 KB are read, once per load; the
  line is cut to 200 characters; a missing log adds nothing) and the hint
  `run `daemon.py status` for details`.
- **Benign run states are never urgent.** An alert whose only issues are
  `overlap`, `interrupted` or `incomplete` is `fyi`, even under a custom
  urgent rule. See [inbox.md](inbox.md#stale-and-recovered-alerts).

## [0.8.2] - 2026-11-17

### Fixed

- **A calendar invite no longer shows twice.** The events stream's calendar
  rows stayed in the list next to, or after, the invite they announce. Ids are
  now compared by series (a recurring instance suffix `_YYYYMMDD[THHMMSS[Z]]`
  is stripped). While the pending invite is listed (and not marked done or
  acknowledged; for one that is, see below), its `New invite` rows and the `Updated` rows about the listed
  instance are hidden and the newest such update's changed fields are added to
  the invite row as a `changed` thread line; `Cancelled` stays, and so does an
  update about another instance. After an RSVP from the inbox, `New invite`
  rows and `Updated` rows about the answered instance not newer than the answer
  are hidden; a later update shows. That mark is ignored once the series has
  moved to another instance, or when a later snapshot still lists the event as
  unanswered. After an answer given in the calendar, `New invite` is hidden and
  updates stay. For a declined meeting, update and cancel rows are hidden too
  (an inbox decline covers its own instance, a snapshot decline the series).
  For an invite marked done or acknowledged, every calendar row about the
  closed instance that is not newer than the close is hidden (the done/ack mark
  now records the invite's event id); later rows show, a cancel included.
  The filter runs when the sources are merged (`collect` and the app's refresh)
  and leaves the cached rows alone. See
  [inbox.md](inbox.md#calendar-rows-that-an-invite-makes-redundant).

## [0.8.1] - 2026-11-17

### Fixed

- **Memory mark on Slack items without `reply_to`.** The match for the
  `◆`/`◇` mark built Slack ids only from `meta.reply_to`, which a source sets
  only where a reply is allowed, so many captured threads showed no mark.
  Without a valid `reply_to` channel the match now falls back to
  `meta.channel_id`, `meta.thread_ts` and `meta.message_ts` (same patterns,
  same exact and thread-level rules). Matching only: `reply_to` stays the only
  send destination, and a valid `reply_to` still wins. A `reply_to` that has
  only a channel borrows `meta.thread_ts` / `meta.message_ts` for matching
  when `meta.channel_id` is the same channel.
- **Arrow keys at start.** When the first poll was still running at start,
  the queue was hidden, lost focus and kept none once the items arrived, so
  up and down did nothing until a mouse click. The queue now takes focus back
  when it is shown with nothing focused (the first item is highlighted as
  before). It does not do so while the terminal is in the background (a poll
  then no longer takes the focus from the filter, the thread pane or the
  draft editor); on regaining focus it takes the focus only if nothing has it.

## [0.8.0] - 2026-11-17

Pending invites say more, the inbox shows what the daemon already saved to
memory, and other people's RSVP answers are hidden by default. No setting is
required: a 0.7.0 `inbox.yaml` works unchanged.

### Added

- **Richer invite items.** The thread of a pending invite now has `subject`
  (the full title), `when`, `from` (the organizer's name, taken from the
  snapshot row first, then the JSON twin), `attendees` (`5 people: Ann Lee,
  Bob Ray, +3 more`; rooms and groups skipped, nameless guests counted but not
  named, `attendees unknown` when the guest list is hidden), `where` (the
  location) and one `conflict` line per overlapping event you are going to or
  organize (accepted, tentative or yours; up to 5, then `+N more`). The gist
  starts with `⚠ ` when there is such a clash. An overlap with another
  unanswered invite is not a warning: it is an `also pending` line and
  `meta.also_pending`. Attendee names and the count leave out declined guests
  and you. New `meta` keys: `attendees` (up to 25 names), `attendee_count`,
  `attendees_known`, `conflicts`, `also_pending`. Names only: an email address, or a name that
  contains an at-sign, never appears in the item.
- **`show_rsvp_replies`** (top-level, `true` or `false`, default `false`): the
  calendar events of kind `rsvp` (other people answering your invites) are no
  longer listed unless it is `true`. They are dropped before the newest row per
  event is chosen, so the event still shows its latest non-RSVP change (`new`,
  `update` or `cancel`) after someone replies.
  New, updated and cancelled invites are not affected.
- **Already in memory.** An item the daemon saved to memory shows the `◆` mark
  (the one `m` leaves with `mark_done: false`) and an `In memory: <id>` line in
  the detail view. The match uses `state/events.jsonl`: `meta.memory_id`, then
  `meta.source_id`, then for Slack items `slack:<channel>:<thread_ts>:<message_ts>`
  (a saved reply) and `slack:<channel>:<message_ts>` from a valid
  `meta.reply_to`; those are exact. When only the thread root
  (`slack:<channel>:<thread_ts>`) is saved the match is thread-level: a dim `◇`,
  a `Thread in memory: <id>` line, and `m` offers "Save anyway". The id found is
  put in `meta.memory_id` (exact) or `meta.thread_memory_id` (thread-level) of
  the listed item, so it reaches the save, context, precheck and custom-action
  commands. Items with no match get no mark.

### Changed

- `m` is labelled "save to memory by hand" (the daemon saves on its own); on an
  item already in memory it toasts "Already in memory (<id>)" and runs nothing.
- Without `save:`, `m` no longer appears in the footer, the help screen (the
  "RSVP / save" section becomes "RSVP") or the command palette.
- Invite thread: the first line is now `subject`, then `when`.

## [0.7.0] - 2026-11-16

"Send" becomes a choice. Some workplaces block headless (non-interactive)
Claude Slack SENDS with a managed security hook while a draft tool and
reactions still work. After you pick `a` (reply) or `n` (nudge) the confirm
screen now offers up to three actions. Settings are new and optional, so a
0.6.0 `inbox.yaml` behaves as before (`send.mode` defaults to `send`); the
confirm screen also gains a "Copy for Slack" button next to Send.

### Added

- `send.mode` (`send` default, `draft` or `both`) and `send.draft_tool` (an
  `mcp__...` tool, the same `args` mapping as `tool`). `send.tool` is no longer
  needed in mode `draft`; `draft_tool` is needed in `draft` and `both`; the two
  tools must differ.
- **Draft in Slack** (`d`, mode `draft` or `both`): the same pinned path as a
  send (`exact_args`, `attempts=1`, `max_calls=1`, the precheck, the 5 s undo
  window) on `send.draft_tool`, pinned to `{channel, text, thread_ts?}` mapped
  through `send.args`; spend logged as `inbox-draft`, audit kind `draft`. A
  success does NOT mark the item done and says "Draft placed in Slack: open it
  with o and press Enter." A reply that contains `attached_draft_exists` says
  "A draft already waits in that conversation; use Copy instead". Failure
  wording never says a message was sent ("no draft placed", "the draft may
  have been placed, check Slack"). Never retried; one Slack write at a time,
  drafts included.
- **Copy for Slack** (`c`, always offered once `send:` or `copy: true` is set
  and the text passes the text checks): purely local (no tool call, no undo
  window), and it needs no destination: with no valid `meta.reply_to`, or a
  channel or group reply without a thread, the confirm screen opens with only
  Copy (and a warning "Draft and Send need a valid thread/DM destination").
  The draft is converted to Slack mrkdwn by `mrkdwn.to_slack_mrkdwn`, which
  only touches unambiguous standard markdown, because the drafts usually are
  Slack text already (a single `*x*` is bold there): `**x**` and `__x__` to
  `*x*`, `~~x~~` to `~x~`, `[text](url)` to `<url|text>` (label escaped) and
  `# heading` to `*heading*` (a `**...**` whose body holds a single `*` is
  left as it is). Single `*x*` and `_x_`, lists, quotes, inline,
  fenced (backtick and tilde) and indented code are left alone. The result goes
  on the clipboard (OSC 52). The item stays. Toast "Copied for Slack: open it
  with o and paste".
- `copy: true` (top level, default `false`): "Copy for Slack" without `send:`.
- `slack_open: {url_template}` and `team: "T..."` (top level): `o` opens the
  template URL for an item with a valid `meta.reply_to`. Placeholders
  `{channel}`, `{thread_ts}`, `{message_ts}`, `{team}`; `{thread_ts}` is the
  thread's timestamp, else the message's, so a thread opens at the thread. The
  template must start with `slack://` or `https://`; `team` must match
  `^T[A-Z0-9]{6,12}$`; `{team}` needs `team:`; the template is printable ASCII
  without spaces. Without the setting, or when the
  template cannot be filled, `o` opens the item's link as before. The confirm
  screen shows the URL `o` will open, and `o` in the screen opens it (the item
  the screen was built for, and audited for it, even if a poll moved the
  selection meanwhile). The text pane is sized to the screen height so the
  title, the destination lines and the buttons stay visible with a long draft;
  a copy-only screen has its own footer line. At
  terminal widths under 100 the buttons wrap into two columns instead of
  clipping.
- The confirm screen: Cancel stays the default; the buttons and keys are
  `d` Draft in Slack, `y` Send, `c` Copy for Slack, `esc` Cancel (a key that
  is not offered does nothing). No new key in the queue.
- Commented examples for all of it in `inbox.example.yaml`
  (`tools/validate_examples.py` checks them).

### Fixed

- `claude_bridge.call_tools` and `complete` resolve `base_dir` to an absolute
  path. A relative `base_dir` made `--settings` relative to the run's own cwd
  (the run directory), which doubled the path, so `claude` exited 1.

### Changed

- `claude_bridge` keeps a fixed `(attached_draft_exists)` tag on a tool error
  whose body names it (never the body), so the draft path can tell.
- `daemon.py inbox --version`, the header and the help overlay show 0.7.0.

## [0.6.0] - 2026-11-16

Real Slack writes and a polish step, all opt-in. Without `send:`, `react:` and
`polish:` in `inbox.yaml` nothing changes, except that `E` and `P` are now the
inbox's own keys (an `actions:` entry that used one of them is refused; pick
another letter, the free ones are `h l p t w x z` and `B D F H J K M N O Q U V
W Z`).

### Added

- `send:` (no default): `{tool, args: {channel, thread_ts, text}, timeout_s
  (30 to 300, default 90), model (default haiku), precheck}`. `a` (reply) and
  `n` (nudge) now send for real when it is set; without it they behave as
  before (a refusal on a real item, a pretend send in `--demo`). The same flow
  as RSVP: confirm modal (Cancel focused), the 5 s undo window (quitting
  inside it cancels), then one worker call, never retried, a row in the audit
  log, a running send counted in the quit guard, results back through the
  non-blocking `_post`. One headless call through the bridge whose only
  allowed tool is `send.tool`, pinned to exactly `{channel, text}` plus
  `thread_ts` when there is one (`exact_args=True`, `attempts=1`). Success is
  a real non-error result (a JSON `ok: false` or an error is a failure); a
  timeout or a run cut off by a sleep says "the message may have been sent,
  check Slack"; a guard stop or a tool error says "not sent". A sent item is
  marked done. Only one send runs at a time. The text is exactly the current
  draft (also after `e` edits) with trailing whitespace removed; it is refused,
  never cut, when it is empty, over 4000 characters ("the text is N characters;
  the limit is 4000; edit it first") or holds a control or bidi-override
  character (editor text is cleaned like any draft). The confirm modal shows
  the destination (`<where> (thread)` or `(DM)`), the real channel id and
  thread timestamp, and the full text, scrollable and line by line as it will
  be sent (the old modal joined lines). Spend is logged as `inbox-send`.
- `send.allow_channel_posts` (default `false`): a reply to a channel or group
  (`C`, `G`) without a `thread_ts` would be a top-level post and is refused
  ("would post to the channel, not a thread"), for nudges too; a DM (`D`)
  needs no thread. `true` allows it (labelled `(channel)`). Checked when the
  modal would open and again when the send runs.
- A write can no longer run twice. `call_tools(..., max_calls=None)` caps the
  allowed calls of each pinned tool in a run (default unchanged for the
  daemon); send, react and RSVP pass `1`. The hook claims one marker file per
  allowed call in the 0700 run directory and denies the next (to whichever
  call asks first, not necessarily the first in the stream); the stream
  re-check judges the cap on results: it fails a run only when more calls
  returned a real result than the cap allows, ignores the error results of
  calls the hook denied, and when nothing confirms the call it raises an
  "unclear" error that reads "may have been sent, check Slack". A real send is never
  reported as "not sent": when one result confirms it and another does not the
  toast says "sent at least once; check Slack", and a plain-text reply that
  reads like an error (and carries no Slack timestamp) is "unconfirmed: may not
  have been sent; check Slack".
- Quitting while a send, nudge or reaction runs: the exit message says it "may
  have been sent, check Slack", the audit row reads "running at quit", and when
  the call ends one line goes to stderr and the row is closed.
- `meta.reply_to = {channel, thread_ts?, message_ts?}`: the documented source of
  a send's destination (`channel` `^[CDG][A-Z0-9]{8,12}$`, timestamps
  `^\d{9,11}\.\d{6}$`, a wrong format makes it invalid; never read from item
  text). An item without a valid one says so and cannot be sent to.
- `send.precheck: {argv, timeout_s (1 to 30, default 10)}`: an optional
  read-only command run just before the confirm modal (behind a "checking"
  modal, `esc` cancels). Stdin `{"v": 1, "action": "precheck", "item", "text"}`
  only, never argv; stdout `{"v": 1, "ok": true, "warn": str|null}`. A `warn`
  shows prominently in the modal (Cancel stays the default); a failure or timeout
  shows "couldn't check for a newer reply" and never blocks the send.
- `react:` (no default) and `E`: `{tool, args: {channel, timestamp, name},
  emojis (1 to 9 names, `^[a-z0-9_+\-]{1,64}$`, default `eyes`,
  `white_check_mark`, `+1`, `pray`), timeout_s, model, mark_done (default
  true)}`. Emoji picker, confirm, undo, then one pinned call (`exact_args`,
  never retried) on `meta.reply_to.message_ts`, else `thread_ts`. A valid
  `meta.suggested_reaction` that is one of the `emojis` is preselected in the
  picker (still confirmed); one outside the list is ignored unless
  `react.allow_suggested: true`. Spend is logged as `inbox-react`.
- `polish:` (no default) and `P`: `{mcp_config, tone_tool, tone_args, model
  (default sonnet), timeout_s, max_budget_usd (default 0.05), cache_h (default
  24)}`. One read-only tone call pinned to `tone_tool` and `tone_args` on the MCP
  servers of `mcp_config`, cached on disk in `state/inbox-polish/tone.json`
  (0600 in a 0700 directory), then a no-tools, one-turn rewrite
  (`claude_bridge.complete`, usage tag `inbox-polish`) that keeps meaning,
  facts, names, numbers and language. The draft, up to the last 10 thread
  messages and the tone text are data between random-token markers. The
  rewrite also passes `--strict-mcp-config` with no config file (no MCP server
  loads; `complete(..., strict_mcp=True)`, default off for other callers). A
  modal shows old and new; accept replaces the draft, edit opens the editor with
  the new text, cancel (the default) changes nothing; both re-check that the
  draft did not change while the review was open. Polish never sends.
- Bridge: `call_tools(..., mcp_config=None)`. A path (absolute, an existing
  regular file you own with no group or other access, up to 1 MB, opened with
  `O_NOFOLLOW` and judged with `fstat`) adds `--mcp-config <copy>
  --strict-mcp-config`, where the copy is a 0600 file in the run's private
  directory written from the bytes read from that same checked descriptor (no
  check-then-use gap); the allowlist hook and the stream re-check still
  restrict the run to the pinned tools. The default leaves the argv unchanged.
- Mail items from the `events` source show more. The daemon records, for Gmail
  captures, `details.mail_from` (display name only, never an address, 80
  characters; a value with an `@`, also the fullwidth one, is dropped),
  `mail_subject` (200), `mail_snippet` (300; Gmail's own snippet, a short
  excerpt of the body; only when the source gives one: the `gws` path does
  when its read has it, the connector path does not; it rides beside the item
  and is not written to note frontmatter) and `mail_thread_id`, all cleaned
  plain text. The
  item gets `sender` = `mail_from`, `where` = the subject, a one-message thread
  from the snippet and a Gmail thread link from `mail_thread_id` and
  `gmail_account`; the gist is unchanged and an event without these fields
  looks as before.
- `meta.memory_id`: the `events` source copies an event's `memory_id` (when it
  matches `^[a-z0-9][a-z0-9-]{0,200}$`) into the item's meta, so a context
  command can link the captured entry.

### Changed

- `E` and `P` are reserved keys (see above). `ConfirmSend` scrolls and shows
  the text line by line.

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
  the first message of a thread of two or more (not when the item has a
  `thread_note`). `v` toggles to oldest first and
  back (the queue is not touched); a new `thread_order: newest | oldest`
  setting (default `newest`) is the starting order, and the toggle is saved in
  `state/inbox-state.json` (`prefs`), so a restart keeps it (the saved choice
  wins over the setting once made). The item header, `meta.thread_note` and the
  RSVP hint stay on top. `v` is now a built-in key, so it is not available to
  `actions:`. Footer, help overlay and command palette list it.

### Changed

- A choice `id` of an `actions:` command is an opaque handle: it is handed back
  to `prepare` and `run` byte for byte (never cleaned or cut). A row whose id is
  not a string of 1 to 200 characters, or holds a control character, is skipped;
  only `label` and `detail` are cleaned for display.
- The confirmation modal of the write actions (RSVP, save, actions) keeps its
  label column to a width (8 to 16 cells, a longer label is cut with an
  ellipsis), wraps each line of a multi-line value, and scrolls when the rows
  are taller than the box (`down`/`up`, `j`/`k`, `pagedown`/`pageup`; Cancel
  stays the default).
  The label column plus the value always fit the box's inner width (61 cells).
- A bad `choices` answer names the field that is wrong (for example
  `choices[0].label must be a string`).
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
