# Inbox changelog

Changes to `daemon.py inbox`, its item format and its source protocol. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
versions follow [Semantic Versioning](https://semver.org). The policy is in
[inbox.md](inbox.md#versioning).

## [Unreleased]

## [0.13.5] - 2026-11-25

An easier due-date picker for tasks (`s` in the Tasks view). Nothing changes
for a source or a settings file.

### Added

- **Today** is the first choice of the `s` picker. It sets the due date to
  today (the date only).
- **Next work day** in the picker, when `work_week:` is set: the first day
  after today that is a work day (`start` and the `days - 1` days after it).
  With `sunday` and `5`, on a Thursday it is Sunday. It is left out when it is
  tomorrow, so the same date never shows twice in a row. `work_week.days` is
  read for the first time.
- **Typed due dates** in Pick date: `today`, `tomorrow`, a weekday name or its
  3-letter form (`thu`: the next such day; today's own weekday means a week
  out), `d/m` or `d.m` (`25/11`: this year, or next year when it has passed),
  `+3` / `+3d` (days, 1-365) and `+2w` (weeks, 1-52), next to `YYYY-MM-DD`.
  The "Not a date" message lists the forms. A day before today is still
  refused.

### Changed

- The picker's choices now start at Today: `1` Today, `2` Tomorrow, then Next
  work day (with `work_week`), Next Sunday or Next week, +1 week, Pick date
  and No date. The number keys follow that order, so `1` is no longer
  Tomorrow.

## [0.13.4] - 2026-11-25

Word jumps in the text fields, a calendar-week view of THIS WEEK, and a calmer
due-date picker. Nothing changes without the new optional `work_week:`.

### Added

- **`work_week:`** in `inbox.yaml`, `{start: <weekday>, days: <1-7>}` (the same
  shape as the daemon's `memory.work_week`; `start` is required, `days`
  defaults to 5 and is only stored for now). With it set, `THIS WEEK` in the
  Tasks view ends before the next `start` day, so a task due next Sunday is
  `LATER`, the due cell names a weekday only inside `THIS WEEK`, and the `s`
  picker's `Next Sunday` becomes `Next week` (the next `start` day). Unset
  keeps the rolling 7 days.

### Fixed

- **Word keys in every text field** (the `/` filter, the row menu filter, the
  snooze, note, due-date and task fields, and the draft editor):
  `alt+left` / `alt+right` jump a word, `alt+shift+left` / `alt+shift+right`
  select by word, `alt+backspace` deletes the word to the left and
  `alt+delete` the word to the right. `ctrl+left` / `ctrl+right` work as
  before.
- **The cursor stays in place when you set a due date** (`s` in the Tasks
  view). The row moves to its new section; the cursor stays at the same place
  in the list, on the row that is there now, even when a refresh lands while
  the picker is open. Undo puts it back on the task.
- **Pick date refuses a day before today.** The screen stays open and says so,
  like it does for a bad format. A date in another year shows its year in the
  picker.

## [0.13.3] - 2026-11-24

Start a task from any row. `t` opens a small modal, and your own command (the
task launcher) drafts and starts a task session. The inbox only talks to that
command; the roles come from your config, and it knows no tools or sessions of
its own. Nothing changes without `task_launcher:` in `inbox.yaml`, except that
`t` is now a reserved key.

### Added

- **`task_launcher:`** in `inbox.yaml`: `argv`, `timeout_s` (launch, default
  30), `draft_timeout_s` (default 90), `roles` (required, chips `1`-`9`),
  `models` and `efforts` (chips). One command, JSON on stdin, one JSON line on
  stdout, no shell, item text never in argv. See the [Task launcher
  protocol](inbox.md#task-launcher-protocol): a read-only `draft` call and a
  `launch` call that runs once and is never retried.
- **`t` on a row** opens the modal at once, with local defaults: a slug name
  (`-2` when taken), a role by the row (a pull or merge request link, a decision
  task, anything else), model and effort chips (`m`, `e`; the first stop is the
  launcher's default), a goal line from a template, a collapsed acceptance line
  (`a`) and, when the draft returned them, a persona line and brief chips (`t`).
  A background `draft` call fills only the fields you have not touched; `enter`
  works before it returns (and stops it); `esc` cancels. `enter` queues the
  launch with the 5 s undo window; a failure is a toast with the first line of
  the launcher's message. Without `task_launcher:` `t` says `no task launcher
  configured`. A row that cannot start a task (a digest, saved, review or agent
  row, an invite, an alert, a row whose task still runs) says why.
- **A badge on the row** a task came from, `▶ <name> · <role> · <state>`, from
  the agent row whose `meta.source_key` is the row's `id`; green when the task
  has finished (`meta.state` `completed`, or `meta.bucket` `done`).
- **Row menu**: `Start a task…` (`t`), `Go to task` (`>`, also bound on the queue; the row in the Agents
  view), and, for a finished task with a report link on its agent row, `o`
  "Open task report" and `<` "Open the source link". `o` on such a row opens the
  report.
- The footer shows `t task` (the seventh and last slot), the help has a
  "Start a task" section, the palette has "Start a task…" and "Go to task", and
  `inbox.example.yaml` has a commented `task_launcher:` block.
- **Unclear launches.** Only an explicit `ok: false` is a failed launch. A
  timeout, a non-zero exit or an unreadable reply is a warning, "launch result
  unclear; it may have started (check the Agents view)". A launched row refuses
  a second `t` until its card shows or 5 refreshes pass.
- Extra draft fields are passed through to launch: keys of a `draft` answer's
  `fields` that the modal does not show are kept (validated, capped) and sent
  back unchanged in the `launch` call.
- Badges work without `task_launcher:`: any `meta.kind: agent` row with a
  `meta.source_key` badges that row (and, when finished, redirects `o` to its
  http(s) link).

### Changed

- **`t` is a reserved key.** An `actions:` entry on `t` is now refused at
  startup with a message that names the clash; give it another key (it stays in
  the row menu). The free action keys are `h l p z` and `B F H J K N O Q U V W
  Z`.

## [0.13.2] - 2026-11-24

A sessions strip and an Agents view: what your agent sessions are doing, on
screen next to the queue. Both are generic: they show what any command source
sends. Nothing changes for a source that sends neither `status` nor
`meta.kind: agent` rows.

### Added

- **`status` in a source's `list` reply** (optional): up to 12 lines
  `{"text", "level": "ok"|"warn"|"alert", "item_id"?}`, each cleaned to one
  line and cut at 160 characters. A bad `status` is dropped and never touches
  the items; unknown keys are ignored. See
  [Status lines](inbox.md#status-lines).
- **The sessions strip**, pinned above the queue in every view: the status
  lines of every source that sent some, the level choosing the colour. It
  shows 4 lines (`strip_lines:` in `inbox.yaml`, 1 to 12), then
  `+N more · 6`, and is hidden when no source sends a line. **Show / hide
  sessions strip** in the `ctrl+p` palette turns it off and on (kept in
  `prefs.sessions_strip`); it has no key of its own.
- **View `6` Agents**: every `meta.kind: agent` row, grouped `NEEDS YOU`,
  `WORKING` and `DONE` (`meta.bucket`, else the row's section), with `where`,
  the age, and the marks `live` / `no session` (`meta.live`) and `old rules`
  (`meta.rules_stale`). The full row menu and the keys work as on any row. A
  row that a Feed rule mutes stays hidden here too. In this view the strip
  shows all its lines instead of `+N more · 6`.
  `6` is a view key like `1`-`5`; the help, the Health footer and the palette
  ("Go to Agents") follow.

### Changed

- **Working and queued agent rows leave the Inbox.** An agent row that is not
  waiting for you (a `fyi` row) is now listed in Agents only: no longer in the
  Inbox (even when a Feed rule promotes it) and no longer in the Feed. An agent
  row that waits for you (`urgent` or `needs_reply`) stays in the Inbox, and is
  in Agents too.

## [0.13.1] - 2026-11-23

`d` on a Slack ask or a waiting row can ask what happened before it closes, so
a source can keep the outcome. It is opt-in per source; nothing changes for a
source that does not ask for it.

### Added

- **`close_outcome: true` on a command source** (needs `close` in its `acts`).
  With it, `d` on a Slack ask (I OWE) or a waiting row opens an `Outcome:`
  prompt. An empty answer is refused, `esc` cancels and closes nothing. After
  the usual 5 s undo window the source gets `close` with
  `{"outcome": "<one line, up to 300 characters>"}`. Every other row (FYI,
  feed, RSVP, alert, review, digest, saved, agent card, task, decision) keeps
  its `d`. A source without the flag keeps the instant `d`. See
  [Close with an outcome](inbox.md#close-with-an-outcome-d).
- **A model draft of the outcome.** When `summary:` or `polish:` is set, one
  background no-tools bridge call (the data markers of Draft for me, an 8 s
  timeout, logged as `inbox-outcome`) fills the open prompt with a one-line
  suggestion. It never overwrites text you typed, and a failure or a timeout
  leaves the line empty. Nothing is stored or sent by it.
- The row menu names the key "Done with outcome…" on such a row, and the `?`
  help table says so.

## [0.13.0] - 2026-11-23

Simple triage. The inbox had about 35 single-letter keys, a footer of 13
hints on a reply row and a command palette of about 45 commands whatever row
was selected. Four verbs now carry the day (`a`, `d`, `s`, `o`) and `enter`
lists the rest for the row you are on. No key was removed and no key changed
what it does: every old key works as before.

### Added

- **A row menu (`enter`).** A popup in the style of which-key that lists only
  what the selected row can do now, each entry with its key and a clear label.
  One press of an entry's key runs it (it calls the same method as the key
  itself); the arrows and `enter` pick the highlighted entry; `/` filters by
  text (the first `esc` clears the filter, the next closes the menu). Keys
  that mean different things on different rows get their own labels: on a todo
  `D` is "Done with outcome…", on a Slack ask it is "Draft for me", on a REVIEW
  row it copies a save hint. The menu lists your `actions:` of `inbox.yaml`,
  relay (`!`), react (`E`), polish (`P`), edit, nudge, RSVP decline and
  tentative, waits on (`w`), mute (`x`), save to memory (`m`), copy, context
  and summary, the Feed's less like this and rules, and undo while something
  waits out its window; each one only when it is configured and valid for the
  row. Nothing that is off or does not apply is listed. See
  [Keys](inbox.md#keys).
- **A "This item" group in the command palette (`ctrl+p`).** With nothing
  typed, the commands of the selected row (the same entries as its menu) come
  first, then the rest. A typed query matches both. Settings and rare toggles
  (Hebrew order `b`, thread order `v`, audit log `L`, copy section as Markdown,
  theme) are in the palette and keep their keys.
- **The `?` help opens with the verbs.** `a`, `d`, `s`, `o`, `enter` and `u`,
  then a short table of what `a`, `d` and `s` mean on each kind of row (Slack
  ask, waiting, RSVP, todo, decision, agent card, FYI, alert), then "Power
  keys (also in enter)". The scroll keys and the wheel work as in 0.12.4.

### Changed

- **`enter` on a Feed row opens the row menu; `tab` moves to its details.**
  Until now `enter` on a Feed row moved the focus to the thread pane. `tab`
  does that (it already cycled the panes; it is now the documented key). A
  mouse click on a Feed row still moves the focus to the details.
- **The footer shows at most seven hints for the selected row**, in this
  order: `a <label>` (`send`, `accept`, `nudge` or `ack`; left out when the row
  has no approve action), `d done`, `s later`, `o open` (when there is
  something to open), `enter more` and `? help`. The context, summary, react,
  polish, waits on, relay, draft for me, thread order, custom-action, copy,
  save, view and group hints left the footer; their keys work as before and
  each is in the row menu (the `1`-`5` and `[`/`]` keys are in the help). A
  task row shows `d` and `s` only when its source lists the act. The footer of
  a focused pane, of the filter and of an empty queue is unchanged apart from
  being shorter where it was long.

## [0.12.4] - 2026-11-22

### Fixed

- **The mouse wheel did not scroll the `?` keyboard reference or a long
  confirmation.** The box is narrow; the rest of the screen is the dimmed
  backdrop. The wheel scrolled only with the pointer over the text or its
  scrollbar. Over the backdrop, the border, the padding or a title it did
  nothing, and on a wide terminal that is most of the screen. The help screen
  and the send and write confirmations now take any wheel event and scroll
  their text. The keys were not affected.

## [0.12.3] - 2026-11-19

### Added

- **Remind me on a task (`m`).** The Tasks detail card did not say enough to
  know what a task was about. `m` (or `Remind me` in the command palette)
  fills the Detail pane with where the task came from (each source id as
  `Slack thread`, `meeting recording`, `memory entry`, ...), its date, the
  people, the original words (quoted, up to six lines), the last activity
  (`meta.last_activity`, `meta.updated` or `meta.last_seen_ts`) and the
  related items from the existing context command (a worker, a loading line,
  the Context pane's cache). Anything missing says so (`source not recorded`).
  Read-only; `m` or `esc` closes it. On other rows `m` still saves to memory.
  See [Remind me](inbox.md#remind-me).

### Fixed

- **Stacked header rows in the terminal scrollback.** Scrolling above the
  inbox in iTerm2 showed many copies of the header, the status bar and the
  pane titles, one per resize. Textual draws a frame row by row with a `\n`
  after each row but the last; when the terminal gets shorter, a frame built
  for the old height is still in flight, its extra rows land on the new
  bottom row, and the `\n` there scrolls the alternate screen. iTerm2's
  default "Save lines to scrollback in alternate screen mode" keeps every
  line that scrolls off the top. The app was always full screen (no inline
  mode, nothing written to stdout before or after the alternate screen). The
  driver is now wrapped so each `\n` becomes cursor-down plus column 1, which
  moves the same way but stops at the bottom row; a stale frame is painted
  over by the resize repaint. A regression test runs the demo on a pty that
  shrinks and grows and checks that no linefeed is written.
- **An I OWE item's Thread pane leads with what you owe.** It opened on
  `<person> waits on you`, the thread-in-memory line and then the thread from
  its first message, often your own old root post, so the message you owe a
  reply to was far below. The pane now starts with a `YOU OWE` block: who
  waits and since when, the message that creates the obligation (the first
  substantive message after your last reply from the item's sender, matched
  loosely by name, else from someone else, whose name then heads the block;
  a short nudge such as `?` or `any update` is skipped and noted as
  `+ nudged 2d later`; the gist when there is none), quoted and cut at four lines, and
  `To do: ...`. The action is the first of `meta.task`, `meta.todo` and
  `meta.ask` that is set, else `RSVP` for an invite, else the drafted reply,
  else `acknowledge` for an ack-only row, else `Reply to <person>`. The full
  thread follows under a dim `thread` line; the list rows are unchanged. See
  [Thread pane of an I OWE item](inbox.md#thread-pane-of-an-i-owe-item).

## [0.12.2] - 2026-11-18

### Added

- **Near-duplicate captures show as REVIEW rows.** When the memory store
  refuses a capture as a near-duplicate, the daemon's `duplicate` event is now
  a row in the Inbox's new `REVIEW` group (listed only when there is one):
  `Not saved: near-duplicate of <matched title or id>`, with a detail pane of
  the skipped title, source id, matched entry and any owner actions not filed.
  `d` dismisses it (a local mark). `D` copies a command-line hint (a template
  for the store's `add --force-new`) and saves nothing: the `save:` command
  would receive the review text, not the captured content, so it is not used.
  Rows age out with the events window and dedupe per source id (newest wins)
  and by event id. A `duplicate` event of Gmail, Zoom or Mila used to show as an ordinary
  mail or recording row; it now shows here. See
  [Review rows](inbox.md#review-rows).

### Fixed

- **The Tasks view (and the thread pane) kept snapping back to the top.**
  Every redraw of the same item (a refresh that changed a row, a terminal
  resize) drew the detail card or thread again and scrolled it home, so a
  long card could not be read. The pane now keeps its scroll position for the
  same item and starts at the top for another item or the other thread order.
- **A redraw no longer moves the list.** Every redraw (a refresh about every
  30 s, a resize) rebuilt the queue and snapped its scroll to the cursor or
  the top, so a list scrolled with the wheel or a half-read page jumped. The
  viewport now stays where it was; only a hidden cursor row is brought into
  view, with the least scrolling. In the thread pane (newest message first) a
  new message on top no longer pushes the reader's place down.
- **Task footer hints.** A task row lists `tab card`; with the card focused the
  footer says `j/k scroll` and `esc queue`.
- **Flaky teardown.** `context_tick` could raise `NoMatches '#context-body'`
  while the app was closing; the render now returns when the pane is gone.
- **Queue paging.** PgUp near the top cleared the cursor (the page landed on a
  group header); it now stays on the first row. Home, `g` and PgUp to the
  first row also show that row's group header (and reach the top when the
  cursor is already there), and resizing the terminal
  shorter keeps the cursor row in view.

## [0.12.1] - 2026-11-18

Mostly a test and CI patch; one fix to the `?` screen.

### Fixed

- **The `?` keyboard reference scrolls.** The reference is taller than most
  terminals, but nothing inside it took focus, so only the mouse wheel moved
  it. The body is now a focused scroll container: j/k, the arrow keys,
  PgUp/PgDn, Home/End and the wheel all scroll it; esc and `?` still close.
- **Flaky Textual tests.** The tests waited for state with a fixed
  `pilot.pause(N)` and then asserted, so a slow worker, timer or layout pass
  failed the CI `inbox-ui` job at random (for example the thread pane scroll
  after a toggle). They now wait for the state itself with `wait_for` or
  `until`; a short pause stays only where a test checks that something does
  not happen. Undo windows that a test must act inside are long, so a slow
  machine cannot close them early.

### Changed

- **Faster CI.** The `inbox-ui` job runs in 4 shards (the module list and its
  balance are in `tools/inbox_ui_shards.py`, which also fails CI when a new
  inbox test module is in no job) and is skipped when a pull request changes
  nothing the inbox imports. The `inbox-ui` check still always reports.

## [0.12.0] - 2026-11-18

The Tasks view: a command source can list open tasks, and the inbox shows,
sorts and acts on them. Opt-in: a source whose items are not tasks, and a task
source with no `acts:`, change nothing else.

### Added

- **Task rows.** An item with `meta.kind: "task"` (and `task_type` `todo` or
  `decision`, `due`, `date`, `people`, `evidence`, `source_ids`, and the item's
  own `link`) is a task. Task rows never show in the Feed, Waiting or the
  Inbox's URGENT, I OWE and NUDGE DUE.
- **View 3 Tasks** replaces the placeholder: OVERDUE, DUE TODAY, THIS WEEK,
  LATER, DECIDE (every decision) and NO DATE, empty sections hidden, each
  sorted by due date then creation date. The tab shows how many tasks are
  overdue or due today (`3 Tasks 4`). "Today" is the local date, recomputed on
  every refresh. Rows show a `☐` or `◇` glyph, the title, people, a due cell
  (`today`, `2d late`, `Thu`, `21 Nov`, `-`) and the age; overdue rows are
  drawn like urgent ones. The detail pane shows type, due, created date,
  people, evidence and source ids; `o` opens the link or says "no link".
- **Inbox `DUE TODAY`** after NUDGE DUE: the overdue and due-today tasks
  (shown only when there are some).
- **Task keys** on the acts protocol (undo window, pending retry, ops log): `d`
  closes a todo at once and asks for the decision (required) on a decision; `D`
  asks for an outcome and closes a todo; `s` picks a due date (Tomorrow, Next
  Sunday, +1 week, a validated `YYYY-MM-DD`, No date); `x` drops with an
  optional reason. New acts `drop` and `due` for `acts:` (the inverses are
  `reopen` and `due` with the previous date, so `u` in `L` undoes them). A
  source with no `acts:` is read-only.
- `memory` is a valid item `source` (a generic label and glyph for items that
  come from a memory store).
- `tests/inbox_fake_source.py` lists task items and `tests/test_inbox_tasks*.py`
  cover the sections across midnight, each key path, the due inverse, the
  Inbox section, the tab count and a read-only source.

## [0.11.0] - 2026-11-18

### Added

- **`open_with`.** An optional top-level list of `{host, app}` rules in
  `inbox.yaml`. `o` opens an `https` URL whose host matches a rule (`fnmatch`,
  case insensitive, first match wins) in that macOS app with
  `/usr/bin/open -a <app> <url>` (argv list, no shell, 5 s timeout), instead of
  the default browser. The URL is still the one `open_target` chose. A non-zero
  exit, a timeout or an `OSError` falls back to the browser, and the toast says
  why; other platforms skip the rules silently. A URL with userinfo or a
  backslash in its authority never matches, and `slack://` and `http://` URLs
  never go to an app. The open runs in a worker thread, not on the UI loop.
  `host` is a glob of hostname characters and `*`; `app` matches
  `^[A-Za-z0-9 ._-]{1,64}$` and is not only dots or spaces; any other key is a
  settings error. Off by default. See [inbox.md](inbox.md#draft-and-copy).

## [0.10.0] - 2026-11-17

Comms control: the inbox can tell a command source what you did with a row,
and write a reply for you. Everything here is opt-in. A source with no
`acts:` behaves exactly as in 0.9.0 (a test pins it).

### Added

- **Acts.** A `command` source may list `acts: [close, snooze, mute, waits_on,
  relay]` in `inbox.yaml`. After the 5 s undo window the inbox calls
  `<argv> act` in a worker (never on the UI thread) with
  `{"v":1, "op_id", "action", "id", "meta", "args", "seen"}` and reads
  `{"ok", "message", "shadow", "conflict"}`. `ok` keeps the local mark (the
  status says `✓ synced`, or `shadow` when the source only logged the op); a
  `conflict` (the item has a newer message) puts the local mark back with a
  toast and is not retried; any other failure (ok false, a timeout, a bad
  reply) keeps the mark, stores the op in `state/inbox-ops-pending.jsonl` and
  retries it on refresh, at most every 5 minutes, with the **same `op_id`**
  (the source must be idempotent on it). The pending file holds at most 50 ops
  and drops entries older than 7 days, with a toast. `u` within the undo
  window cancels before anything is sent. See [inbox.md](inbox.md#acts).
- **`d` and `s` also tell the source** (`close`, `snooze` with the chosen
  time) on a row whose source lists the act. **`w`** picks who the next move
  is with (Me, a person in the thread, Nobody (FYI)); **`x`** on such a row
  adds "Mute this conversation: 1 day / 1 week / untrack" to the mute picker
  (the Feed rule choices stay first, and unchanged); **`!`** relays the row to
  your work session with an optional one-line note. Rows the views make (digest
  and saved-to-memory rows) never send acts.
- **`D` drafts a reply for you** from the thread: one no-tools bridge call
  (the model, timeout and budget of `summary:` or `polish:`) that fills the
  Draft pane, is kept per item in the inbox state (an unchanged thread shows it
  again after a restart) and never sends. It does not overwrite a draft the
  row already has.
- **`n` with no stored draft** on a row of an acts source falls back to the
  new `nudge_template` setting (default `Hi {first}, any news on {topic}?`),
  through the same confirm and send path (draft mode stays a Slack draft).
- **`done_reaction`** (new setting, off by default): after the undo window, `d`
  on a row with its own Slack message also adds that emoji through the existing
  `react:` path. It needs `react:`. It uses `reply_to.message_ts` only (never
  the thread root, unlike `E`), and with acts on it is sent only after the
  source answered `ok`: a shadow answer, a failure kept for a retry or a
  conflict skip it with a toast. Quitting drops it.
- **Health strip**: `N not synced` while ops wait for a retry, and `shadow`
  when the last act reply said shadow.
- **`L` is the ops log** when any source lists acts: time, action, item and
  result of the ops sent, from `state/inbox-ops-log.jsonl` (capped at 500
  rows). `u` on a `close` or `mute` row sends the inverse (`reopen` /
  `unmute`) after a confirm, once per op; `a` shows the session audit log. Without acts `L`
  is the session audit log as before.
- `tests/inbox_fake_source.py`: a fake command source that records requests
  and answers ok, shadow, conflict or failure as configured.

- The pending op is written **before** the `act` call (a kill during it leaves
  the op for a retry with the same `op_id`) and the files are fsynced with their
  directory; a newer op for the same item replaces its older pending ones; a
  conflict on a retry leaves a mark you made since alone; ops of a source no
  longer configured are not counted in `N not synced`; an op that cannot be
  stored is reported as lost.

### Changed

- `w` and `!` on a row whose source lists no acts now toast "Acts are not
  enabled for this source" (they did nothing before; the other keys are
  unchanged).
- `w` and `D` are now the inbox's own keys (and `!`, which is not a letter):
  `w` and `D` are no longer free for `actions:`. The free letters are
  `h l p t z` and `B F H J K N O Q U V W Z`.
- `sources.run_command_act` (which no code called until now) and
  `items.parse_act_response` carry the optional `shadow` and `conflict` flags
  (both default to false); `items.parse_act_request` also accepts the acts and
  the optional `op_id`, `args` and `seen`.
- Quitting inside the undo window of a `d`, `s` or mute still commits the local
  mark; the op is kept in the pending file instead of being sent (quitting never
  waits for a source) and a done reaction is dropped. A `w` or `!` is cancelled
  on quit, like a send. A running `D` draft or ops-log undo asks before
  quitting, like a polish; the quit message says when a done reaction may
  already have been posted.
- `update_hints` no longer fails on a message that arrives during teardown.
- The help screen, the footer hints (for rows whose source lists acts) and the
  command palette describe the new keys.

## [0.9.0] - 2026-11-17

The inbox is organised by whose move it is: five views on the keys `1` to `5`
and a one-line health strip under the tab bar.

### Added

- **Five views.** `1` Inbox (URGENT, I OWE, NUDGE DUE), `2` Waiting (open asks
  by person, bookmarks last as TRACKING), `3` Tasks (a placeholder: tasks
  arrive in 0.11; the tab keeps the keys stable), `4` Feed (every `fyi` row by
  routine) and `5` Health. A view is a filter and a grouping of the same
  items, so selection, marks (done, snooze, ack) and the 5 s undo work the same
  in every view. Rows show their direction: `← Alice` (Alice waits on you) and
  `→ Alice` (you wait on Alice). The header is now a tab bar with a count per
  view; Health shows how many alerts need you.
- **Health strip and Health view.** The line under the tab bar shows one cell
  per routine group (`✓` ok with the age of the last good run, `⚠` attention,
  `✗` failing, `◌` busy), the day's spend against its cap, and optional
  heartbeats. The Health view lists what needs you (an alert's `cause`,
  `last_error`, `remedy` and `since` when the daemon provides them), a
  routine table (state, schedule, last run, last capture, note), the
  connectors, the spend and the heartbeats. Both read the same code as
  `daemon.py status`, read-only, in a worker thread every 30 s, cached by the
  mtimes of `logs/run.log`, `state/alerts.json` and
  `state/claude-usage.jsonl` (see [inbox.md](inbox.md#health)).
- **Feed rules.** `feed.window_h` (default 24) and `feed.rules` in
  `inbox.yaml`: the first matching rule decides `show`, `digest` (one row per
  routine and day), `hide` or `inbox` (promote the row into the Inbox view).
  In the Feed, `x` mutes a row (this kind from this routine, this routine, or
  titles like it; for a day, a week or always), `-` is "less like this" (a
  digest instead of a hide) and `M` lists the rules from the file and the ones
  made in the UI, which can be switched off or removed there. UI rules are
  saved in `state/inbox-state.json` (prefs), never in `inbox.yaml`. See
  [inbox.md](inbox.md#feed).
- **`health.heartbeats`** in `inbox.yaml`: files whose mtime says a process is
  alive, each with `stale_min` and an optional `hours` window such as
  `sun-thu 09:00-19:00` (outside it a stale file is not a warning).
- **Sheet events** (`source: sheet`, the events of a sheet-watching routine)
  now show, as `sheet` rows with the `▦` glyph, grouped under their routine in
  the Feed. `sheet` is a new value of the item `source` field.
- **Events carry `meta.routine`, `meta.title` and, for calendar rows,
  `meta.starts`** (read by the Feed rules). Alert rows carry the daemon's
  optional `cause`, `last_error`, `remedy`, `since` and `connector` in `meta`.

### Changed

- **Keys.** `1` to `4` no longer jump to a section: they switch the view (and
  `5` is Health). Section jumps are now `[` and `]`, which move to the previous
  and next group of the open view (a section in the Inbox, a person in
  Waiting, a routine in the Feed). New keys: `x`, `-` and `M` in the Feed.
  `x` and `M` are no longer free for `actions:` (a configured key is refused as
  for any other built-in letter). `S` is now a shortcut to the Feed, filtered
  to saved-to-memory; `f` cycles the routine filter in the Feed (the source
  filter elsewhere); `Y` copies the group the selected row is in.
- **The Saved view is part of the Feed**, as the routine `saved to memory`
  (captures that no queue row already shows). The saved rows are read with
  every refresh, so the change-driven refresh of the old screen is gone.
- **The Inbox shows what needs a move from you**: `fyi` rows no longer sit
  under it; they are in the Feed (older than `feed.window_h` they age out of it,
  `A` shows them all; only rows of the events source age out, a `command`
  source's rows stay while it lists them, and an active alert never ages out or
  hides). A waiting row whose nudge is due is in the Inbox as well as in
  Waiting.
- **The Health badge** counts the alerts that need you and the failing routine
  groups that no such alert names yet.
- **Saved-to-memory and digest rows take few keys** (`o`, `d`, `s`, `y`; a digest
  also `x` and `-`); the rest says why it does nothing. With a filter on,
  `d` and `s` on a digest row reach only the rows the filter shows.
- Rules made with `x` and `-` live in `prefs.feed_rules` of the state file. A
  downgrade to 0.8.x ignores them and drops them the next time it writes the
  file.
- The alert thread prefers the daemon's own `cause`, `last_error`, `remedy`
  and `since` (shown in local time) and falls back to the 0.8.3 logic (the
  time in `alerts.json`, the newest log line) when they are absent.
- The help screen and the footer hints describe the views.

### Fixed

- A flaky test of the send flow that read the pending list right after a
  fixed pause now waits for the condition.

## [0.8.3] - 2026-11-17

### Fixed

- **An active alert no longer drops out of the inbox.** Only events inside
  `window_h` were read, and an alert fires one event when it becomes active,
  so a condition active for days was not listed. Every key that
  `state/alerts.json` lists as active is now always shown, using its newest
  alert event whatever its age; with no event on file, a row is made from the
  key and its time. The age rule still applies (urgent while younger than
  `alert_urgent_max_age_h`, `fyi` after). An active row has one id per
  episode, `events:alert-active:<key>@<since>`, so a done or ack mark survives
  the event file's rotation and a re-fire (a new time) shows it again. An event
  older than the episode is not used. Resolved keys behave as before.
- **Alert rows had an empty detail pane.** An alert row now has a thread: the
  routine, the issues, when the alert became active (local time), the newest
  `ERROR` or `FATAL` line of that routine from `logs/run.log` that was written
  since the alert became active (shown with its age; the last 256 KB are read,
  once per refresh; the line is cut to 200 characters; a missing log adds
  nothing) and the hint `run `daemon.py status` for details`. The entries read
  top-down in both thread orders (`v`).
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
