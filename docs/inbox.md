# Unified inbox

`python daemon.py inbox` is a keyboard-first terminal inbox over several
sources. This page is the contract: the item format, the source-command
protocol, the built-in `events` source, local state and settings.

Version: see [docs/inbox-changelog.md](inbox-changelog.md); `python daemon.py
inbox --version` prints it.

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
`<title> - <when> - from <organizer>`. `meta` is
`{"kind": "invite", "event_id", "calendar_id", "series", "my_rsvp", "start",
"source_id"}` so a later RSVP action has what it needs. Actions: `ack` (hides
the row locally) and `open` (the meeting link from the cache, only when it is a
plain http(s) URL; otherwise there is no `open`). Titles and organizers are
untrusted display text. A missing cache means no invites and no error; an
unreadable or unfamiliar one is a source error (`events.invites`) and a cache
older than 6 hours marks the source stale.

Calendar `details.start`, `end` and `changed` become the gist suffix and thread
lines. A Gmail thread id (the second `:`-segment of
`gmail:<thread>`, `gmail:<thread>:followup-open` or `gmail:<thread>@<message>`)
becomes a link
`https://mail.google.com/mail/u/<gmail_account>/#all/<thread id>`; other
events have no link yet.
Items are `fyi`/`digest` unless an urgent rule matches. By default
`budget-hit:*`, `connector:*` and `attention:*` alerts are urgent. Titles and
people are untrusted (see above).

### Mail label filter

`email_require_label: MY_LABEL` (events source, default off) keeps only the
Gmail events that were captured through that label.

The event stream does not record a thread's labels, nor whether a capture came
from a `saved_request` routine (`details` is empty for Gmail events), and the
inbox never calls Gmail. So the filter works from what the daemon *does* keep:
each event names its `routine`, and the routine's config holds its search
query. The inbox reads `<base>/routines/*.yaml` and accepts a Gmail event only
when its routine is a `gmail_mcp` routine whose `query` is a plain conjunction
with a positive `label:MY_LABEL` term (case-insensitive; no `OR`, braces or
parentheses, and `-label:MY_LABEL` does not count). Every thread such a routine
captured matched the query, so it carried the label when it was captured.

What it does not prove: the label is checked at capture time, not now (removing
the label later changes nothing), and a routine whose query selects the label
in another way (a label id, a union) does not match. If no routine matches,
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
marks, no actions, nothing is written.

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
| `sources` | the `events` source | list of `type: events` and `type: command` sources (below) |

The `events` source takes `window_h` (1 to 720, default 48), `urgent` rules,
and:

| Key | Default | Meaning |
| --- | --- | --- |
| `invites_days` | `7` | 0 to 60: show unanswered calendar invites starting within this many days (0 turns them off; see "Pending calendar invites") |
| `saved_days` | `2` | 1 to 30: how far back the Saved view (`S`) looks |
| `email_require_label` | `null` | a label name, e.g. `MY_LABEL`: show only mail captured through a routine whose query requires that label (see "Mail label filter"); `null` shows all mail |

A `command` source takes `argv` (list), `timeout_s` (1 to 120) and
`supports_all` (`true` or `false`, default `false`): declare it only if
`<argv> list --all` works.

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
`R` show or hide resolved alerts, `A` show all (see below), `S` saved view
(read-only; `o` opens, `esc` returns), `y` copy the item, `Y` copy its section,
`L` audit log, `b` Hebrew order, `ctrl+t` theme, `ctrl+p` palette, `?` help,
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
