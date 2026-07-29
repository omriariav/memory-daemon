# memory-daemon

*(formerly `workspace-daemon`)*

A small scheduled automation that sweeps your work sources — Gmail, Google
Drive docs, Slack channels and @mentions — against declarative routines,
distills each match with an LLM, and sinks the result into one or both of:

- a **markdown note** in an Obsidian vault (documents), and/or
- a **[personal-memory](https://github.com/vladimanaev/personal-memory) entry**
  (a local RAG memory store), written through the store's own CLI so source-id
  dedup, index sync and versioning all apply. Model output is validated before
  it touches the store: entry types against the store's enum, person slugs
  against the store's known-slug list (unknown ones are dropped, never minted),
  and every entry is tagged `auto-captured` for later review.

Gmail matches can additionally be triaged (label / mark read / unstar / archive).

**Adding a new routine is a drop-in YAML file, never a code change.** A routine
may own one source or combine several transports under one domain prompt.

```
source (gws / built-in Slack client) ──▶ LLM (yoetz) ──▶ vault note and/or memory
                                                    ──▶ ledger and triage
```

See `routines/_example-slack-to-memory.yaml` for the Slack→memory shape.

### Where the extraction prompt lives

A routine's `analyze.instruction` can be written inline, or sourced from the
memory store's connector file:

```yaml
analyze:
  instruction_from_connector: slack   # <store>/memory/connectors/slack.md,
                                      # falling back to <store>/connectors/slack.md
  instruction_extra: >-               # optional, appended to it
    Stream-specific guidance for this routine.
```

The connector body *is* the extraction prompt ("what is memory-worthy in
Slack"), and personal-memory's web UI edits exactly that file — so prompt
tuning is a browser edit that the next run picks up, with no config change, and
interactive agent sessions reading the same connector apply identical judgment.

Use it for **general sweeps of a source**. Keep an inline `instruction` when the
routine is a **specialized job** on that source (mining a recurring report,
restructuring meeting transcripts) — the store holds one prompt per source, not
one per job. Missing connector, unreadable file, or a stub body fails at
`daemon.py validate` rather than mid-run.

The ledger is keyed by source item id, so an item is summarized once however
broad the query or however often the daemon runs. It is written before triage
and updated after it, which is what makes both halves recoverable — see
[Crash safety](#crash-safety).

## Requirements

| | |
|---|---|
| **Python 3.9+** | stdlib only, plus `pyyaml` |
| **[`gws`](https://github.com/omriariav/workspace-cli)** — workspace-cli, an unofficial Google Workspace CLI | provides Gmail read/search/label/archive. Must be authenticated: `gws auth login` (developed against v1.41.0) |
| **[`yoetz`](https://github.com/avivsinai/yoetz)** — CLI LLM gateway | `brew install avivsinai/tap/yoetz`, then configure a provider key |

The Slack client is included in this repository. Put its user token in
`~/.config/memory-daemon/slack.json`:

```json
{
  "user_token": "xoxp-replace-me",
  "mention_user": "person@example.com"
}
```

Keep that file private (`chmod 600`). Set `MEMORY_DAEMON_SLACK_CONFIG` to use a
different path. `mention_user` is optional; when omitted, the client resolves
the authenticated user's email and therefore needs `users:read` and
`users:read.email`. The optional mentions integration shells out to `ada`;
ordinary channel, DM, and group-DM reads do not.

`gws` and `yoetz` are found on `PATH`. To pin them explicitly (useful under
launchd, which does not inherit a login shell's `PATH`):

```sh
export WORKSPACE_DAEMON_GWS_BIN=/path/to/gws
export WORKSPACE_DAEMON_YOETZ_BIN=/path/to/yoetz
```

```sh
pip3 install pyyaml
gws auth login          # Gmail scopes
yoetz models list       # confirm your provider/model resolves
python3 -m workspace_daemon.slack_cli auth-test
```

> **Model note:** keep `max_output_tokens` at 4096 or above. Reasoning models
> spend the budget on thinking tokens before emitting visible output, and a
> lower cap truncates the summary mid-sentence.

## Usage

```sh
./daemon.py list                              # routines, cadence, enabled state, last run
./daemon.py validate                          # check all routine YAML
./daemon.py run --dry-run                     # preview; data and state unchanged
./daemon.py run --routine weekly-report       # run one routine for real
./daemon.py run                               # run everything enabled
./daemon.py tick                              # run only routines whose cadence is due
./daemon.py new                               # interactive scaffold
```

`--dry-run` makes **no** LLM call, **no** source mutation, and **no** vault,
memory-store, or state write. It still performs real source reads so the preview
reflects reality, and appends to `logs/run.log` so failed previews remain
diagnosable.

## Codex and Claude Code plugin

This repository is also a marketplace for the dual-compatible
`memory-daemon-manager` plugin. Its shared skills can inspect, add, edit, and
remove routine YAML and private connector prompt overrides. Every mutation
shows a diff first, requires an unchanged plan token, validates the whole
daemon afterward, and rolls back on validation failure. The skills never run
the daemon or start its scheduler implicitly. The helper supports macOS and
Linux; Windows is outside memory-daemon's POSIX/launchd support boundary.

Install for Codex:

```sh
codex plugin marketplace add omriariav/memory-daemon
codex plugin add memory-daemon-manager@memory-daemon
```

Install for Claude Code:

```sh
claude plugin marketplace add omriariav/memory-daemon
claude plugin install memory-daemon-manager@memory-daemon
```

See [`plugins/memory-daemon-manager/README.md`](plugins/memory-daemon-manager/README.md)
for upgrade, uninstall, and recovery instructions.

## Domain routines, routing, and cadence

Use `source:` for a compact single-source routine. Use `sources:` when one
domain receives evidence through several transports:

```yaml
id: product-area
enabled: true
schedule: {every: 4h}         # integer + m, h, or d
routing: {priority: 50}       # lower wins between specific routines

sources:
  - kind: gmail
    query: '{from:product-group@example.com} {is:unread is:starred}'
    actions: [apply_label]    # chat items can never receive this
  - kind: slack
    channels: [C0123EXAMPLE]
    hours: 26
  - kind: gchat
    spaces: [spaces/AAAAEXAMPLE]
    hours: 26

label: Product Area
analyze:
  provider: gemini
  model: gemini/gemini-3.1-pro-preview
  instruction: Keep durable decisions, constraints, and commitments.
memory:
  store: /absolute/path/to/personal-memory
  type: note
  tags: [product-area]
```

Routing chooses a routine before analysis; an extraction prompt cannot hand an
item to a sibling routine after ownership is recorded. Give every domain prompt
an explicit boundary: name adjacent domains it does not own, state how to handle
mixed items, and require the exact `NOT MEMORY-WORTHY` token when no independently
in-scope fact remains. This prevents a broad people- or channel-based source
from permanently claiming material under the wrong domain prompt.

The legacy routine-level `actions:` key remains valid for `source:` routines.
With `sources:`, actions belong on the Gmail source block. Validation rejects
actions on Slack, Google Chat, and Drive sources.

Candidate listing happens before analysis. When several routines match the
same source item, ownership is resolved before the processed ledger is checked:

1. A specific routine always beats a routine with `routing.fallback: true`.
2. Within either class, lower `routing.priority` wins (default `100`).
3. Equal-ranked distinct routines are ambiguous. The item is skipped with an
   error instead of letting YAML filename order choose its prompt.

This makes a low-frequency, broad inbox sweep safe as a fallback:

```yaml
id: periodic-inbox-sweep
schedule: {every: 1d}
routing: {fallback: true}
sources:
  - {kind: gmail, query: '{is:unread is:starred}', actions: []}
  - {kind: slack, include_mentions: true, hours: 26}
  - {kind: gchat, all_spaces: true, hours: 26, max_results: 0, batch_messages: daily}
```

Even when only the fallback is due, enabled specific routines still list their
candidates for routing. An item waits for its owner; it is never captured under
the fallback prompt merely because that fallback ran first. If a specific
source cannot be listed, only overlapping candidates are held because ownership
cannot be proven; a Drive failure, for example, does not stop an unrelated Gmail
fallback. Ownership discovery also expands lower source caps to the largest
overlapping cap, while retaining the configured cap as that routine's processing
budget.

Broad Google Chat and Slack-mention fallbacks also exclude explicitly configured
spaces/channels from disabled routines. This lets routines be armed one at a
time without the fallback capturing a parked domain's traffic under the wrong
prompt.

For an hourly Google Chat fallback, `batch_messages: daily` creates one stable
source identity per space and UTC calendar day. New messages and replies update that
same daily memory instead of changing from a daily singleton to a separate
thread entry. `batch_unthreaded: daily` remains available when real multi-message
threads should retain their own stable identities. Daily candidates are
re-fetched from the start of their UTC day before analysis, so a short discovery
window or result cap cannot replace a complete memory with a partial slice.

Set `catch_up: true` on an uncapped all-space GChat daily batch when every
message must eventually be swept even after sleep or a long outage:

```yaml
catch_up: true
catch_up_overlap: 1h
```

After an error-free run, `state/cursors.json` records when that source scan
started. The next scan begins one overlap before that checkpoint. The processed
ledger skips unchanged daily versions, while messages that arrived during the
previous run remain eligible. A source, analysis, or memory error holds the
cursor; catch-up items ledgered with a memory error are retried. Before the
first successful run, `batch_messages_after` is the bootstrap boundary (or the
configured `hours` window is used when no boundary exists).

When changing an existing routine from thread batching to `batch_messages`,
set `batch_messages_after` to the last timestamp covered by the old mode:

```yaml
batch_messages: daily
batch_messages_after: "2026-07-28T06:46:03Z"
```

The boundary is exclusive. Pre-boundary messages stay under their legacy source
ids, while later messages use the new `gchat:<space>:daily:<date>` namespace.
Remove neither the boundary nor legacy ledger rows after cutover.

Slack supports the same durable queue for explicitly configured channels and
workspace-wide mentions. Use `direct_channels` for public or private channels
that should be read through Slack's history API, rather than summarized by Ada:

```yaml
kind: slack
direct_channels: [C0123EXAMPLE]
include_mentions: true
max_results: 0
catch_up: true
catch_up_overlap: 1h
catch_up_after: "2026-07-28T08:00:00Z"
```

The direct channel reader exhausts every history page after the cursor and
rebuilds a complete UTC-day digest before updating its stable memory entry.
`catch_up_after` is the exclusive bootstrap boundary before the first
successful cursor checkpoint. Catch-up rejects `ada_channels`, because a
curated, capped summary cannot prove complete coverage. Mentions still use
Ada's search integration; if a cursor ever falls more than 90 days behind, the
run fails closed and asks for a manual backfill instead of silently advancing.

`daemon.py run` is manual and ignores cadence. `daemon.py tick` is the
scheduler entrypoint: it reads `schedule.every`, runs due owners sequentially
under the existing global lock, and records attempts in
`state/schedule.json`. A failed dependency is retried on that routine's cadence,
not on every coordinator wake-up. A dry-run tick never updates schedule state.
Routines without `schedule.every` retain the legacy hourly cadence; the template
sets `4h` explicitly for new routines.

See `_example-domain-routine.yaml` and `_example-fallback-sweep.yaml`.

## Adding a routine

1. `cp routines/_template.yaml routines/my-routine.yaml` — or run `./daemon.py new`
2. Edit the file. Every field is documented inline in the template.
3. `./daemon.py validate`
4. `./daemon.py run --routine my-routine --dry-run`
5. Drop `--dry-run` when the preview looks right.

Files starting with `_` are ignored by the loader, so the template and the
examples stay inert.

### Routine schema

```yaml
id: weekly-report              # defaults to the filename stem
enabled: true
description: Summarize the weekly report email.

source:
  kind: gmail                  # gmail, drive_docs, slack, or gchat
  query: 'from:reports@example.com subject:"Weekly Report" is:unread'
  max_results: 20              # optional, default 20

analyze:
  provider: gemini             # yoetz provider
  model: gemini/gemini-3.1-pro-preview
  max_output_tokens: 4096      # optional, default 4096 — do not go below ~2048
  pick_label: true             # optional — let the model choose a Gmail label
  focus_domains: [Revenue, Churn]   # optional — injected into prompt + frontmatter
  instruction: >-
    Summarize strictly for wins, opportunities, risks and losses in the focus
    domains. Group by domain, say "none found" where nothing applies.

output:
  vault_dir: /absolute/path/to/vault/inbox
  slug_prefix: weekly-report-summary
  # filename_template: "{slug_prefix}-{date}-{title}"  # {slug_prefix} {date} {title} {id}
  # kind: email-scoop-summary                        # optional frontmatter override
  # tags: [kind/email-scoop-summary, status/inbox]   # optional frontmatter override

actions: [apply_label, mark_read, unstar, archive]
```

**Actions** run in order after the note is written:
`apply_label`, `mark_read`, `mark_unread`, `star`, `unstar`, `archive`.
Use `[]` to leave the mailbox untouched.

### Scoping: the inbox as a work queue

Dedupe is keyed on item id, so a broad query is safe from *re*-processing. It is
not safe from *first*-processing — an unscoped query summarizes everything it
matches on the first run, which is real money and a flooded vault.

The pattern that works well: scope on `in:inbox` and end the routine with
`archive`. The inbox becomes the queue, and archiving is what marks an item
done. `is:unread` is a trap if you tend to read mail before the daemon runs —
check the count first, since a filter that matches nothing fails silently.

### Pulling content from a linked Doc

Some notification emails are only a stub. Gemini meeting notes are the case this
was built for: the mail holds one tab of the notes and the link to the real
document exists solely in the HTML part, which `gws` strips — so there is no URL
to follow. `source.expand` locates the doc by title instead and swaps its tabs in
as the body:

```yaml
source:
  kind: gmail
  query: 'from:gemini-notes@google.com in:inbox'
  expand:
    kind: drive_doc
    title_from_subject: "Notes: '(?P<title>.+?)'"   # needs a (?P<title>) group
    name_contains: Notes by Gemini
    tabs: [Full notes, Transcript]
    on_missing: body        # or 'error' to skip and retry next run
```

For those meetings that is ~2k chars of email versus ~31k of document.

The daemon preserves structured identity candidates from source metadata:
Gmail `From`/`To`/`Cc` addresses, Google Chat membership records, and Drive file
owners after a successful document expansion. A memory sink resolves each exact
address through `gws contacts directory-search` before the extraction model may
link it. Verified Gmail and Chat identities become allowed candidates, and the
model selects only people materially involved in that memory; Drive owners are
linked directly. A verified person may be new to the store.

The authenticated Workspace account is identified through `gws drive about`
and excluded: personal memory should link the other people, not its own owner.
Fuzzy directory results are never accepted: the returned contact must contain
the requested email. Directory or account-identity failures do not discard the
capture; they skip unsafe enrichment and add `people-unmapped` for later repair.
This requires Workspace directory-read access and the source-specific metadata
scopes in the `gws` authorization.

Picking the **wrong** document is the worst thing this can do — a confident
summary of somebody else's meeting, filed in your vault. Three guards:

- A doc is accepted only if its **name starts with** the captured title *at a
  title boundary*, so `Roadmap review extended` never answers for
  `Roadmap review`. Drive search matches document bodies too, hence the check.
- When the item has a date, that date is **required**, not preferred — it is
  ANDed into the Drive query. A recurring weekly has one doc per occurrence, and
  silently substituting last week's is worse than finding nothing.
- Use a **greedy** `.+` in `title_from_subject`. A non-greedy `.+?` stops at the
  first apostrophe, so `Notes: 'Omri's weekly'` captures just `Omri` — a
  fragment broad enough to match an unrelated meeting.

When no doc is found, `on_missing: body` falls back to the email stub so the item
is never silently dropped. That is a real quality cliff, so it is never
indistinguishable from success: the note gets `expanded: false`, state records an
`expand_fallback` reason, and the run summary counts it. To find and re-run them:

```sh
grep expand_fallback state/processed.json
```

Set `on_missing: error` instead to skip the item entirely, leaving it in the
queue to retry on the next run.

`source.kind: drive_docs` also exists for searching Drive directly, with
`name_contains`, `mime_type` and `tabs`. It has no inbox to bound it and no
Gmail actions, so reach for the `expand` form unless you genuinely want every
matching document in your Drive history.

### One routine, several report streams

When a routine covers several recurring reports that differ only in what they
should be called and where they are filed, declare them — that is a lookup, not
a judgement call, so it should not go to the model:

```yaml
streams:
  "Weekly Report DACH TEAM":            # matches the SUBJECT
    title: Weekly - DACH                # stable name for the note + filename
    label: EMEA                         # Gmail label for this stream
  "Channel Business weekly report":
    title: Weekly - Channel Business
    label: CHANNELS
  "Regional report thread":
    title: Weekly - Regional
    label: EMEA
    message_updates: true                # every reply is a fresh report

output:
  filename_template: "{title}-{date}"   # -> weekly-dach-2026-07-19.md
```

Keys match the **subject**, which is the stable identity of a recurring report:
colleagues reply into these threads, so keying on the sender would miss those.
Prefix a key with `from:` to match the sender instead.

`title` matters more than it looks — raw subjects carry `RE:`/`FW:` prefixes and
their own embedded dates, which make for noisy, unsortable filenames
(`re-turkey-africa-baltics-weekly-updates-472026-2026-07-20.md`). A routine-wide
fixed `label:` also works when every item belongs under the same one.

Set `message_updates: true` only when a sender deliberately reuses one thread
and each new reply is itself a fresh report. In that mode the daemon analyzes
only the newest reply text (quoted history is removed), dates replies from
their Gmail header instead of a stale date in the reused subject, and gives
each message its own memory source ID. The default remains thread-oriented:
quoted context is preserved, an explicit subject date wins, and memory updates
the existing thread entry.

### Label caching

The user-label catalog is ~940 names and changes maybe monthly, so it is cached
in `state/labels.json` for 14 days rather than refetched every hour.

The usual trap with a TTL is staleness: create a label, reference it in a
routine, and get a false `does not exist in Gmail` until the cache expires. A
miss is therefore self-healing — the catalog refetches once before reporting a
name as unknown, so the only case that pays for a fetch is the one where the
cache is provably behind. Force it with `daemon.py run --refresh-labels`. A dry run reads the cache but
never writes it, keeping the no-state-write promise.

One caveat this does not solve: with `pick_label: true` the model chooses from
whatever is cached, so a label created in the last 14 days is not offered. It
degrades to no label rather than an error.

**Label safety.** With `pick_label: true` the full catalog of *user* labels is
passed into the same call that writes the summary, and the model returns a final
`LABEL: <name>` line. That line is stripped from the note body and the name is
resolved case-insensitively **against the real catalog** before Gmail sees it. A
hallucinated label resolves to nothing and `apply_label` is skipped — an
unvalidated name is never sent to Gmail.

## Output

`<vault_dir>/<slug_prefix>-<email date YYYY-MM-DD>.md`, suffixed with a short
message id if that file already exists. YAML frontmatter followed by the
summary:

```yaml
---
kind: email-scoop-summary
rule_id: weekly-report
source: gmail
gmail_message_id: 19f94d5c35713ea8
gmail_thread_id: 19f94d5c35713ea8
gmail_link: https://mail.google.com/mail/u/0/#inbox/19f94d5c35713ea8
email_from: reports@example.com
email_subject: Weekly Report
email_date: 'Thu, 24 Jul 2026 09:02:11 +0000'
focus_domains: [Revenue, Churn]
gmail_label_applied: Reports
generated_by: workspace-daemon (yoetz + gemini/gemini-3.1-pro-preview)
generated_at: '2026-07-26T10:29:32Z'
tags: [kind/email-scoop-summary, status/inbox]
---
```

## Scheduling (macOS LaunchAgent)

The LaunchAgent is a lightweight coordinator. It wakes every 15 minutes and
calls `daemon.py tick`; routines that are not due make no source or LLM calls.
The template deliberately uses `RunAtLoad: false`, so installation itself
never triggers the first real run. Render the template, add your key, then load
it:

```sh
sed "s|__REPO_DIR__|$PWD|g; s|__PYTHON__|$(command -v python3)|g; s|__HOME__|$HOME|g" \
  launchd/com.workspace-daemon.plist.template \
  > ~/Library/LaunchAgents/com.workspace-daemon.plist

# replace REPLACE_ME with your provider API key
$EDITOR ~/Library/LaunchAgents/com.workspace-daemon.plist

launchctl unload ~/Library/LaunchAgents/com.workspace-daemon.plist 2>/dev/null
launchctl load   ~/Library/LaunchAgents/com.workspace-daemon.plist
launchctl list | grep workspace-daemon
```

Check on it:

```sh
tail -f logs/run.log
tail -f logs/launchd.err.log
```

Unload with `launchctl unload ~/Library/LaunchAgents/com.workspace-daemon.plist`.

The rendered plist holds an API key and absolute paths, so `launchd/*.plist` is
gitignored — only the template is tracked.

## Layout

```
daemon.py                  CLI entrypoint
workspace_daemon/
  config.py                routine discovery, loading, validation
  shell.py                 binary resolution, subprocess, logging
  gmail.py                 gws Gmail adapter
  drive.py                 gws Drive/Docs adapter, tab reading, doc lookup
  contacts.py              exact Workspace-directory identity resolution
  slack_cli.py             built-in read-only Slack Web API client
  slack_source.py          Slack thread discovery and rendering
  llm.py                   yoetz adapter, prompt building, label extraction
  notes.py                 frontmatter + note writing
  actions.py               declarative Gmail triage actions
  state.py                 processed.json
  runner.py                the run loop
routines/                  one YAML per routine (yours are gitignored)
launchd/                   LaunchAgent template
state/  logs/              runtime, gitignored
```

## Crash safety

This runs unattended on a laptop that sleeps, so the interesting failures are
interruptions rather than exceptions. Four properties hold:

**An item is never summarized twice.** The ledger entry is written — atomically,
and fsynced — immediately after the note, before any Gmail action. A crash
during triage cannot cause a second summary. A crash in the narrower window
*before* the ledger write leaves an unledgered note, so the item is retried —
and the retry overwrites its own note rather than writing a second copy, because
collision is judged by the `item_id` in the note's frontmatter rather than by
mere filename existence.

**Triage is never silently half-applied.** The entry is first recorded with every
action `pending`, then updated with what actually succeeded. A failing action
does not abort the rest of the sequence; it is left in `actions_pending` and
retried at the start of the next run, by item id rather than by re-querying —
once `archive` lands, an `in:inbox` query can no longer see the item. Every
action is idempotent, so replaying a partial sequence is safe, and the retry
never re-summarizes. `daemon.py run` reports the count.

**A partial write cannot corrupt anything.** Notes and the ledger both go through
a temp file plus `os.replace`, with the containing directory fsynced so the
rename itself survives power loss. An unreadable or wrong-shaped ledger raises a
clear error rather than a traceback — and does not tempt you to delete it, since
an empty ledger means re-summarizing and re-triaging everything still matched.

**Two runs cannot overlap.** A real run takes an exclusive lock on
`state/run.lock`. launchd will not overlap a `StartInterval` job with itself, but
a manual `daemon.py run` alongside the scheduled one would otherwise have both
processes summarizing the same item. A second run exits cleanly, logging that it
skipped. Dry runs never take the lock.

`flock` binds to an inode, not a path, so deleting `state/run.lock` mid-run
leaves the holder guarding an orphan and lets another run lock a freshly created
file. There is no rendezvous left to defend at that point, so the holder checks
before each item and stops rather than racing on. Don't delete that file while a
run is going.

## Tests

```sh
python3 -m unittest discover -s tests   # 135 tests, no gws/yoetz needed
python3 -m pyflakes daemon.py workspace_daemon/ tests/ tools/
python3 tools/validate_examples.py      # the shipped template and examples
```

Every case in the suite is a bug that actually shipped and was caught in review:
note-collision ownership, a failed ledger write riding along with the next
successful one, wrong-shaped JSON, permission preservation, temp sweeping, lock
exclusion, and partial action failure with ordered retry. CI runs both on 3.9
and 3.12.

## Operational notes

- A failure on one message is caught, logged, and does not abort the run; the
  same is true for a failing routine.
- `state/processed.json` is the dedupe ledger. Delete an entry to force a
  message to be reprocessed. It is gitignored — it is local runtime data.
- The ledger grows without bound; there is no pruning. At roughly 280 bytes per
  entry and a few dozen items a month, that is a non-issue for years.
- Your own `routines/*.yaml` are gitignored too, since queries, addresses and
  vault paths are personal. Only `_template.yaml` and `_example-*.yaml` ship.
