# Unified inbox

`python daemon.py inbox` is a keyboard-first terminal inbox over several
sources. This page is the contract: the item format, the source-command
protocol, the built-in `events` source, local state and settings.

Phase 1a (this version) reads and triages. Nothing is sent: `reply` and
`nudge` are part of the format but the UI refuses them until phase 2.

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
| `meta` | object | opaque to the inbox; handed back to the source unchanged by `act` |

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

## Source-command protocol

A `command` source is any program, run without a shell (`~` at the start of an
argument is expanded):

- `<argv...> list` prints to stdout
  `{"v": 1, "items": [...], "stale": bool, "age_min": number|null, "error": str|null}`.
  `stale` says the source's own data is old; `age_min` is how old; `error` is
  a message the UI shows without hiding the items.
- `<argv...> act` reads `{"v": 1, "action": str, "id": str, "meta": object, "text"?: str}`
  on stdin and prints `{"ok": bool, "message": str}`.

Phase 1 calls only `list`, with the configured timeout, stdin closed, in its
own process group (killed whole on timeout). stdout is read in chunks and the
group is killed as soon as it passes 4 MB; only the last 64 KB of stderr are
kept, so a chatty source cannot exhaust memory. `timeout_s` must be below
`poll_interval_s` (the default adapts to short intervals). Output over 4 MB, non-JSON output,
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

Polls never overlap: a poll that is still running when the next one is due (or
when you press `r`) is skipped, and `r` says "Poll already running". Each poll
first re-reads the state file, so marks made in another pane show up.

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
| `gmail` | `email` | `mail` | `open` (reply arrives in phase 2) |
| `gcal` kinds `new`, `update`, `cancel`, `rsvp` | `calendar` | `calendar` | `ack` |
| `mila`, `zoom` | `recording` | `mila` or `zoom` | `ack` |
| `daemon` kind `alert` | `alert` | `daemon` | `ack` |

`open` is listed only when the item has a link (the UI also hides it for any
item with a null `link`, whatever a source says). Today only Gmail events have
one, so the other rows are `ack` only.

Calendar `details.start`, `end` and `changed` become the gist suffix and thread
lines. A Gmail thread id (the second `:`-segment of
`gmail:<thread>`, `gmail:<thread>:followup-open` or `gmail:<thread>@<message>`)
becomes a link
`https://mail.google.com/mail/u/<gmail_account>/#all/<thread id>`; other
events have no link yet.
Items are `fyi`/`digest` unless an urgent rule matches. By default
`budget-hit:*`, `connector:*` and `attention:*` alerts are urgent. Titles and
people are untrusted (see above).

### Stale and recovered alerts

An alert stays urgent only while it is relevant. Three rules apply to every
alert (`daemon` kind `alert`), after the urgent rules above:

1. **Newest per key.** Alerts are grouped by `details.key`; only the newest
   of each key is listed, so an older alert for the same condition never
   shows beside it.
2. **Age.** An alert older than `alert_urgent_max_age_h` (default 6, range 1
   to 168) drops to `fyi`/`digest`. It stays listed until `window_h` passes.
3. **Recovery.** An alert whose condition has since ended is *resolved*: it is
   `fyi`, carries `meta.resolved: true`, and is hidden unless `R` is on
   (`R` toggles "show resolved"; the header counts the hidden ones). Resolved
   rows show a green check.
   - `attention:<routine>`: resolved when the run log (`logs/run.log`, read
     the way `status` reads it) shows the routine's latest result as `ok` and
     later than the alert. This record exists for every routine kind,
     maintenance routines (calendar-cache, sheet-watch) included, because it
     comes from the scheduler tick and not from the capture ledger. Only
     alerts whose `details.issues` are tick-derived (`last run`, `N last-run
     errors`, `incomplete`, `overlap`) resolve this way; an alert about the
     ledger (`memory sink`, `source expansion`, `Gmail triage`, `meeting
     match`) is not proven fixed by a clean tick, so it only ages out.
   - `connector:<server>`: resolved when `state/claude-usage.jsonl` holds a
     successful call that used that connector, later than the alert.
   - Every other key (`budget-*`, `hold:*`, `usage-unreadable`) has no
     recovery signal: age rule only.

Recovery is read-only and best effort. A missing file, an unfamiliar shape (a
different daemon version) or any read error means "unknown", and the alert
keeps only the age rule. Limits: a routine whose latest tick result is
something other than `ok` (a later `skipped`, `paused`, `incomplete` or
`error`) is not resolved, because only the latest result per routine is
kept; and both logs are read in full on each poll that has an alert needing
them.

## Local state

`<base>/state/inbox-state.json`:
`{"v": 1, "items": {"<id>": {"done": ts, "ack": ts, "snooze_until": ts, "updated": ts}}}`.

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
- `--demo` keeps marks in memory only and does not read `inbox.yaml`.

## Settings (`<base>/inbox.yaml`)

Gitignored and optional; copy `inbox.example.yaml`. A missing file means the
defaults; a broken one prints every problem and exits 2.

| Key | Default | Meaning |
| --- | --- | --- |
| `poll_interval_s` | `60` | 5 to 3600 |
| `theme` | `dark` | `dark` or `light` |
| `bidi_mode` | `auto` | `logical`, `visual` or `auto` (see below) |
| `gmail_account` | `0` | integer >= 0: the `/mail/u/N/` index in Gmail links, for when the account is not your first signed-in one |
| `alert_urgent_max_age_h` | `6` | 1 to 168: an alert older than this is `fyi`, not urgent (see "Stale and recovered alerts") |
| `sources` | the `events` source | list of `type: events` (`window_h`, `urgent` rules) and `type: command` (`argv` list, `timeout_s` 1 to 120) |

Urgent rule keys (all keys of one rule must match; any rule makes an event
urgent): `source` (the event's source, e.g. `gcal`), `kind`, `key_prefix`
(alerts), `title_regex`, `starts_within_h` (calendar start time).

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
`d` done, `s` snooze, `n` nudge, `o` open link, `u` undo, `r` poll now,
`R` show or hide resolved alerts, `y` copy the item, `Y` copy its section,
`L` audit log, `b` Hebrew order, `ctrl+t` theme, `ctrl+p` palette, `?` help,
`q` quit.

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

## Safety model

- Nothing leaves the machine in phase 1a. The confirm modal (destination and
  exact text, Cancel focused, `y` to send), the undo window and
  quit-cancels-sends exist so phase 2 sends are safe from day one; real
  sources cannot reach them yet (`LocalActions` refuses).
- Every action lands in the audit log (`L`).
- `open` launches only plain `http(s)` links.
- Sources run with your privileges: only configure commands you trust.

## Keeping private data out of the repo

`tools/privacy_check.py` scans tracked files; see the README ("Tests") for the
local denylist and the pre-commit hook. Demo and fixture data must be invented.
