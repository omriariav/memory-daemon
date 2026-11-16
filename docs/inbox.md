# Unified inbox

`python daemon.py inbox` is a keyboard-first terminal inbox over several
sources. This page is the contract: the item format, the source-command
protocol, the built-in `events` source, local state and settings.

Version: see [docs/inbox-changelog.md](inbox-changelog.md); `python daemon.py
inbox --version` prints it.

The inbox reads and triages. Replies are not sent: `reply` and `nudge` are part
of the format but the UI refuses them until phase 2. Version 0.3.0 adds the
first two writes, both opt-in: [RSVP on pending
invites](#rsvp-on-pending-invites) and [save to memory](#save-to-memory).
Everything else is a local mark. Version 0.4.0 adds two read-only helpers:
the [Context pane](#context-pane-c) and an [AI summary](#ai-summary-i).

## Install and run

```sh
pip install 'textual>=8.2,<9' python-bidi   # python-bidi is only for Hebrew in visual order
python daemon.py inbox --demo [--empty]     # invented data; nothing is read or written
python daemon.py inbox [--base-dir DIR]     # DIR holds state/ and inbox.yaml (default: this checkout)
```

Without Textual the command prints the install hint and exits 2; the rest of
the daemon never imports it. Press `?` for keys, `ctrl+p` for the palette.

## Item format v1

One item is one JSON object:

```json
{"v": 1, "id": "events:3f9a1c0d2b7e4a58", "source": "calendar",
 "section": "fyi", "urgency": "digest",
 "sender": "Ann Example", "where": "calendar",
 "gist": "Updated: Weekly sync (2030-01-08T10:00:00+00:00)",
 "ts": 1893492000.0, "link": null,
 "thread": [{"who": "start", "ts": 1893492000.0, "text": "09:00 -> 10:00"}],
 "draft": null, "actions": ["ack", "open"], "meta": {}}
```

| Field | Type | Rule |
| --- | --- | --- |
| `v` | int | exactly `1` |
| `id` | string | 1 to 200 chars, no control characters. Stable across polls and scoped to its source, e.g. `events:<event id>`. Done, snooze and ack marks are keyed by it. |
| `source` | string | `slack`, `email`, `calendar`, `recording`, `alert`, `drive`, `other` (picks the glyph and the `f` filter) |
| `section` | string | `urgent`, `needs_reply`, `waiting`, `fyi` |
| `urgency` | string | `urgent` or `digest` |
| `sender`, `where` | string | one line, up to 200 chars |
| `gist` | string | one line, up to 120 chars |
| `ts` | number | epoch seconds (finite, not negative) |
| `link` | string or null | `http://` or `https://` URL with a host and no whitespace, control (C0/C1) characters or DEL, or null |
| `thread` | list | `{"who": str, "ts": number, "text": str}` objects, up to 100, oldest first. Slack items are rendered as Slack mrkdwn, every other source as plain text. |
| `draft` | string or null | suggested reply text, up to 8000 chars |
| `actions` | list | no duplicates, a subset of `reply`, `ack`, `done`, `snooze`, `nudge`, `open` |
| `meta` | object | opaque to the inbox; handed back to the source unchanged by `act`. Four optional keys are read: `meta.thread_note`, a string shown as one dim line above the thread (cleaned to one line, cut at 200 chars; an item with an empty `thread` and a note shows only the note), `meta.reply_to`, the Slack destination of a send or a reaction (see [The `reply_to` contract](#the-reply_to-contract)), `meta.suggested_reaction`, an emoji name (`^[a-z0-9_+\-]{1,64}$`) that the react picker preselects (see [Slack reactions](#slack-reactions-e)), and `meta.memory_id`, the id of the memory entry an `events` item was captured as (`^[a-z0-9][a-z0-9-]{0,200}$`; the built-in `events` source copies it from the event when present and well-formed, so a context command can link the entry; a command source may set it too) |

All fields are required. Unknown extra keys are ignored and dropped.

How the UI uses `actions`: `ack` enables `a` (acknowledge), `reply` enables the
draft and `a`/`e`, `nudge` (in `waiting`) enables `n`, `open` enables `o` (hidden for an
item with a null `link`). `done` and `snooze` always work, because
they are local marks; listing them only tells phase 2 that the source wants an
`act` call as well.

### Untrusted text

Every string from a source is display text from somebody else. The inbox
strips ANSI escapes, control characters and bidi overrides/embeddings/isolates
(LRM, RLM and ALM are kept), flattens single-line fields, and cuts a `gist`
longer than 120 characters instead of rejecting it. Text is drawn as plain
text, never as markup, so a title such as `[b]x[/b]` stays literal. Sources
should still send clean data; `validate_item` in `workspace_daemon/inbox/items.py`
is the strict check and `parse_item` is sanitise-then-validate.

## Save protocol

`m` on any item saves it to memory by running the command configured as
`save:` in `inbox.yaml` (see [Save to memory](#save-to-memory)). There is no
default: the inbox never saves anything by itself. The command is any program,
run without a shell (`~` at the start of an argument is expanded). It is called
with **no extra arguments** and receives one JSON object on stdin:

```json
{"v": 1, "action": "save", "item": { ...the item v1 dict, as listed... }}
```

and prints one JSON object on stdout:

```json
{"ok": true, "message": "Saved to your memory", "memory_id": "m-123", "duplicate": false}
```

| Field | Type | Rule |
| --- | --- | --- |
| `ok` | bool | required; `false` is a failure and the inbox shows `message` |
| `message` | string | required; shown in the toast and the audit log (one line, cleaned) |
| `memory_id` | string or null | optional (null when left out), up to 200 chars |
| `duplicate` | bool | optional (false when left out). `ok: true` with `true`: already in memory, unchanged; this is a success (the toast says "Already in memory", the row gets the saved mark). `ok: false` with `true`: a likely duplicate, see below |
| `candidate` | object or null | optional, only read with `ok: false` and `duplicate: true`: `{"id": str, "title": str, "score": number}` (any part may be left out), the memory the item looks like |

**Likely duplicate.** A reply with `"ok": false, "duplicate": true` is not an
error. Its `memory_id` is null (nothing was saved); the existing memory's id is
`candidate.id`. The inbox shows `message` and the candidate in a "Possible
duplicate" modal (Cancel focused) offering "Save anyway". The modal does not
open by itself (it could land on top of another one and a stray key would
confirm it): a toast says "Possible duplicate, press m to review", and `m` on
that item opens it. Cancel saves nothing and adds no mark. Confirm waits out
the undo window again, then resends the same stdin with `"force": true` added.
If that forced call still answers `ok: false` it is shown as an error (the
inbox asks once). Any other `ok: false` is an error toast with `message`; a
failure is never shown as a save.

**Force.** After a duplicate answer and the user's "save anyway", the same
payload is sent again with `"force": true` added at the top level
(`{"v": 1, "action": "save", "item": {...}, "force": true}`); the command should
then save without its duplicate check. The key is absent on a first call.

The whole item travels on stdin only: the argv is exactly the configured list
and never contains item text. The same limits as command sources apply: the
configured `timeout_s` (default 30, 5 to 120), stdin written then closed, a
process group stopped whole on timeout, output stopped past 4 MB, and the last
64 KB of stderr kept (its tail is shown when the exit code is not 0). Unlike a
command source's `list`, a save that times out (or overflows) first gets
`SIGTERM` for the whole group and 5 seconds to clean up before the `SIGKILL`,
so a command's own child (a CLI it started) is not cut off mid-write. On
`SIGTERM` the command must stop and wait for any child processes it started
before it exits: the inbox sends `SIGKILL` to the whole group after the 5 s
grace, whatever is still running. A timeout is reported as "timed out after Ns;
the save may still complete": the command may have finished its work, so check
before saving again. A timeout, an oversized or non-JSON answer, a missing `ok`
or `message`, a wrong type, a non-zero exit or a program that will not start is
a failure: a toast with the reason, no mark. A save is never retried by the
inbox, so a command should treat a repeated item as `duplicate`. The `item` is
untrusted display text (see above); a command that writes it anywhere must
treat it as data.

## Context protocol

`c` opens the Context pane (see [Context pane](#context-pane-c)), which shows
what the command configured as `context:` in `inbox.yaml` returns for the
selected item. There is no default: without `context:` the inbox runs nothing
and `c` says "Context not configured". The command is any program, run without
a shell (`~` at the start of an argument is expanded). It is called with **no
extra arguments** and receives one JSON object on stdin:

```json
{"v": 1, "action": "context", "item": { ...the item v1 dict, as listed... }}
```

and prints one JSON object on one line on stdout:

```json
{"v": 1, "ok": true, "sections": [{"title": "Person", "lines": ["Role: Example engineer", "Team: Platform"]}], "message": null}
```

| Field | Type | Rule |
| --- | --- | --- |
| `v` | int | required, exactly `1` |
| `ok` | bool | required; `false` shows `message` in the pane instead of sections |
| `sections` | list | optional (empty when left out): `{"title": str, "lines": [str]}` objects; both keys required in each, every line a string |
| `message` | string or null | optional; shown for `ok: false` (cleaned to one line, cut at 300 characters). With `ok: true` it is ignored unless there are no sections |

**Caps.** Everything the command prints is untrusted display text (see
"Untrusted text"). Titles and lines are cleaned to one line with `clean_line`
(ANSI and OSC escapes, control characters and bidi overrides removed) and cut:
a title at 80 characters, a line at 300; only the first 6 sections and the first
40 lines of each are kept, and the whole answer stops at 20000 characters of
title and line text (what comes after is dropped). The raw output is limited to
256 KB: the command's process group is stopped past that and the pane shows
"response is too large". A wrong type, a missing `v`, `ok`, `title` or `lines`,
or output that is not JSON shows "bad response: ..." instead of partial text.

**The command must be read-only.** The inbox runs it on its own, every time
you select an item while the pane is open, and may run it again after a refresh
or a cache expiry; it can run for items you never act on. Do not let it send,
post, write or change anything. It also must exit within `timeout_s` (default
20, 2 to 60) or, on `SIGTERM`, stop and wait for any child processes it
started before it exits. A timeout sends `SIGTERM` to the whole process group
and `SIGKILL` 5 s later, whatever is still running (the same handling as the
save command); the pane says "timed out after Ns". A non-zero exit shows
`exit N` and the tail of stderr. Item text reaches the command only as JSON on
stdin, never in argv, and is untrusted: a command that writes or queries with
it must treat it as data. A command is never retried by the inbox; a failure
is cached for 30 s so the pane does not rerun a broken command on every key.

## Actions protocol

`actions:` in `inbox.yaml` adds generic, confirmed write actions, one key each.
There is no default and the inbox ships none: the program behind an action is
yours (see [Custom actions](#custom-actions)).

```yaml
actions:
  - {key: "p", label: "Add to agenda", argv: ["python3", "~/bin/my-action.py"], timeout_s: 120}
```

One program serves one action. It is called up to three times per use, each
time with **no extra arguments**, the request as one JSON object on stdin and
the answer as one JSON line on stdout. `name` is the action's `label`; the item
(untrusted display text, see above) is only ever on stdin, never in argv.

| Call | stdin | stdout | Effect |
| --- | --- | --- | --- |
| `choices` | `{"v": 1, "action": "choices", "name": "...", "item": {...}}` | `{"v": 1, "ok": true, "choices": [{"id", "label", "detail"}], "message": null}` | none (read-only) |
| `prepare` | `{"v": 1, "action": "prepare", "name", "item", "choice": "<id>"}` | `{"ok", "rows": [[label, value], ...], "state": "<opaque string>", "message", "duplicate"}` | none (read-only; reads fresh data) |
| `run` | `{"v": 1, "action": "run", "name", "item", "choice": "<id>", "state": "<opaque string>"}` | `{"ok": true, "message": "..."}` | the only call that changes anything |

Answer fields. `v` may be left out of an answer; if present it must be 1.
`choices`: at most 50, each with a string `id`, a `label` (80 chars) and an
optional `detail` (200). The `id` is an opaque handle and comes back to
`prepare` and `run` byte for byte (never cleaned or cut); a row whose `id` is
not a string of 1 to 200 characters or holds a control character is skipped,
and a repeated `id` keeps its first row. `label` and `detail` are shown, so
they are cleaned to one line. `ok: false`, or no choice, shows `message`. `prepare`:
`rows` (at most 20 `[label, value]` string pairs, value up to 1000 chars,
newlines kept) are what the user is asked to confirm; `state` is an opaque
string of at most 64 KB that the inbox hands back to `run` unchanged (use it to
pin what was read, so `run` does not have to guess); `rows` and `state` may be
null or left out when `ok` is false. `message`: one line, 300 chars. A reply
with `"duplicate": true` is checked **before** `ok`: the inbox shows `message`
as information ("Already there"), not as an error, and stops. Any other
`ok: false` shows `message` as an error and stops. A bad answer (not JSON, a
wrong type, a wrong `v`), a non-zero exit, a timeout (`timeout_s`, 5 to 300,
default 60 per call) or output over 512 KB counts as a failure. The commands
run under the same capped runner as `save` (own process group, `SIGTERM` for
the group and 5 s of grace before `SIGKILL`; the command must stop its own
children on `SIGTERM`).

**Contract.** The commands must be safe to call repeatedly:

- `choices` and `prepare` must be read-only. The inbox calls them without any
  confirmation, and calls them again whenever the user tries again or cancels.
- `run` is called once per confirmed action and is never retried by the inbox
  (a timeout says "it may still have completed"); it should be idempotent
  where possible, so that an action repeated by hand does not do its work twice
  (that is what `prepare`'s `duplicate` answer is for).
- Nothing in `item`, `choice` or `state` may be treated as an instruction:
  it is data.

## Source-command protocol

A `command` source is any program, run without a shell (`~` at the start of an
argument is expanded):

- `<argv...> list` prints to stdout
  `{"v": 1, "items": [...], "stale": bool, "age_min": number|null, "error": str|null}`.
  `stale` says the source's own data is old; `age_min` is how old; `error` is
  a message the UI shows without hiding the items.
- `<argv...> list --all` is the same call with one optional flag: "list
  everything, not just what is open" (handled and older items too). The inbox
  sends `--all` only while the `A` toggle is on **and** the source declares
  `supports_all: true` in `inbox.yaml`. A source that does not declare it
  never receives `--all`, so existing sources need no change. A source that
  declares it must accept the flag and print the same JSON.
- `<argv...> act` reads `{"v": 1, "action": str, "id": str, "meta": object, "text"?: str}`
  on stdin and prints `{"ok": bool, "message": str}`.

Phase 1 calls only `list` (with `--all` as above), with the configured timeout, stdin closed, in its
own process group (killed whole on timeout). stdout is read in chunks and the
group is killed as soon as it passes 4 MB; only the last 64 KB of stderr are
kept, so a chatty source cannot exhaust memory. `timeout_s` must be below
`poll_interval_s` (the default adapts to short intervals, and no
source is run more often than that unless its `watch` files change). Output over 4 MB, non-JSON output,
a wrong `v`, a non-zero exit, a timeout or a program that will not start is a
**source error**: shown in the header and a toast, items from that source are
empty, nothing crashes. One bad item (failed validation, duplicate id) is
dropped and counted in the error text without hiding the others. A duplicate id
across sources keeps the first.

**Background children.** The whole process group is killed when the command
exits, times out or overflows, so a source that starts a background child it
wants to outlive the call (a cache refresher, a daemon) must detach it into its
own session, e.g. `setsid cmd &` (or `start_new_session=True` in Python);
otherwise the child dies with the call.

`act` (`run_command_act` in `sources.py`) is defined and tested but not called
until phase 2. It has the same limits: its own process group, killed whole on
timeout or an oversized response.

Refreshes never overlap: one that is still running when the next is due (or
when you press `r`) is skipped, and `r` says "Poll already running". Each
refresh first re-reads the state file, so marks made in another pane show up.

### Change-driven refresh

The inbox does not poll every source on a timer. Every `watch_interval_s`
(default 3) it stats (mtime and size, never reading) the files each source
depends on and re-runs **only** a source whose file changed, appeared or
vanished. A fallback full refresh runs every `poll_interval_s` (default 600),
and `r` refreshes everything at once.

- Built-in sources know their files. `events`: `state/events.jsonl`, its `.1`
  rotation and `state/alerts.json` (resolved alerts), plus `routines/*.yaml`
  while `email_require_label` is set. `events.invites`: the calendar cache's
  `state/calendar-snapshot.json`. The Saved view watches `events.jsonl` while
  it is open and reloads its rows when the file changes.
- A `command` source lists its files in `watch: [paths]`. Relative paths are
  relative to the base directory; absolute paths and `~` work. A source with
  no `watch` list is refreshed by the fallback timer and `r` only.
- Debounce: a source is re-run at most once per 2 s, however often its files
  change; the change is kept and fires when the 2 s are over. A change that
  lands while a refresh is running is picked up right after it.
- Watched paths are stat-ed on the UI thread: keep them on local disks, not
  network mounts, or a slow mount stalls the screen.
- A stat failure counts as a stable state, never an error; a file that does
  not exist yet is watched, and its appearing is a change.
- The header shows `updated Ns ago` for the least recently refreshed source;
  per-source errors show as before. A watched source whose files were checked
  and did not change counts as fresh; a source with no `watch` list is as old
  as its last run.
- Time-based rules are re-applied every 30 s to what the sources already read,
  without touching a file: an invite that has ended, the `window_h` cutoff, an
  alert dropping from urgent after `alert_urgent_max_age_h`, and
  `starts_within_h` urgent rules.

## Built-in `events` source

Reads `<base>/state/events.jsonl` and the `.1` rotation (schema in
`workspace_daemon/events.py`), read-only and never taking `events.lock`. The live
file is read before the `.1` rotation and events are de-duplicated by `id`, so
a rotation in between cannot hide events. A torn last line or garbage line is
skipped. Only the last `window_h` hours are shown
(default 48). One event per `source_id` is kept, the newest, so a calendar
event that was created, then updated, appears once.

| Event | Item `source` | `where` | `actions` |
| --- | --- | --- | --- |
| `gmail` | `email` | the mail subject (`details.mail_subject`), else `mail` | `open` (reply arrives in phase 2) |
| `gcal` kinds `new`, `update`, `cancel`, `rsvp` | `calendar` | `calendar` | `ack` |
| `mila`, `zoom` | `recording` | `mila` or `zoom` | `ack` |
| `daemon` kind `alert` | `alert` | `daemon` | `ack` |

A Gmail capture event carries plain-text display fields in `details` (each
is cleaned to one line and capped when it is written and read again;
`mail_snippet` is Gmail's own snippet, a short excerpt of the body): `mail_from` (the sender's display name only, never an address; `<...@...>`
is stripped and a value that still holds an `@`, or its fullwidth form, is
dropped; 80 characters),
`mail_subject` (200), `mail_snippet` (Gmail's own snippet, 300) and
`mail_thread_id`. The daemon records a field only when its Gmail source
provides it: the `gws` path reports the snippet when its read has one, the
connector (`gmail_mcp`) path does not, so there it is left out. The `email`
item then shows `sender` = `mail_from` (else the people list, else `mail`),
`where` = the subject, a one-message thread `{who: mail_from, text: snippet}`
when there is a snippet, and a link to the Gmail thread built from
`mail_thread_id` (else from the event's `source_id`) and `gmail_account`. The
`gist` stays the memory title. The event's `memory_id`, when it matches
`^[a-z0-9][a-z0-9-]{0,200}$`, is copied to `meta.memory_id` so a context
command can link the captured entry.

`open` is listed only when the item has a link (the UI also hides it for any
item with a null `link`, whatever a source says). Today only Gmail events have
one, so the other rows are `ack` only.

### Pending calendar invites

Upcoming events whose RSVP is not yet answered (`needsAction`) are listed under
**Needs reply** as `calendar` items, one row per invite (a recurring series is
one row, at its next instance). They are read from the calendar cache, never
from the network:

- `<base>/state/calendar-snapshot.json` (written by the `calendar_cache`
  routine, `{"v": 1, "fetched_at", "window_hours", "events": {id: row}}`):
  `my_rsvp`, `status`, `start`, `end`, `title` and `conference` of each event.
- The cache's JSON twin (the routine's `output` path with a `.json` suffix),
  for the organizer's name, which the snapshot lacks. It covers only the
  shorter output window (`window_hours`, 48 by default), so an invite further
  out shows without an organizer.
- The `calendar_cache` routine's config under `<base>/routines/` for its
  `calendar_id` (default `primary`).

An invite is shown when `my_rsvp` is `needsAction`, its status is not
`cancelled`, it has not ended and it starts before now plus `invites_days`
(default 7). Accepted, tentative, declined and organizer rows never show. The
window cannot reach past what the snapshot covers (`events_window_days` of the
routine, 7 by default); asking for more adds a note to the source's status.
Times are shown in the machine's local time zone. The row is `sender` = the
organizer (or "calendar invite"), `where` = "invite", and a gist of
`<title> - <when> - from <organizer>`. The item `ts` is the event's start (the
cache records no arrival time for an invite), so the row's age reads `in 2h`,
and invites list soonest first, after the replies in the section. `meta` is
`{"kind": "invite", "event_id", "calendar_id", "series", "my_rsvp", "start",
"source_id"}` so a later RSVP action has what it needs. Actions: `ack` (hides
the row locally) and `open` (the meeting link from the cache, only when it is a
plain http(s) URL; otherwise there is no `open`). A `New invite` calendar row
from the events stream is dropped when its series has a pending-invite row
(the same event, now actionable). Titles and organizers are untrusted display
text. A missing cache means no invites and no error; an
unreadable or unfamiliar one is a source error (`events.invites`) and a cache
older than 6 hours marks the source stale.

### RSVP on pending invites

On a pending-invite row `a` accepts, `X` declines and `T` answers tentative
(the answers sent are `accepted`, `declined`, `tentative`). It needs the
`rsvp:` block in `inbox.yaml` (no default). Only the rows the inbox builds
itself (`invites:` ids) qualify; a command source's item never does, and an
event or calendar id outside `[A-Za-z0-9_@.+=-]` is refused.

1. A modal shows the event title, the time, the organizer and the exact answer.
   Cancel is focused; `y` confirms, `esc` cancels (nothing happens, and the
   audit log says so).
2. The row leaves the queue and the 5 s undo window opens (`u` restores it;
   `q` inside the window cancels the RSVP). Once the call has started, `q` says
   "N write(s) still running; q again to quit (they finish first)"; a second
   `q` quits and the exit message says how many writes are still finishing (the
   process ends when they do).
3. A worker makes one headless Claude call through the bridge
   (`workspace_daemon/claude_bridge.py`, `call_tools`). The only allowed tool is
   `rsvp.tool`; its arguments are pinned to
   `{<args.event_id>: <event id>, <args.calendar_id>: <calendar id>,
   <args.response>: <answer>}`, and the call must carry exactly those three
   arguments and no others (the bridge's opt-in `exact_args=True`: its
   PreToolUse hook denies a call that differs by a byte or adds an argument
   such as a notification flag, and the run fails if one ran anyway; every
   other bridge caller keeps the default, where extra arguments are allowed).
   The run is never retried (`attempts=1`). The prompt names the tool and prints
   the three pinned values as JSON; item text never enters it. The bridge
   guards apply first: `state/PAUSED` stops the call, and so does an exhausted
   or unreadable daily budget (`state/budget.yaml`). Spend is logged in
   `state/claude-usage.jsonl` under the tag `inbox-rsvp`.
4. Success is read from the tool's result (the run needs a real, non-error
   result whose arguments equal the pins; a JSON answer with `error` or
   `ok`/`success` false counts as a failure), never from the model's words.
   Then the row is hidden by a local mark and the action goes to the audit log.
   The mark (`rsvp` with the answer, `rsvp_event`, the event id it was made
   for, and `rsvp_at`, its time, in `inbox-state.json`) hides only that event:
   a recurring series is one row whose `event_id` moves on to the next
   instance, and that instance shows again. The mark stops mattering once the
   calendar cache reports your answer (the row is then no longer listed and the
   mark is pruned with the other marks). If a cache snapshot fetched more than
   10 minutes after the answer still lists the same event as unanswered, the
   answer did not take: the row comes back with a "RSVP not confirmed by
   calendar" hint (a `!` in the queue, a line above the thread) so a silent
   failure cannot hide an invite. The snapshot's fetch time is
   `meta.cache_fetched`.
5. Any failure (guard stop, tool error, timeout, no confirmed result) keeps the
   row and shows a toast with the reason; the reason carries only fixed kinds
   (never prompts or tool output).

A recurring series is one row. The answer applies to the instance in
`meta.event_id` (the next one) only, and the modal says so; the series is not
answered, and the next instance appears as a new invite to answer.

Without `rsvp:`, `X` and `T` say "RSVP not configured", and `a` keeps
acknowledging the invite as in 0.2.0.

### Save to memory

`m` on any item shows a modal with the source, sender, gist and link, then the
5 s undo window (`q` inside it cancels), then runs the `save:` command (see
[Save protocol](#save-protocol)). On success the toast shows the command's
message (or says it was already saved) and the action goes to the audit log.
The item is then marked done, as `d` would (a local mark and an audit row; the
row leaves the queue, and `u` inside the next 5 s brings it back), also when
the command answered that it was already in memory and after a forced "Save
anyway". `save: {mark_done: false}` keeps the row instead, with a `◆` and a
local `saved` mark. A failure toasts the reason, adds no mark and leaves the
row; a likely duplicate asks "Save anyway" first (see the protocol) and leaves
the row too. Without `save:`, `m` says "Save not configured".

Calendar `details.start`, `end` and `changed` become the gist suffix and thread
lines. A Gmail thread id (the second `:`-segment of
`gmail:<thread>`, `gmail:<thread>:followup-open` or `gmail:<thread>@<message>`)
becomes a link
`https://mail.google.com/mail/u/<gmail_account>/#all/<thread id>`; other
events have no link yet.
Items are `fyi`/`digest` unless an urgent rule matches. By default
`budget-hit:*`, `connector:*` and `attention:*` alerts are urgent. Titles and
people are untrusted (see above).

### The `reply_to` contract

A source that wants its items to be answerable from the inbox puts the Slack
destination in `meta.reply_to`:

```json
"meta": {"reply_to": {"channel": "C0EXAMPLE01", "thread_ts": "1893492000.000100",
                      "message_ts": "1893492060.000200"}}
```

| Key | Rule |
| --- | --- |
| `channel` | required; a Slack conversation id matching `^[CDG][A-Z0-9]{8,12}$` (`C` channel, `D` DM, `G` group) |
| `thread_ts` | optional (absent or null); matching `^\d{9,11}\.\d{6}$`. When present the reply goes into that thread. Without it the post would be top-level: fine in a DM (`D`), refused in a channel or group (`C`, `G`) unless `send.allow_channel_posts: true` |
| `message_ts` | optional; same format. The message a reaction goes on |

A value in the wrong format makes the whole `reply_to` invalid; unknown keys
inside it are ignored and never used. The destination is never read from
`gist`, `thread` text, `where` or any other item text: only from here. An item
without a valid `reply_to` cannot be sent to, and `a` or `n` say so instead of
opening the confirm modal. `where` is only the label shown in the modal.

### Slack send (reply and nudge)

`a` on a reply and `n` on a nudge send the draft to Slack when the `send:`
block is set (no default). Without `send:` the confirm flow exists only in
`--demo` (nothing is sent) and `a` / `e` / `n` on a real item say "Replying is
not available yet", as in 0.5.0.

1. **Checks.** The item needs a valid `meta.reply_to` (above). A reply to a
   channel or group (`C`, `G`) without a `thread_ts` would be a top-level post:
   it is refused with "would post to the channel, not a thread" unless
   `send.allow_channel_posts` is `true` (a DM needs no thread; a nudge follows
   the same rule). The text is the current draft, including any `e` edits (an
   open edit is saved first; editor text is cleaned like any draft, so control
   and bidi-override characters are removed), with only trailing whitespace
   removed. It is refused, never cut, when it is empty, when it is longer than
   4000 characters ("the text is N characters; the limit is 4000; edit it
   first") or when it still holds a control or bidi-override character (LRM,
   RLM and ALM are fine). Only one send runs at a time: while one waits out its
   undo window or runs, another is refused with a message.
2. **Precheck (optional).** With `send.precheck` the command runs first, in a
   background thread behind a "checking" modal (`esc` cancels the whole send and
   stops the command). See below.
3. **Confirm.** The modal shows the destination (`<where> (thread)`,
   `(DM)` or `(channel)`), under it the real route (the channel id and the
   thread timestamp, so a wrong `where` label cannot hide it) and the full text, scrollable (`j`/`k`, the arrows
   and the page keys), laid out line by line exactly as it will be sent.
   Cancel is focused; `y` confirms, `esc` cancels (the audit log says so).
4. **Undo window.** The row leaves the queue and the 5 s undo window opens (`u`
   restores it; `q` inside the window cancels the send). Once the call has
   started `q` says "N write(s) still running; q again to quit (they finish
   first)"; a second `q` quits, the exit message says the send "may have been
   sent, check Slack", its audit row reads "running at quit", and when the call
   ends the process prints one line to stderr and closes the row.
5. **The call.** A worker makes one headless Claude call through the bridge
   (`call_tools`) whose only allowed tool is `send.tool`. Its arguments are
   pinned to exactly `{<args.channel>: <channel>, <args.text>: <text>}` plus
   `<args.thread_ts>: <thread_ts>` when the destination has one, and the call
   must carry exactly those arguments (`exact_args=True`: the PreToolUse hook
   denies a call that differs by a byte or adds an argument, and the run fails
   if one ran anyway). `attempts=1`: a send is never retried, and
   `max_calls=1` allows ONE call of the tool in the run: the hook claims a
   marker file per allowed call in the 0700 run directory and denies the next,
   and the stream re-check fails the run if a second call ran anyway, so the
   model cannot post twice. The prompt
   carries the tool name and the pinned values only. The bridge guards apply
   first (`state/PAUSED`, the daily budget); spend is logged as `inbox-send`.
6. **Result.** Success needs a real, non-error result of the pinned call
   (never the model's words); a JSON answer with `error` or `ok`/`success`
   false is a failure, and a plain-text reply that reads like an error
   (error, failed, denied, not_in_channel, rate limited, ...) without a Slack
   timestamp is unconfirmed ("may not have been sent; check Slack"). A real
   send is never reported as "not sent": if any result confirms it and another
   one does not, the toast says "sent at least once; check Slack". A timeout, or a run cut off by a sleep, is reported as
   "the message may have been sent, check Slack" and the row comes back; a
   guard stop or a tool error says "not sent". If the tool ran more than once
   the toast says so, so a duplicate can be spotted. On success the item is
   marked done (local mark, so it stays gone) and the toast says "Sent to
   Slack". Every send is a row in the audit log.

A nudge is the same path with the nudge text.

**`send.precheck`.** An optional read-only command that can warn about
something that changed (for example a reply you already sent from another
device). Right before the confirm modal opens it runs with the capped runner
(its own process group; `SIGTERM`, then `SIGKILL` 2 s later on a timeout or
`esc`). Stdin is one JSON line, never argv:
`{"v": 1, "action": "precheck", "item": {...}, "text": "<the exact text>"}`;
stdout is `{"v": 1, "ok": true, "warn": "<one line>" | null}` (up to 64 KB).
A `warn` is shown prominently in the confirm modal (for example "⚠ You already
replied at 16:40") and added to the audit log; Cancel stays the default.
A failure of any kind (exit code, timeout, bad JSON, `ok` not `true`) shows
"⚠ couldn't check for a newer reply" in the modal. A precheck only advises: it
never blocks a send, and nothing it prints is sent anywhere. If the draft or
the selection changes while it runs, nothing is sent and the toast says so.

`send:`:

| Key | Default | Meaning |
| --- | --- | --- |
| `tool` | required | the Slack connector tool as Claude Code lists it, `mcp__<server>__<tool>` |
| `args` | required | the tool's parameter names for `channel`, `thread_ts` and `text` (three different names) |
| `timeout_s` | `90` | 30 to 300 for the whole bridge call |
| `model` | `haiku` | the model of the one-call run |
| `precheck` | off | `{argv, timeout_s}`: the optional precheck command (above); `argv` is a list (no shell), `timeout_s` 1 to 30, default 10 |
| `allow_channel_posts` | `false` | `true` allows a send to a channel or group without a thread (a top-level post) |

### Slack reactions (`E`)

`E` adds an emoji reaction to the item's message when `react:` is set (no
default). The target is `meta.reply_to.message_ts`, else `thread_ts`; without
either the key says so. The flow is the RSVP pattern: an emoji picker (the
configured names as `:name:`, `1`-`9` quick pick, `esc` cancels; a valid
`meta.suggested_reaction` that is one of your `emojis` is highlighted and
marked "suggested" (one outside your list is ignored, unless
`react.allow_suggested: true`, which adds it in front, for that item only);
`enter` still picks it, nothing is sent until the confirm), the confirm
modal (Cancel focused), the 5 s undo window (`u` undoes, `q` inside it
cancels), then one pinned call with exactly `{<args.channel>: <channel>,
<args.timestamp>: <timestamp>, <args.name>: <emoji>}` (`exact_args=True`,
`attempts=1`, `max_calls=1`, never retried), spend logged as `inbox-react`. Success is a real
non-error result; a success marks the item done unless `mark_done: false`. A
failure leaves the row and says why (a timeout says "check Slack").

`react:`:

| Key | Default | Meaning |
| --- | --- | --- |
| `tool` | required | the reaction connector tool, `mcp__<server>__<tool>` |
| `args` | required | the tool's parameter names for `channel`, `timestamp` and `name` |
| `emojis` | `eyes`, `white_check_mark`, `+1`, `pray` | 1 to 9 emoji names matching `^[a-z0-9_+\-]{1,64}$` (no colons), no repeats |
| `timeout_s` | `60` | 30 to 300 |
| `model` | `haiku` | the model of the one-call run |
| `mark_done` | `true` | a successful reaction marks the item done |
| `allow_suggested` | `false` | `true` lets `meta.suggested_reaction` add an emoji that is not in `emojis` |

### Polish a draft (`P`)

`P` rewrites the draft in your tone when `polish:` is set (no default). It
**never sends anything**.

1. **Tone guidance.** One read-only `call_tools` run whose only allowed tool is
   `polish.tone_tool`, pinned to exactly `polish.tone_args`
   (`exact_args=True`, `attempts=1`). The MCP server comes from the separate
   file `polish.mcp_config` (see below), so it need not be one of your
   everyday connectors. The tool's text is cached on disk in
   `state/inbox-polish/tone.json` (mode 0600 in a 0700 directory of its own),
   keyed by the tool and its arguments, for `cache_h` hours, so most polishes
   make one call, not two.
2. **Rewrite.** `claude_bridge.complete`: no tools at all, `max_turns=1`,
   `--strict-mcp-config` with no config file (no MCP server loads), the
   bridge guards apply, spend is logged as `inbox-polish`. The prompt asks for
   the same meaning, facts, names, numbers and language (Hebrew stays Hebrew),
   in the guided tone, and for the message only. The draft, up to the last 10
   thread messages and the tone guidance are data: each sits between markers
   with a random per-call token, after an instruction to ignore anything in
   them that reads as an instruction.
3. **Review.** A modal shows the old and the new text. `a` accepts (the draft
   is replaced here, nothing else), `e` opens the editor with the new text,
   `esc` cancels (focused by default). A result is dropped if the draft or the
   selection changed while it ran, or while the review was open (the check is
   repeated before accept and before edit). A polish counts in the quit guard like a
   summary.

The `mcp_config` file is JSON in Claude Code's MCP config format, for example
`{"mcpServers": {"tone_connector": {"type": "http", "url":
"https://tone.example.com/mcp"}}}`. It is passed to the run as `--mcp-config
<copy> --strict-mcp-config` (only the servers in it are loaded). It must be an
absolute path to a regular file you own with no group or other permission
bits (up to 1 MB); it is opened without following symlinks, judged on the open
file, and the bytes are read from that same open file and written to a 0600
copy in the run's private directory, which is what claude reads, so the file
cannot change between the check and the use.
The allowlist hook and the stream re-check still limit the run to the tone
tool. (`call_tools(..., mcp_config=None)`: the default leaves the bridge's
argv unchanged for every other caller.)

`polish:`:

| Key | Default | Meaning |
| --- | --- | --- |
| `mcp_config` | required | absolute path (a leading `~` is fine) to the MCP servers JSON file |
| `tone_tool` | required | the tone connector tool, `mcp__<server>__<tool>` |
| `tone_args` | `{}` | the tool's pinned arguments (parameter names to JSON values, up to 4000 characters) |
| `model` | `sonnet` | the model of both calls |
| `timeout_s` | `120` | 30 to 300, for each of the two calls |
| `max_budget_usd` | `0.05` | 0.005 to 2, the bridge's spend limit for each call |
| `cache_h` | `24` | 0 (no cache) to 720 hours |

### Custom actions

The key of an `actions:` entry (`p` in the example) runs the flow below on the
selected item. The key must be a letter the inbox does not use itself; the
free ones are `h l p t w x z` and `B D F H J K M N O Q U V W Z`, and
validation refuses any other (and a key or label used twice).

1. **Choices.** A "looking up choices" modal while the `choices` call runs in a
   background thread (`esc` cancels and stops the command). The answer opens a
   picker (`enter` or `1`-`9` picks, `esc` cancels), even when there is only
   one choice.
2. **Prepare.** The `prepare` call runs in a background thread with the same
   modal (it can take about 20 s: it reads fresh data). A `duplicate` answer
   shows "Already there" and stops; `ok: false` shows the message and stops.
3. **Confirm.** The rows go into the confirmation modal (Cancel is focused;
   `y` confirms).
4. **Undo window.** The same 5 s window as RSVP and save (`u` undoes, `q`
   inside it cancels, nothing runs).
5. **Run.** The `run` call runs once in a worker thread. The result is a toast
   and a row in the audit log (`L`). A running action counts in the quit guard
   like a write (`q` asks, then waits for it).
6. **Mark done.** On success the item is marked done, as `d` would (a local
   mark, an audit row, a 5 s window in which `u` brings the row back), unless
   the entry says `mark_done: false`. A failed action leaves the row.

### Context pane (`c`)

`c` shows or hides the Context pane, below the thread pane (so the right column
reads thread, context, draft). It needs `context:` in `inbox.yaml`; without it
`c` toasts "Context not configured" and opens nothing. `tab` reaches the pane
and `j`/`k`, `g`/`G` scroll it.

- While the pane is open, selecting an item fetches its context in a
  background thread, after the selection has stayed for about 400 ms
  (moving quickly through the queue fetches only where you stop). Opening the
  pane fetches the selected item at once. A newer selection supersedes an
  older one: a result that arrives for an item that is no longer selected is
  kept in the cache but not drawn.
- Results are cached in memory per item (its id and its `ts`) for 10 minutes;
  a changed item, with a new `ts`, is fetched again. Failures are cached for 30
  seconds. Nothing is written to disk. The pane shows the age of what it
  displays ("fetched just now", "cached, fetched 3m ago"). When the entry of
  the item on screen expires, its old rows stay drawn, marked "context stale,
  fetched 12m ago · refreshing…", and one new fetch starts by itself (checked
  every second); the rows are replaced when it answers. A failed refetch is a
  cached error for 30 seconds, so a broken command is retried at most that
  often while the item stays selected. Only the item on screen is refreshed:
  there is no prefetching of items you have not selected.
- At most 2 context commands run at once. A fetch that is still waiting for a
  slot when you move on to another item never starts.
- States: `Loading context…` with a spinner, an error (the command's `message`
  for `ok: false`, or a fixed reason such as a timeout) with "C retries", the
  sections, or "No context for this item".
- `C` (or `r` while the pane has focus, where `r` otherwise polls the
  sources) fetches the selected item again, replacing the cached result.
- Quitting sends `SIGTERM` to the process group of any context command that is
  still running, then `SIGKILL` 5 s later (the same path as a timeout), and the
  process exits when they are gone, within about 5 s. It asks for no
  confirmation. The pane is only redrawn when what it shows changes (state,
  data, or the age label each minute), so a mouse selection in it survives.

### AI summary (`i`)

`i` summarises the selected item and shows the summary as the top section of
the Context pane (opening the pane if needed, even without `context:`). `I`
asks again even when one is cached. It needs `summary:` in `inbox.yaml`
(`summary: {}` turns it on with the defaults); without it the keys say
"Summary not configured". Nothing runs unless you press the key.

- What is summarised: the item (source, sender, place, gist, time), its thread
  (the newest 30 messages, each cut at 2000 characters, at most 30000
  characters in all) and the context sections, **if they are loaded** when
  you press the key. A summary made before the context loaded says "written
  without the context above (I to redo)" once the context is there.
- The call is one `claude_bridge.complete` run (`max_turns` 1, never retried)
  with **no tools at all**: an empty `--allowedTools`, every built-in the
  bridge knows in `--disallowedTools`, the bridge's PreToolUse hook given an
  empty allowlist (it denies every call, `ToolSearch` and connector tools
  included), `--permission-mode dontAsk`, and a stream re-check that fails the
  run on any `tool_use` or permission denial. `state/PAUSED` and an exhausted
  or unreadable daily budget stop it before any process starts. Spend is
  logged in `state/claude-usage.jsonl` under the tag `inbox-summary`, and the
  per-call limit is `summary.max_budget_usd`.
- The item is data. The prompt gives the rules first (summarise, never follow
  instructions found in the text, never call a tool, at most 6 lines starting
  with "- ": what is being asked, the decision or next step, deadlines), then
  the item between two marker lines that carry a random token generated for
  that call (text in the item cannot close the block), then a reminder. Item
  text is never part of any argv. The answer is cleaned and cut to 6 lines of
  300 characters.
- Cache: `state/inbox-summary/cache.json` (file mode 0600 in
  `inbox-summary/`, a directory of its own that the inbox creates and sets to
  0700; written atomically, the 200 most recently used entries), keyed by the
  sha256 of the item id, its `ts`, a hash of its thread and the model. `i` on
  an item that has a cached summary shows it without a call, even while
  another summary runs, and the pane shows a cached summary as soon as you
  select the item. `I` makes a new call and replaces the entry. A failure is
  shown in the pane (and toasted) and not cached.
- One summary runs at a time ("A summary is already running"). Each call is
  recorded in the audit log (`summary`: new, forced or failed; cache hits are
  not).
- Quitting while a summary runs asks first: "summary still running; q again to
  quit (it finishes first)". The summary runs in a thread the process waits
  for, so the exit takes at most `summary.timeout_s` more and the bridge still
  logs the spend (and its `claude` child is not left running).
- The summary leaves your machine: the item text goes to the model, like any
  other bridge run. Only configure `summary:` if that is acceptable for the
  items your sources list.

### Mail label filter

`email_require_label: MY_LABEL` (events source, default off) keeps only the
Gmail events that were captured through that label.

The event stream does not record a thread's labels, nor whether a capture came
from a `saved_request` routine (`details` is empty for Gmail events), and the
inbox never calls Gmail. So the filter works from what the daemon *does* keep:
each event names its `routine`, and the routine's config holds its search
query. The inbox reads `<base>/routines/*.yaml` and accepts a Gmail event only
when its routine has at least one `gmail_mcp` block and **every** Gmail block
(`gmail` or `gmail_mcp`) of that routine has a `query` that is a plain
conjunction with a positive `label:MY_LABEL` term (case-insensitive). Events
carry the routine id, not the block, so one unlabelled block rejects the whole
routine. Quoted phrases are inert (`subject:"label:MY_LABEL"` is not a label
term), `-label:MY_LABEL` does not count, and any `OR`, `NOT`, `|`, brace or
parenthesis rejects the query. Every thread such a routine captured matched
its query, so it carried the label when it was captured.

What it does not prove: the label is checked at capture time, not now
(removing the label later changes nothing), and a routine whose query selects
the label in another way (a label id, a union) does not match. The filter uses
the routine's **current** query: editing the query changes which older events
show, not only new ones. If no routine matches,
no mail is shown and the source's status says why. Showing the live labels, or
the `saved_request` flag, per event would need the capture to record them in
`details`.

### Saved view

`S` opens a read-only list of what the daemon saved to memory in the last
`saved_days` days (default 2): capture events (kind `new` or `update` with a
memory id) from `events.jsonl`, newest first, one row per saved item (the
newest capture of a `source_id` wins). Each row shows the source glyph, title,
time and kind. `o` opens the link when the row has one (Gmail threads);
`esc` (or `S`) returns to the inbox. Saved rows are not inbox items: no
marks, no actions, nothing is written. They come from the capture events, not
from the queue, so an item that a save marked done is still listed here.

### Stale and recovered alerts

An alert stays urgent only while it is relevant. Three rules apply to every
alert (`daemon` kind `alert`), after the urgent rules above:

1. **Newest per key.** Alerts are grouped by `details.key`; only the newest
   of each key is listed, so an older alert for the same condition never
   shows beside it.
2. **Age.** An alert older than `alert_urgent_max_age_h` (default 6, range 1
   to 168) drops to `fyi`/`digest`. It stays listed until `window_h` passes.
3. **Recovery.** An alert whose condition has ended is *resolved*: it is
   `fyi`, carries `meta.resolved: true`, and is hidden unless `R` is on
   (`R` toggles "show resolved"; the header counts the hidden ones). Resolved
   rows show a green check. Only `alert` items can be resolved; a command
   source's own `meta.resolved` hides nothing.

   The signal is the daemon's own record of active alerts,
   `state/alerts.json` (key to time sent, or `{"pending": time}`).
   `alerts.check` drops a key on the first tick that finds its condition
   cleared, for every key kind (`attention:`, `interrupted:`, `connector:`,
   `budget-*`, `hold:`, ...) and every routine kind, maintenance routines
   such as calendar-cache and sheet-watch included. An alert is resolved only
   when its key is **no longer listed** there. A key that is still listed is
   active even if the problem changed (a last-run error that became a memory
   sink issue keeps its key), so it is never resolved early. The run log and
   the usage log are not consulted.

Recovery is read-only and best effort. A missing, unreadable, oversized or
unfamiliar `alerts.json` (a different daemon version), or one last written
before the alert (a copied file, a clock jump), means "unknown", and the alert
keeps only the age rule. Limit: an alert is resolved on the poll after the
daemon's next tick that drops the key, so it can lag by a tick.

## Local state

`<base>/state/inbox-state.json`:
`{"v": 1, "items": {"<id>": {"done": ts, "ack": ts, "snooze_until": ts, "updated": ts}}}`.
A `prefs` object may sit beside `items` (`{"thread_order": "newest"}`): display
choices made with a key, never item data, and not pruned.

- Done and ack hide an item; a snooze hides it until its time, then it
  reappears (checked every 30 s and on each poll).
- Writes are atomic (temp file plus rename) with mode 0600 in a 0700 directory.
  Each write re-reads the file first so two windows do not lose each other's
  marks. A corrupt file reads as empty and is kept as `inbox-state.json.bad`.
- Pruning: a mark is dropped once its item has not been listed by any source
  for 7 days (each poll re-stamps the marks of items that are still listed, at
  most hourly), so a done item stays hidden for as long as a source keeps
  showing it, however old. Active snoozes are never pruned by age. At most
  5000 entries are kept (least recently listed dropped first).
- Writes are serialised by a `threading.Lock` and an `fcntl` lock on
  `inbox-state.lock`, and go through a uniquely named temp file, so several
  panes (and threads) never corrupt the file or lose each other's marks.
- Done, ack and snooze are queued for 5 seconds so `u` can undo them; quitting
  commits them. A queued send or nudge is never committed on quit.
- `<base>/state/inbox-summary/cache.json` holds the AI summaries (see
  [AI summary](#ai-summary-i)): mode 0600 in a 0700 directory of its own, the
  200 most recently used entries.
  Context results are kept in memory only.
- `--demo` keeps marks in memory only and does not read `inbox.yaml`.

## Settings (`<base>/inbox.yaml`)

Gitignored and optional; copy `inbox.example.yaml`. A missing file means the
defaults; a broken one prints every problem and exits 2.

| Key | Default | Meaning |
| --- | --- | --- |
| `poll_interval_s` | `600` | 30 to 3600: the fallback full refresh (see "Change-driven refresh") |
| `watch_interval_s` | `3` | 1 to 30: how often the watched files are stat-checked |
| `theme` | `dark` | `dark` or `light` |
| `bidi_mode` | `auto` | `logical`, `visual` or `auto` (see below) |
| `thread_order` | `newest` | `newest` or `oldest`: which message the thread pane lists first (see "Thread order") |
| `gmail_account` | `0` | integer >= 0: the `/mail/u/N/` index in Gmail links, for when the account is not your first signed-in one |
| `alert_urgent_max_age_h` | `6` | 1 to 168: an alert older than this is `fyi`, not urgent (see "Stale and recovered alerts") |
| `sources` | the `events` source | list of `type: events` and `type: command` sources (below) |
| `rsvp` | off | calendar RSVP through the Claude bridge (below) |
| `save` | off | the command `m` runs to save an item (below) |
| `context` | off | the command the Context pane (`c`) runs (below) |
| `actions` | none | custom confirmed write actions, one key each (below) |
| `summary` | off | the AI summary (`i`) through the Claude bridge (below) |
| `send` | off | real Slack send for replies and nudges (see "Slack send") |
| `react` | off | Slack emoji reactions, `E` (see "Slack reactions") |
| `polish` | off | rewrite the draft in your tone, `P` (see "Polish a draft") |

The `events` source takes `window_h` (1 to 720, default 48), `urgent` rules,
and:

| Key | Default | Meaning |
| --- | --- | --- |
| `invites_days` | `7` | 0 to 60: show unanswered calendar invites starting within this many days (0 turns them off; see "Pending calendar invites") |
| `saved_days` | `2` | 1 to 30: how far back the Saved view (`S`) looks |
| `email_require_label` | `null` | a label name, e.g. `MY_LABEL`: show only mail captured through a routine whose query requires that label (see "Mail label filter"); `null` shows all mail |

A `command` source takes `argv` (list), `timeout_s` (1 to 120),
`watch` (list of paths, default none; see "Change-driven refresh") and
`supports_all` (`true` or `false`, default `false`): declare it only if
`<argv> list --all` works.

`rsvp:` (no default; omit it and RSVP is off):

| Key | Default | Meaning |
| --- | --- | --- |
| `tool` | required | the connector tool's name as Claude Code lists it, `mcp__<server>__<tool>` (a built-in such as `Bash` is refused) |
| `args` | required | the tool's parameter names: `event_id`, `calendar_id` and `response` (three different names) |
| `timeout_s` | `60` | 30 to 300 for the whole bridge call (the bridge waits about 6 s for the connectors to attach inside it) |
| `model` | `haiku` | the model of the one-call run |

The `response` parameter receives `accepted`, `declined` or `tentative`
(Google Calendar's wording). The calendar id comes from the `calendar_cache`
routine (`primary` by default).

`save:` (no default; omit it and saving is off): `argv` (list, no shell),
`timeout_s` (5 to 120, default 30; a timeout stops the command with `SIGTERM`,
then `SIGKILL` 5 s later) and `mark_done` (default `true`: a successful save
marks the item done; `false` keeps the row, marked saved).

`actions:` (no default; a list of up to 8): each entry takes `key` (one free
letter), `label` (up to 40 characters, unique; it is the `name` the command
receives), `argv` (list, no shell), `timeout_s` (5 to 300, default 60, per
call) and `mark_done` (default `true`: a successful run marks the item done).
See [Actions protocol](#actions-protocol).

`context:` (no default; omit it and the Context pane is off): `argv` (list, no
shell) and `timeout_s` (2 to 60, default 20; a timeout stops the command with
`SIGTERM`, then `SIGKILL` 5 s later). See [Context protocol](#context-protocol).

`summary:` (no default; omit it and the summary is off; `summary: {}` enables
it with these defaults):

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | `haiku` | the model of the one-call run |
| `timeout_s` | `90` | 30 to 300 for the whole bridge call |
| `max_budget_usd` | `0.05` | 0.005 to 2: the bridge's spend limit for one call |

Urgent rule keys (all keys of one rule must match; any rule makes an event
urgent): `source` (the event's source, e.g. `gcal`), `kind`, `key_prefix`
(alerts), `title_regex`, `starts_within_h` (calendar start time).

### Thread order

The thread pane lists the **newest** message first by default, so a long
thread opens on the latest reply, with the pane scrolled to the top. `v`
flips it to oldest first and back. In newest-first order a dim "thread start"
marker closes the list under the first (parent) message of a thread of two or
more messages (left out when the item has a `thread_note`, which may be saying
that the thread is cut). The item header, the `meta.thread_note` line and the "RSVP not
confirmed" hint stay on top in both orders. The queue is not affected: this
only changes how the thread pane draws the item's `thread` (which an item
always carries oldest first).

`thread_order` in `inbox.yaml` is the starting order. The key saves the choice
in `state/inbox-state.json` (a `prefs` entry beside the marks, 0600), and once
you have pressed `v` the saved choice wins over `inbox.yaml` after a restart.
(`b`, the Hebrew toggle, is per session and is not saved.)

### Hebrew and RTL

`bidi_mode`: `logical` passes text through (the terminal runs bidi),
`visual` wraps and then reorders each line with python-bidi (the terminal does
not). `auto` picks by `$TERM_PROGRAM` (or `LC_TERMINAL=iTerm2`, which survives tmux
inside iTerm2): iTerm2 does its own bidi, so it maps to `logical`; unknown
terminals fall back to `visual`. `$INBOX_BIDI` overrides
`inbox.yaml`, and `b` toggles at runtime. Without python-bidi `visual` degrades
to `logical`. All of this lives in `workspace_daemon/inbox/bidi.py`; the
`AUTO_TABLE` there is the one place to record a terminal's behaviour.

## Keys

`j`/`k` move, `g`/`G` ends, `1`-`4` jump to a section, `tab` switches pane,
`/` fuzzy filter, `f` source filter, `a` approve or ack, `e` edit draft,
`d` done, `s` snooze, `n` nudge, `o` open link, `u` undo, on a pending invite
`a`/`X`/`T` accept, decline or answer tentative (needs `rsvp:`), `m` save to
memory (needs `save:`; marks the item done), any key set in `actions:`
(needs `actions:`), `E` react to the message (needs `react:`), `P` polish the draft (needs `polish:`), `a`/`n` also send to Slack when `send:` is set, `c` context pane (needs `context:`), `C` fetch its
context again, `i` AI summary and `I` a new one (needs `summary:`), `r` refresh
everything now (inside the context pane, fetch its context again),
`R` show or hide resolved alerts, `A` show all (see below), `S` saved view
(read-only; `o` opens, `esc` returns), `y` copy the item, `Y` copy its section,
`L` audit log, `b` Hebrew order, `v` thread order (newest or oldest first;
saved), `ctrl+t` theme, `ctrl+p` palette, `?` help,
`q` quit.

### Show all (`A`)

`A` toggles "show all": while on, the header shows `all` and every `command`
source with `supports_all: true` is polled as `<argv> list --all`; other
sources are unaffected. It polls at once, is off at every start and is never
saved. Items revealed by the toggle do not raise "new urgent" alerts.

### Copying

Copying uses the terminal clipboard escape (OSC 52), so the terminal must
allow it (iTerm2: "Applications in terminal may access clipboard"; it does not
work in macOS Terminal). The text is built from the item's own logical
strings, never from the bidi-reordered display and never with markup.

- `y`: `sender — where — gist`, then the link on its own line when there is one.
- `Y`: every item in the focused item's section as the screen lists it
  (filters applied), one per line: `sender — where — gist`, plus ` — link` when
  there is one.
- Palette: "Copy item", "Copy section", "Copy section as Markdown" (one
  `- **sender** (where): gist [link](url)` line per item; Markdown specials
  in the text are escaped).
- Mouse: a drag in the thread or draft pane selects text and stays inside
  that pane; `ctrl+c` copies it. The queue is a Textual `OptionList`, which
  does not support selection (it draws lines without text offsets), so use
  `y`/`Y` there. In visual bidi mode mouse selection is off in both panes
  (the screen text is already reordered); `y`/`Y` still copy logical text.

## Versioning

The inbox has its own version, separate from the rest of the daemon, in
`workspace_daemon/inbox/__init__.py` (`__version__`). It follows
[SemVer](https://semver.org) and covers the inbox UI and its source protocol.

- PATCH: fixes.
- MINOR: new features, keys or settings.
- MAJOR: breaking changes to the item format, the source protocol or the config.

Every PR that changes `workspace_daemon/inbox/` adds an entry to
[inbox-changelog.md](inbox-changelog.md) under `[Unreleased]`, or bumps
`__version__` with a matching new heading. At release time the version is
bumped and `[Unreleased]` is moved under the new heading. Releases are tagged
`inbox-vX.Y.Z` on main after the merge.

CI enforces this on pull requests: `tools/check_inbox_version.py BASE_REF`
fails when any file under `workspace_daemon/inbox/` changed since the merge
base but neither `__version__` nor `docs/inbox-changelog.md` did.

The Item format has its own `v` field (currently 1), independent of the app
version. `workspace_daemon/__init__.py` has a `__version__` too: that is the
daemon's version, separate from the inbox's. `daemon.py inbox --version`, the
header and the help overlay show the inbox version.

## Safety model

- Nothing is sent as a reply unless `send:` is configured (0.6.0): without it
  the confirm modal, undo window and quit-cancels-sends only run in `--demo`
  and `LocalActions` refuses. With it the send follows the RSVP pattern (see
  "Slack send").
- The writes (RSVP, save, send, react, custom actions) are off until configured, always confirm
  first (Cancel focused), wait out the undo window, are cancelled by quitting
  inside it, and run in a worker. RSVP, send and react can only make the one pinned tool call
  with exactly the pinned arguments, once and never retried (a send's
  destination comes only from the validated `meta.reply_to`); save passes the
  item as JSON on stdin, never in argv or in a prompt. A running write counts in
  the quit guard and results reach the UI without blocking it.
- Polish never sends: its tone call is read-only and pinned, its rewrite has no
  tools, and the result is only text you accept, edit or cancel.
- The Context pane and the summary are read-only and never ask for
  confirmation. The context command is yours and must be read-only; the summary
  runs the bridge with no tools at all and treats item text as data.
- Every action lands in the audit log (`L`).
- `open` launches only plain `http(s)` links.
- Sources run with your privileges: only configure commands you trust.

## Keeping private data out of the repo

`tools/privacy_check.py` scans tracked files; see the README ("Tests") for the
local denylist and the pre-commit hook. Demo and fixture data must be invented.
