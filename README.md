# memory-daemon

*(formerly `workspace-daemon`)*

A small scheduled automation that runs declarative capture and maintenance
routines over Gmail, Google Drive docs, Google Chat, Slack, local meeting
transcripts, and Google Tasks. Capture routines can distill each match with an
LLM and sink the result into one or both of:

- a **markdown note** in an Obsidian vault (documents), and/or
- a **[personal-memory](https://github.com/vladimanaev/personal-memory) entry**
  (a local RAG memory store), written through the store's own CLI so source-id
  dedup, index sync and versioning all apply. Model output is validated before
  it touches the store: entry types against the store's enum, person slugs
  against the store's known-slug list (unknown ones are dropped, never minted),
  and every entry is tagged `auto-captured` for later review.

Gmail matches can additionally be triaged (label / mark read / unstar / archive).
Maintenance routines perform deterministic work such as Slack conversation
discovery and bidirectional Google Tasks synchronization without an extraction
LLM.

**Adding a new routine is a drop-in YAML file, never a code change.** A routine
may own one source, combine several transports under one domain prompt, or keep
one scheduler per medium while deterministic source queries select specialized
handlers inside it.

```
source (gws / Slack / local files) ──▶ LLM (yoetz) ──▶ vault note and/or memory
                                                   ──▶ ledger and triage
```

See `routines/_example-slack-to-memory.yaml` for the Slack→memory shape.

## Operating model

The daemon now favors **one general owner per communication medium**, with
deterministic routing for known source shapes, instead of a growing collection
of overlapping domain routines:

| Plane | Responsibility |
|---|---|
| General capture | Gmail, Google Chat, and Slack sweeps cover incoming and outgoing work and apply each connector's general memory-worthiness prompt. |
| Deterministic capture | Exact sender/subject rules claim known Gmail traffic such as recurring reports and meeting notes before the general prompt. |
| Specialized capture | Guarded local-transcription ingestion handles material that cannot be discovered reliably through a communication connector. |
| Maintenance | Slack conversation discovery and Google Tasks synchronization run without an extraction LLM. |

The exact production routine files, source identifiers, addresses, labels,
filesystem paths, and connector prompt overrides are intentionally private and
gitignored. The examples in this repository document the schema; the local
`routines/*.yaml` files are the authority for an installed daemon. Never copy
private scope into an issue, pull request, README example, test fixture, or log
excerpt intended for publication.

For the architectural history, incidents that shaped the invariants, and the
handoff checklist for future agents, read [`REFLECTION.md`](REFLECTION.md).

### Where the extraction prompt lives

A routine's `analyze.instruction` can be written inline, or sourced from the
memory store's connector file:

```yaml
analyze:
  instruction_from_connector: slack   # <store>/memory/connectors/slack.md,
                                      # falling back to <store>/connectors/slack.md
  connector_sweep: true               # only on the routine that owns the
                                      # configured connector-wide sweep
  instruction_extra: >-               # optional, appended to it
    Stream-specific guidance for this routine.
```

The connector body *is* the extraction prompt ("what is memory-worthy in
Slack"), and personal-memory's web UI edits exactly that file — so prompt
tuning is a browser edit that the next run picks up, with no config change, and
interactive agent sessions reading the same connector apply identical judgment.
The connector's `fetch:` frontmatter belongs to personal-memory's interactive
pull workflow; memory-daemon does not use it for source enumeration. The
routine's `source:`/`sources:` block is authoritative for daemon coverage.

`instruction_from_connector` selects a prompt; it does not by itself claim
source-wide coverage. Set `connector_sweep: true` only on the routine that owns
the configured connector-wide sweep. After every enabled routine scope for that
source has completed successfully—even when every candidate was already seen or
rejected as noise—the daemon advances `memory connectors mark-pulled` to the
oldest successful scope checkpoint. A non-due owner therefore holds the safe
watermark instead of creating a silent gap. Partial and inline specialized
routines do not advance connector health on their own. Connector coverage
sources use `max_results: 0`; a bounded source or a reported upstream service
cap holds the watermark until complete coverage succeeds. Gmail, Slack, and Google Chat
sweep publishers also require `catch_up: true`; Google Chat requires
`max_per_space: 0`.

Use it for **general sweeps of a source**. Keep an inline `instruction` when the
routine is a **specialized job** on that source (mining a recurring report,
restructuring meeting transcripts) — the store holds one prompt per source, not
one per job. Missing connector, unreadable file, or a stub body fails at
`daemon.py validate` rather than mid-run.

The ledger is keyed by source item id, so an item is summarized once however
broad the query or however often the daemon runs. It is written before triage
and updated after it, which is what makes both halves recoverable — see
[Crash safety](#crash-safety).

### Deterministic handlers inside a medium sweep

Known source types should not be guessed by an extraction model. A broad Gmail
sweep can keep exact sender/subject queries for meeting notes, recurring reports,
or other deterministic queues and select a named handler for each:

```yaml
sources:
  - kind: gmail
    query: 'from:meeting-notes@example.com in:inbox'
    max_results: 0
    handler: meeting-notes
    actions: [apply_label, mark_read, archive]

  - kind: gmail
    query: '{newer_than:1d is:unread is:starred}'
    max_results: 0
    actions: []

handlers:
  meeting-notes:
    analyze:
      instruction: Extract decisions, named actions, and open questions.
      pick_label: true
    output:
      vault_dir: /absolute/path/to/vault/inbox
      slug_prefix: meeting-notes
    memory:
      type: meeting
      tags: [meeting-notes]

analyze:
  provider: gemini
  model: gemini/gemini-3.1-pro-preview
  instruction_from_connector: gmail
  connector_sweep: true
memory:
  store: /absolute/path/to/personal-memory
  type: note
  tags: [gmail, general-sweep]
```

Source blocks are evaluated in declaration order. If several blocks in the
same routine match one thread, the first block owns it; put narrow deterministic
queries first and the general source last. Handler source blocks require
`max_results: 0`, because a capped exact query cannot prove that overflow items
belong to the fallback. The deliberate exception is Gmail's managed
self-forwarded Chat queue: an active queue claim beats ordinary source order so
its todo lifecycle cannot be lost. That lifecycle must use the routine-level
default profile, not a handler. These choices happen before any LLM call.

The selected handler may override `analyze`, `output`, `memory`, `label`, and
`streams`; provider/model, store paths, and other omitted mapping fields are
inherited from the medium routine. An inline handler instruction replaces the
general connector prompt rather than appending to it. Exact
`operator_confirmed_source_ids` remain routine-level because an archived source
must be replayable through the default source even after its original handler
query no longer returns it.

Gmail actions remain on the source block, so mailbox mutation is tied to the
same deterministic match. Logs, vault frontmatter, and ledger records include
`handler_id`, while scheduling, connector health, and `rule_id` remain attached
to the single medium routine. See
[`_example-medium-handlers.yaml`](routines/_example-medium-handlers.yaml) for a
complete meeting-note plus recurring-report example.

## Requirements

| | |
|---|---|
| **Python 3.9+** | stdlib only, plus `pyyaml` |
| **[`gws`](https://github.com/omriariav/workspace-cli)** — workspace-cli, an unofficial Google Workspace CLI | provides Gmail, Chat, Drive/Docs, directory, Calendar, and Tasks access. Authenticate with the scopes required by the enabled routines: `gws auth login` (developed against v1.41.0) |
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

For complete joined-conversation discovery and content reads, the recommended
Slack user-token scopes are:

| Purpose | Scopes |
|---|---|
| Conversation metadata | `channels:read`, `groups:read`, `im:read`, `mpim:read` |
| Message and thread content | `channels:history`, `groups:history`, `im:history`, `mpim:history` |
| Identity resolution | `users:read`, `users:read.email` |

The daemon does not use Slack search for its general sweep, so `search:read` is
not required. It also does not write Slack messages, so `chat:write` and
`mpim:write` are not required by memory-daemon. An optional external mentions
helper may have its own authorization requirements.

`gws` and `yoetz` are found on `PATH`. To pin them explicitly (useful under
launchd, which does not inherit a login shell's `PATH`):

```sh
export WORKSPACE_DAEMON_GWS_BIN=/path/to/gws
export WORKSPACE_DAEMON_YOETZ_BIN=/path/to/yoetz
export WORKSPACE_DAEMON_ADA_BIN=/path/to/ada
export WORKSPACE_DAEMON_NPX_BIN=/stable/node/bin/npx
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
./memory-daemon-status.sh                     # scheduler + health of every routine
./daemon.py validate                          # check all routine YAML
./daemon.py run --dry-run                     # preview; data and state unchanged
./daemon.py run --routine weekly-report       # run one routine for real
./daemon.py run                               # run everything enabled
./daemon.py tick                              # run only routines whose cadence is due
./run.sh                                      # load scheduler + start one due-routine tick
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
schedule:
  every: 4h                   # base/off-hours cadence; integer + m, h, or d
  work_hours:                 # optional faster local cadence
    every: 15m
    days: [mon, tue, wed, thu, fri]
    start: "08:00"
    end: "18:00"
    timezone: Europe/London
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
  # Exact, explicit user overrides for known false-negative source verdicts.
  # Only these canonical ids bypass worthiness vetoes; all others stay filtered.
  # A previously triaged Gmail thread is replayed directly with no Gmail actions.
  operator_confirmed_source_ids: [gmail:example-thread-id]
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

After each source completes successfully, `state/cursors.json` records when that source scan
started. The next scan begins one overlap before that checkpoint. The processed
ledger skips unchanged daily versions, while messages that arrived during the
previous run remain eligible. A source, analysis, or memory error holds the
affected source's cursor without holding successful unrelated sources; catch-up
items ledgered with a memory error are retried. Before the
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

Set `session_gap_minutes` (for example, `120`) on daily GChat or direct Slack
digests to split long-separated conversation bursts into distinct durable
memories. The first session keeps the historical daily source id; later
sessions get stable `:session:<first-message-time>` suffixes.

Gmail supports the same durable cursor. Keep the catch-up `query` free of
`newer_than:`, `older_than:`, `after:`, and `before:`; the daemon appends its
own `after:` boundary. Use a timeless `queue_query` for Inbox items that must
remain eligible regardless of age:

```yaml
kind: gmail
query: '{in:inbox in:sent}'
queue_query: 'in:inbox {is:unread is:starred}'
max_results: 0
catch_up: true
catch_up_overlap: 1h
catch_up_after: "2026-08-01T00:00:00Z"
```

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
reply_roots_after: "2026-06-28T08:00:00Z"
```

The direct channel reader exhausts channel history from `reply_roots_after`,
uses the cursor to select new activity, and expands affected threads through
Slack's replies API. This is necessary because Slack does not return an old
root from channel history merely because it received a new reply. Set the root
floor to the beginning of the scope's first run. The reader rebuilds each
affected UTC activity day before updating its stable memory entry. Candidate
versions include a content fingerprint, so widening the historical boundaries
reprocesses a day only when the rebuilt content actually changes. The API-read
cost grows with the root-history floor even though unchanged candidates never
reach the LLM.

`catch_up_after` is the exclusive bootstrap boundary before the first
successful cursor checkpoint. Recurring entries use a distinct
`slack:<channel>:daily:<date>` namespace, so a partial cutover day cannot replace
or absorb legacy first-run coverage. Catch-up rejects `ada_channels`, because a
curated, capped summary cannot prove complete coverage. Mentions still use
Ada's search integration; if a cursor falls more than 30 days behind or the
result reaches Ada's 100-item limit, the run fails closed and asks for a manual
backfill instead of silently advancing.

The Slack cursor key fingerprints the configured channels, boundaries, and
mentions flag. Expanding that scope therefore bootstraps from
`catch_up_after`; it never inherits a checkpoint from a smaller channel set.

To build an initial review list without relying on memory or a hand-maintained
inventory, run the read-only Slack census:

```sh
python3 -m workspace_daemon.slack_cli census \
  --hours 48 \
  --requests-per-minute 40 \
  --checkpoint state/slack-census.json
```

It enumerates conversations joined by the authenticated user, scans a bounded
thread-root horizon (30 days by default), and detects both new roots and new
replies to older roots. It prints only active conversation and thread-root
metadata—never message text. The checkpoint is resumable across laptop sleep
and contains IDs, names, types, timestamps, and API errors only. The default
40-request/minute throttle bounds this process's history probes; other clients
using the same app and workspace still share Slack's method-level rate bucket.
The census honors Slack's `Retry-After` response when throttled. Reusing the
path resumes an interrupted census; once a census is complete, the next
invocation refreshes the inventory and starts a new fixed time window. A new
reply to a thread whose root predates the census window is not discoverable
through `conversations.history`; use the normal routine's wider
`reply_roots_after` scan after choosing the recurring scope.

For unattended use, schedule the metadata refresh as its own daily maintenance
routine (see `routines/_example-slack-census.yaml`):

```yaml
id: example-slack-census
enabled: false
role: maintenance
schedule:
  every: 1d
maintenance:
  kind: slack_conversation_census
  checkpoint: state/slack-census.json
  hours: 48
  requests_per_minute: 40
```

The frequent fallback consumes that checkpoint. This discovers joined public
channels, private channels, DMs, and group DMs with recent top-level activity,
while explicit channels declared by domain routines remain excluded:

```yaml
kind: slack
active_conversations:
  checkpoint: state/slack-census.json
  hours: 48
  refresh_every: 1d
  requests_per_minute: 40
  refresh_if_stale: false
max_results: 0
catch_up: true
catch_up_overlap: 1h
catch_up_after: "2026-07-28T08:00:00Z"
reply_roots_after: "2026-06-28T08:00:00Z"
```

A completed checkpoint is reusable until `refresh_every` elapses. With
`refresh_if_stale: false`, a missing, stale, incomplete, future-dated, or
incompatible checkpoint fails the content sweep loudly; it never hides a long
census inside a frequent run. The daily maintenance routine writes fixed-window
snapshots and is resumable on real runs; its dry run performs the real metadata
reads but writes no checkpoint. `refresh_every` must not exceed `hours`, and
changing `hours` invalidates an otherwise fresh cache. Freshness is measured
from the snapshot's upper boundary, not from when a long census happened to
finish. An interrupted checkpoint built for a different window is restarted
rather than resumed.
Stale inaccessible conversation rows are reported as warnings, while
permission, authentication, and other coverage errors fail the sweep closed.
The active set is read through the normal direct-history path, so both incoming
and outgoing messages are eligible and ledgered daily versions prevent
unchanged overlap from reaching the model twice.

The daemon durably records the upper boundary of each census snapshot it
consumes. Before consuming the next snapshot it compares that boundary with
the new snapshot's cutoff. This catches discovery gaps even when an earlier
run reused a cache that was still fresh but hours old. A gap fails closed and
holds both content and discovery cursors; run a manual broader
census/backfill before resuming. Boundaries retain microsecond precision and
adjacent windows include their shared endpoint, avoiding a timestamp sliver
between snapshots. Connector health uses this actual snapshot boundary, not
the later daemon start time. Slack's history API also cannot discover a new
reply whose root predates the census window in a conversation that had no
recent top-level message; pin such critical channels explicitly with
`direct_channels`.

### Bidirectional Google Tasks sync

A maintenance routine can synchronize Google Tasks with open `todo` entries in
a personal-memory store without an LLM. Google task ids become canonical
`google-tasks:<list-id>:<task-id>` source ids, and a private checkpoint records
the last content hash seen on each side:

```yaml
id: google-tasks-sync
enabled: false
role: maintenance
schedule:
  every: 1d
  work_hours:
    every: 1h
    days: [sun, mon, tue, wed, thu]
    start: "08:00"
    end: "20:00"
    timezone: Asia/Jerusalem
maintenance:
  kind: google_tasks_sync
  checkpoint: state/google-tasks-sync.json
  store: /absolute/path/to/personal-memory
  tasklists: all
  outbound_tasklist: replace-with-task-list-id
  outbound_since: "2026-01-01"
  exclude_tags: [no-google-tasks]
  max_tasks: 10000
```

Open Google tasks are imported from the selected lists; new open memory todos
on or after the required `outbound_since` boundary are created in the outbound
list. Title, notes, due date, and completion state synchronize in both
directions. Google completion creates a following memory note, while memory
resolution completes the Google task. Historical completed Google tasks are
not imported during bootstrap. A configured `exclude_tags` match is an ongoing
opt-out: even an already-linked entry is skipped without changing either side.
Google Tasks does not expose a creation timestamp. On first import, the sync
therefore uses the task's authoritative `updated` date for the memory entry and
records the full initial `updated` timestamp in the generated metadata. Both
values remain stable on later edits instead of being replaced by the run date.
A new association fails closed if Google does not return a valid timestamp.
Titles that contain no ASCII slug characters receive a deterministic id based
on the canonical Google Task source id, avoiding empty or colliding filenames.
The sync also performs conservative, model-free identity enrichment: it links
only person slugs that already exist in the private graph, using exact full
names or an unambiguous capitalized first name. Unknown and ambiguous names are
left unlinked. Person links are excluded from the bidirectional content hash,
so enrichment can never cause an outbound Google Tasks edit. Use the optional
`identity_exclude_people` list to suppress specific existing slugs. Identity
enrichment is also restricted to private-only entries; an entry materialized
in any shared graph is reported and skipped so private identity resolution is
never propagated into a shared copy.

If both sides changed since the previous checkpoint, neither is overwritten.
The run reports a conflict and fails closed. A retry after an interrupted write
can recover automatically when the two sides already agree. The configured
`gws` CLI cannot clear an existing Google due date, so that specific change is
also surfaced as a conflict. Dry runs perform real reads and log every planned
import, export, update, completion, link, or conflict without writing either
system or the checkpoint. Immediately before a write, the sync re-reads both
sides and aborts if either changed since discovery. Neither the current `gws`
CLI nor the personal-memory CLI exposes a conditional-write/expected-hash
flag, so a very small race remains between each last read and its subsequent
write. A divergence that remains visible is caught as a conflict on the
following run.

`daemon.py run` is manual and ignores cadence. `daemon.py tick` is the
scheduler entrypoint: it reads `schedule.every` plus an optional timezone-aware
`schedule.work_hours` override, runs due owners sequentially under the existing
global lock, and records attempts in `state/schedule.json`. Outside the declared
days and half-open `start`–`end` window, the base cadence applies. Entering a
work window can make a routine immediately due; leaving it restores the base
interval. A failed dependency is retried on that routine's current cadence, not
on every coordinator wake-up. A dry-run tick never updates schedule state.
Routines without `schedule.every` retain the legacy hourly cadence; the template
sets `4h` explicitly for new routines.

See `_example-domain-routine.yaml`, `_example-fallback-sweep.yaml`, and
`_example-google-tasks-sync.yaml`.

## Adding a routine

1. `cp routines/_template.yaml routines/my-routine.yaml` — or run `./daemon.py new`
2. Edit the file. Every field is documented inline in the template.
3. `./daemon.py validate`
4. `./daemon.py run --routine my-routine --dry-run`
5. Drop `--dry-run` when the preview looks right.

Files starting with `_` are ignored by the loader, so the template and the
examples stay inert.

Keep a new unattended routine at `enabled: false` while testing it against an
already-running scheduler. An explicit manual preview can run that parked
routine without arming it:

```sh
./daemon.py run --routine my-routine --dry-run --include-disabled
```

The flag requires one `--routine`; it can never run every disabled routine.

### Routine schema

```yaml
id: weekly-report              # defaults to the filename stem
enabled: true
description: Summarize the weekly report email.

source:
  kind: gmail                  # gmail, drive_docs, slack, gchat, or mila
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

`slug_prefix` and the literal text in `filename_template` are filename-only:
path separators and `..` are rejected, and format specifications/conversions
are intentionally unsupported. The rendered name must remain one direct file
inside `vault_dir`. Before writing, the daemon resolves that location and pins
the validated directory through the atomic replacement, so a symlink cannot
redirect the write.

**Actions** run in order after the note is written:
`apply_label`, `mark_read`, `mark_unread`, `star`, `unstar`, `archive`.
Use `[]` to leave the mailbox untouched.

### Scoping: the inbox as a work queue

Dedupe is keyed on item id, so a broad query is safe from *re*-processing. It is
not safe from *first*-processing — an unscoped query summarizes everything it
matches on the first run, which is real money and a flooded vault.

For a general attention sweep, set `read_thread: true` on the Gmail source.
The daemon then supplies chronological thread context, including From/To/Cc,
instead of only the latest message. Context is bounded to the newest 50 messages
and 120,000 characters; truncated input is labeled as partial so the model
cannot mistake it for complete coverage. A new reply still has a new Gmail
message id for ledger dedupe, while the memory source id remains the thread id,
so later replies update the existing thread memory rather than duplicating it.
Do not combine this with `streams.*.message_updates: true`: that mode treats
each reply as an independent recurring report, so validation rejects the two
thread models together.

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

### Mila meeting transcriptions

`source.kind: mila` reads completed recordings without modifying Mila's live
storage. It uses `recordings.json` as the index, structured segments as the
preferred transcript, and the paired `.txt` as a fallback. Native Mila meeting
filenames carry the recording start; for imported Voice Memos, the original
`createdAt` is the start. This distinction prevents a 30-minute meeting from
being matched to Calendar 30 minutes late.

Before normal analysis, the source reads nearby Calendar events and asks Yoetz
to select only from that bounded candidate list. A capture proceeds only when
the matcher returns `matched: true`, a supplied event ID, and `confidence:
high`. Medium, low, missing, or invented matches never reach memory.

Mila's directory is read-only. The daemon writes small, private receipts under
`state/transcriptions/processed/` and `state/transcriptions/failed/`; it never
moves audio or transcript sidecars and never edits `recordings.json` or
`folders.json`. The status command reports low-confidence matches as needing
attention. An inconclusive match is retried on the routine's next run so a
corrected Calendar event can resolve it; success atomically clears older
rejections for that stable recording source.

Candidate IDs include the transcript hash, so a corrected transcript is
reprocessed. The memory source ID remains `mila:<recording-uuid>`, so that
correction updates the same memory entry. For an established library, list
already-handled UUIDs in `exclude_recording_ids`; genuinely new recordings are
still discovered even when an iOS Voice Memo is imported days later.

For a copied or renamed legacy transcript, `manual_recordings` points at the
explicit `.srt`/`.txt` pair and the Mila index that still owns its metadata.
That provides a trustworthy UUID and time without hand-editing the active app
database. See `_example-mila-to-memory.yaml`.

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

## Scheduling (macOS LaunchAgents)

Two lightweight coordinators wake every 15 minutes. The capture job runs Gmail,
Google Chat, Slack, and local capture routines; the maintenance job runs
metadata work such as the long Slack conversation census. Separating them means
a census cannot delay a due 15-minute capture. Routines that are not due make
no source or LLM calls. Both templates deliberately use `RunAtLoad: false`, so
installation itself never triggers a real run. Render both templates, add the
provider key only to the capture plist, and leave activation to the explicit
`run.sh` step below:

```sh
# The template uses this stable link so Node upgrades do not break launchd.
# Point it at the version prefix that contains bin/node and bin/npx.
mkdir -p ~/.local
ln -sfn "$(dirname "$(dirname "$(command -v node)")")" ~/.local/node-current

sed "s|__REPO_DIR__|$PWD|g; s|__PYTHON__|$(command -v python3)|g; s|__HOME__|$HOME|g" \
  launchd/com.memory-daemon.plist.template \
  > ~/Library/LaunchAgents/com.memory-daemon.plist
sed "s|__REPO_DIR__|$PWD|g; s|__PYTHON__|$(command -v python3)|g; s|__HOME__|$HOME|g" \
  launchd/com.memory-daemon-maintenance.plist.template \
  > ~/Library/LaunchAgents/com.memory-daemon-maintenance.plist

# Replace REPLACE_ME with your provider API key, then keep both rendered
# definitions private. The maintenance job does not receive the provider key.
$EDITOR ~/Library/LaunchAgents/com.memory-daemon.plist
chmod 600 ~/Library/LaunchAgents/com.memory-daemon.plist \
  ~/Library/LaunchAgents/com.memory-daemon-maintenance.plist

# One-time cleanup when upgrading from the former workspace-daemon label.
launchctl bootout gui/$(id -u)/com.workspace-daemon 2>/dev/null || true
mv ~/Library/LaunchAgents/com.workspace-daemon.plist \
  ~/.Trash/com.workspace-daemon.plist.retired 2>/dev/null || true
```

Do not load the plist directly during installation: `StartInterval` would make
it run about 15 minutes later even with `RunAtLoad: false`. When you explicitly
want to turn the daemon on and choose its first tick, run:

```sh
./run.sh
```

The helper validates every routine, reloads both LaunchAgents from their current
plists, clears launchd disable overrides, and starts one capture and one
maintenance coordinator tick immediately. Each tick runs every **enabled**
routine in its group that is due; the helper never changes a routine's
`enabled` setting. It is safe to call again when the schedulers are loaded.

Check on it:

```sh
./memory-daemon-status.sh
tail -f logs/run.log
```

Operational logs rotate at 20 MiB with five backups. Runtime state, logs, and
non-example routine files are written owner-only (`0600`) under owner-only
directories (`0700`).

`memory-daemon-status.sh` is read-only. It reports both coordinators and shows
each routine's declared role
(`general`, `domain`, `specialized`, `partial`, or `maintenance`) and source
connectors,
distinguishes a last scheduled attempt from a last captured item, shows when
each routine is next due, and flags an unfinished last run, memory-sink
failures, or pending Gmail triage. `partial` means a connector sweep still has
an explicitly bounded scope; legacy routines without `role` display `-`.
The `ARMED` column says whether a routine is enabled independently of its
current `STATUS`: `in-tick`, `due`, `waiting`, `attention`, or `disabled`.
`in-tick` means the routine was selected for the current batch; it does not
claim that every selected routine is executing simultaneously. An armed
scheduler is loaded and will keep checking its schedule; `tick running` means
its coordinator process is executing now. `LAST CAPTURE` is derived from the
capture ledger; maintenance-only routines display `N/A` because the field does
not apply to them. Use `LAST ATTEMPT`, `STATUS`, and `ISSUES` to judge their
health.
Copy it to a directory on `PATH` to call it from anywhere:

```sh
cp ./memory-daemon-status.sh ~/bin/memory-daemon-status.sh
```

The copied command finds the repository at `~/Code/memory-daemon`; set
`MEMORY_DAEMON_DIR` if your checkout lives elsewhere. It exits non-zero when
the LaunchAgent or a routine needs attention, so it can also be used by a
separate monitor. Set `MEMORY_DAEMON_LAUNCHD_LABEL` (or pass `--label`) if the
installed job uses a different label.

Rollback both jobs with:

```sh
launchctl bootout gui/$(id -u)/com.memory-daemon
launchctl bootout gui/$(id -u)/com.memory-daemon-maintenance
```

The rendered plist holds an API key and absolute paths, so `launchd/*.plist` is
gitignored — only the template is tracked.

## Layout

```
daemon.py                  CLI entrypoint
memory-daemon-status.sh    scheduler and per-routine health
REFLECTION.md              architectural retrospective and agent handoff
workspace_daemon/
  config.py                routine discovery, loading, validation
  shell.py                 binary resolution, subprocess, logging
  gmail.py                 gws Gmail adapter
  drive.py                 gws Drive/Docs adapter, tab reading, doc lookup
  contacts.py              exact Workspace-directory identity resolution
  slack_cli.py             built-in read-only Slack Web API client
  slack_source.py          Slack thread discovery and rendering
  mila_source.py           read-only Mila + Calendar matching
  google_tasks_sync.py     deterministic Google Tasks ↔ memory todo sync
  llm.py                   yoetz adapter, prompt building, label extraction
  notes.py                 frontmatter + note writing
  actions.py               declarative Gmail triage actions
  state.py                 processed.json
  runner.py                the run loop
routines/                  one YAML per routine (yours are gitignored)
launchd/                   capture + maintenance LaunchAgent templates
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

Configured memory sinks and source expansion are mutation barriers: Gmail
actions are withheld and the item is retried until the memory entry and any
required expanded document are successfully persisted.

**A partial write cannot corrupt anything.** Notes and the ledger both go through
a temp file plus `os.replace`, with the containing directory fsynced so the
rename itself survives power loss. An unreadable or wrong-shaped ledger raises a
clear error rather than a traceback — and does not tempt you to delete it, since
an empty ledger means re-summarizing and re-triaging everything still matched.

**Work that mutates the same state cannot overlap.** Capture and most
maintenance work take `state/run.lock`. The long Slack conversation census uses
its own `state/slack-census.lock`, so it cannot block frequent capture and can
overlap it safely; its checkpoint is replaced atomically. Manual and scheduled
runs use the same lock mapping. A second run in an occupied lock group exits
cleanly and logs exactly which routines it skipped, while independent groups
continue. Dry runs never take either lock.

`flock` binds to an inode, not a path, so deleting an active file under
`state/*.lock` leaves the holder guarding an orphan and lets another run lock a
freshly created file. There is no rendezvous left to defend at that point, so
the holder checks before each item and stops rather than racing on. Don't delete
lock files while their work is running.

## Tests

```sh
python3 -m unittest discover -s tests   # no gws/yoetz needed
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
