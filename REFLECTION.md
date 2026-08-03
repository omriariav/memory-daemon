# Memory Daemon Retrospective and Agent Handoff

- **As of:** 2026-08-03
- **Audience:** maintainers and future coding agents
- **Scope:** durable architectural and operational context, not a live health
  snapshot

This document explains why memory-daemon looks the way it does. The README is
the reference manual; this is the project memory: the path taken, the failures
that exposed missing invariants, and the rules that should survive future
refactors.

## Public/private boundary

This repository is public. Do not put employer-specific or personal material
in tracked files, GitHub issues, pull requests, test fixtures, screenshots, or
copied logs. In particular, keep these private:

- organization and product names;
- real email addresses and subject lines;
- Slack channel, DM, group-DM, user, and workspace identifiers;
- Google Chat space/member identifiers;
- Gmail labels and private routing rules;
- connector prompt overrides, captured content, vault paths, and graph data;
- OAuth tokens, API keys, rendered LaunchAgent files, and runtime state.

Production routine files without a leading underscore are gitignored. Private
connector overrides live with the memory store. Rendered plists live in
`~/Library/LaunchAgents`, outside this repository. A future agent should inspect
those local files when authorized, but summarize them publicly only in generic
terms.

## Where we started

The first working daemon had four independently grown capture routines:

- a recurring-business-report Gmail job;
- a meeting-notes Gmail job;
- a selected Slack-channel sweep;
- a selected Google Chat space sweep.

They worked in isolation but had never been designed or operated as a set.
Their prompts came from different places, their source windows meant different
things, source overlap could be decided by timing, and none had been trusted
unattended. Gmail actions were especially consequential because a successful
analysis could archive the source email.

The initial operating discipline was therefore intentionally phased:

1. design the routine set without running it;
2. validate and dry-run every routine separately;
3. wet-run one routine at a time and inspect the created memories;
4. arm the scheduler only after every routine had a good real run.

That sequence remains the safest way to introduce any new source, mutation, or
large historical backfill.

## Where the architecture landed

The deployment converged on six responsibilities, expressed through private
routine files:

| Responsibility | Role | Why it exists |
|---|---|---|
| Gmail general sweep | General capture | Owns incoming and outgoing email, timeless attention queues, and exact deterministic handlers. |
| Google Chat general sweep | General capture | Dynamically discovers recent active spaces, including unnamed group chats, and captures incoming and outgoing context. |
| Slack general sweep | General capture | Reads active joined conversations plus explicitly pinned coverage through Slack history/replies APIs. |
| Local meeting transcripts | Specialized capture | Matches a recording to Calendar and captures it only after a high-confidence match. |
| Google Tasks sync | Maintenance | Synchronizes open memory todos and Google Tasks without an extraction model. |
| Slack conversation census | Maintenance | Builds the metadata-only active-conversation inventory consumed by the frequent Slack sweep. |

The important conceptual change was moving from “one routine per topic” toward
“one reliable owner per medium.” Privacy, people, product, and other durable
signals can appear anywhere. A specialized domain routine can reject an item
that no other routine can reclaim once ownership is ledgered. General prompts
therefore perform semantic triage, while deterministic rules continue to claim
traffic that can be recognized safely from source metadata.

This is not a ban on domain routines. Use one only when it has a genuinely
distinct source boundary or output contract that earns the ownership cost.

## Prompt placement

The settled split is:

- **General source sweeps** read the connector body from the memory store. The
  same browser-editable prompt then governs interactive pulls and unattended
  capture.
- **Specialized transformations** keep inline instructions in the routine.
  Recurring reports, meeting-note restructuring, and similar jobs are not the
  general meaning of “memory-worthy Gmail.”
- **Deterministic recognition happens before the LLM.** Exact senders,
  subjects, and source shapes select a handler; the model does not decide which
  pipeline owns the item.

Connector frontmatter configures the personal-memory pull workflow. Daemon
source enumeration comes from routine YAML. Confusing these two authorities was
one of the earliest sources of unauditable coverage.

## Source-specific lessons

### Gmail

The general sweep covers both incoming and outgoing messages. A timeless Inbox
queue keeps unread or starred work eligible even when it is older than the
normal cursor window. Calendar invitations and known bulk producers are
excluded before content reaches the model.

Known recurring mail and meeting-note notifications are deterministic handlers
inside the Gmail owner. Gmail mutations remain attached to those source blocks;
the general fallback is read-only unless a separate decision explicitly arms
actions.

Forwarding a Google Chat message to one's own Inbox is an explicit follow-up
signal, not a duplicate. It receives a separate todo lifecycle while preserving
the original durable memory. The canonical thread memory must never be replaced
by the follow-up todo.

Two bugs established the current follow-up invariants:

1. Reusing the Gmail thread source ID overwrote durable memories with temporary
   follow-up todos. Follow-ups now use a separate `:followup-open` identity and
   completion entries link through `follows`.
2. The dedicated follow-up listing accidentally inherited the broad general
   queue query and cursor window. Ordinary Gmail was misclassified as managed
   follow-up work. The dedicated listing now strips general query state and
   executes only the exact self-forward query.

Repair required restoring affected graph entries from a known pre-run commit,
removing false todo/completion artifacts through the memory CLI, repairing only
the affected ledger rows, rebuilding/verifying the index, and rerunning first in
dry mode. Keep that recovery pattern.

### Google Chat

A static space allowlist cannot cover newly created group chats. An urgent
same-day thread exposed this: the graph looked healthy while an unnamed group
chat was never considered. The general sweep now discovers spaces dynamically
by recent activity and includes unnamed group chats. Configured spaces are pins,
not the only enumeration source.

Coverage must be visible. Logs record discovered, excluded, considered, and
captured scope. Connector `last_pulled` advances after a complete sweep even when
all candidates are noise or already seen. It does not advance after partial
coverage.

Daily space entries use stable source IDs and content fingerprints. New replies
update the appropriate daily/session entry instead of freezing the thread at
first capture or duplicating unchanged overlap.

### Slack

The Slack reader was moved into this repository so source behavior and daemon
behavior could evolve together. The general solution does not depend on Slack
search. It uses joined-conversation metadata plus `conversations.history` and
`conversations.replies`, covering public channels, private channels, DMs, and
group DMs when the token has the corresponding read/history scopes.

A daily, metadata-only census discovers conversations with recent activity. The
frequent content sweep consumes that checkpoint and reads message bodies through
the direct API path. The census and capture sweeps are separate because a long
inventory must not delay latency-sensitive work.

Old thread roots are a structural Slack limitation: channel history does not
surface an old root merely because it gained a new reply. The reader therefore
scans roots back to an explicit floor and expands affected threads. Pin critical
channels explicitly when their reply history must not depend solely on recent
conversation discovery.

An optional mentions helper remains useful but capped. A reported cap or a
missing coverage scope fails closed; it must never advance the cursor and claim
complete coverage.

### Meeting material

Meeting notes are high-value because they preserve decisions, named actions,
open questions, and commitments. Notification email bodies are often only
stubs, so the deterministic Gmail handler expands the linked/located Drive
document and treats the transcript as authority when one exists. It preserves
the meeting date rather than the daemon run date.

For local recordings, the daemon treats the transcription application's folder
as a live external database. It never moves or edits the application's files.
Small receipts under daemon state mark processed or failed recordings. Calendar
matching is bounded to real candidate events and must return high confidence;
otherwise nothing is sent to memory and the recording remains retryable.

### Google Tasks

Tasks synchronization is deterministic and bidirectional. Its canonical source
identity is the composite `google-tasks:<list-id>:<task-id>`, because task IDs
are scoped by their list. The checkpoint holds hashes for both sides, so
one-sided changes propagate and simultaneous divergence fails closed as a
conflict.

Google Tasks does not expose a creation timestamp. Initial imports preserve its
authoritative `updated` time rather than pretending the daemon run time is the
task's origin. Completion is represented as a following memory event rather
than silently rewriting history.

This is maintenance, not capture. It can create or update tasks and memory
todos, but it does not write the daemon capture ledger. Consequently the status
table displays `N/A` under `LAST CAPTURE`; use `LAST ATTEMPT`, `STATUS`, and
`ISSUES` for its health.

## Unattended-service invariants

These rules are more important than any individual implementation:

1. **One owner before analysis.** Routing is deterministic and happens before
   the processed-ledger check or LLM call. Ambiguous equal-ranked ownership is
   an error, not a race.
2. **One stable source identity per durable thing.** Replies may update an
   existing memory; temporary workflows such as follow-ups need their own
   identity and timeline link.
3. **Cursor advancement proves coverage.** Advance only after an uncapped,
   successful source sweep. Hold the affected cursor on fetch, analysis,
   memory, or completeness errors.
4. **Unread/starred queues outlive cursor windows.** An item that still requires
   attention must not disappear merely because the laptop slept or the item is
   older than one interval.
5. **Required sinks precede source mutation.** If memory or required document
   expansion fails, Gmail actions are withheld and the item remains retryable.
   Archive must never make failed work undiscoverable.
6. **Dry run is observational.** It performs real source reads, but no LLM call,
   source mutation, vault write, memory write, checkpoint update, or cursor
   advancement.
7. **Long census work cannot block capture.** The Slack census has a separate
   LaunchAgent and lock group. Google Tasks is shorter maintenance and shares
   `state/run.lock` with capture, so overlap can defer a capture tick until the
   next coordinator wake-up.
8. **Failures are attributable.** Status names the affected routine and counts
   its last-run errors. A healthy later tick clears the operational error.
9. **Identity is verified, never invented.** Source addresses are resolved
   exactly through Workspace directory data; unknown or ambiguous people are
   omitted and marked for later enrichment.
10. **Public artifacts contain no private scope.** Tests use synthetic names and
    IDs. Runtime files, prompt overrides, logs, and rendered plists stay local.

## Scheduling model

Two LaunchAgents wake every 15 minutes:

- `com.memory-daemon` runs due capture routines;
- `com.memory-daemon-maintenance` runs due maintenance routines.

The coordinator interval is only a polling resolution. Each routine owns its
actual cadence, including optional timezone-aware work hours. A routine marked
`due` may wait until the next coordinator wake-up. `armed` means enabled and
eligible; it does not mean a process is continuously running.

Both plists intentionally use `RunAtLoad: false`. Installation must not cause an
unreviewed wet run. `./run.sh` is the explicit activation boundary: it validates,
loads both agents, and starts one tick for each coordinator.

launchd does not inherit a login shell. The rendered capture plist must expose
stable paths for Python, `gws`, `yoetz`, optional mentions tooling, and Node/npm
used by the memory CLI. Use a stable Node symlink rather than a version-pinned
NVM directory. A missing Node runtime can otherwise let source reads and Gmail
analysis succeed while every memory write fails; required Gmail triage remains
withheld for retry.

## What review and wet runs taught us

- An exit code is not enough. After every wet run, inspect the actual memory
  entries for specificity, dates, type, people links, source IDs, and accidental
  overwrites.
- A memory CLI non-zero exit can still mean the entry was written or a near
  duplicate was rejected. Verify by canonical source ID before declaring a sink
  failure or retrying blindly.
- A syntactically valid summary can still be poor. One captured todo preserved
  only “provide input” and lost the concrete questions. It was repaired in place
  under the same source ID. Quality review is part of correctness.
- First runs need a checkpoint. Before a large wet run, preserve the graph commit
  and copy the processed ledger. Recovery should be targeted, not a broad reset.
- Independent review repeatedly found real unattended-service gaps: incomplete
  coverage, misleading status, stale cursors, lock behavior, source mutation
  ordering, and test dependencies. Green CI alone is not sufficient.
- Every review finding, including small hardening suggestions, is resolved before
  merge. The regression suite is the accumulated record of shipped mistakes.

## Standard change workflow

For a routine or source-behavior change:

1. Run `./memory-daemon-status.sh` and inspect `git status`; preserve unrelated
   and untracked user files.
2. Create a branch. Never develop directly on `main`.
3. Inspect the private routine and the relevant connector prompt without copying
   private values into tracked files.
4. Make the smallest change and run `python3 daemon.py validate`.
5. Run the affected routine with `--dry-run`. Review candidate counts, ownership,
   source IDs, intended actions, and completeness—not only the exit code.
6. Before a risky first/backfill run, checkpoint the memory graph and ledger.
7. Wet-run one routine. If it can mutate a source, restate the exact mutation
   immediately before execution.
8. Inspect created/updated memories and the source afterward. Repair poor output
   under the same canonical source ID.
9. Run focused tests, the full suite, static checks, and example validation as
   appropriate.
10. Open a PR, obtain independent review, resolve every finding, and wait for CI.
11. Merge, return to `main`, and use `./run.sh` only when the live scheduler
    should be armed.
12. Confirm a healthy post-change tick with the status command and logs.

Manual `daemon.py run` ignores cadence and does not update scheduled-attempt
state. That is useful for controlled testing but explains why status may still
show a routine as due immediately afterward. A later scheduled tick reconciles
the schedule state and deduplicates already-processed items.

## Debugging order

Use this sequence before changing code or deleting state:

```sh
./memory-daemon-status.sh
tail -n 200 logs/run.log
python3 daemon.py validate
```

Then:

1. Find the latest tick ID and the first attributed error for the affected
   routine.
2. Distinguish source failure, analysis failure, sink failure, pending Gmail
   action, incomplete coverage, and scheduler failure.
3. Inspect only the relevant rows in `state/processed.json`,
   `state/cursors.json`, `state/schedule.json`, or the maintenance checkpoint.
4. Check the memory store by source ID before treating a logged sink error as a
   missing entry.
5. Verify connector `last_pulled` and `last_captured` separately.
6. Retry the smallest routine or item scope. Do not delete the whole ledger.

Do not remove an active `state/*.lock`: `flock` protects an inode, and replacing
the path can admit a second writer while the first still holds the orphaned
lock. Do not broadly reset a dirty worktree or memory graph. Runtime and graph
history are recovery assets.

## Known limitations and future work

- The daemon has health reporting and non-zero status, but no independent alert
  delivery. A dead laptop or a scheduler that never wakes still needs an
  external monitor.
- Daily memory-store backup is an external concern. Continue taking explicit
  checkpoints before risky wet runs until automated backup is available.
- Slack cannot discover every reply to arbitrarily old roots without a bounded
  root scan. Critical conversations should remain explicitly pinned.
- Optional mention discovery has an upstream result cap; large backlogs require
  manual backfill rather than cursor advancement.
- Connector prompts are live configuration. They can improve without a daemon
  deployment, but a poor browser edit can change unattended judgment just as
  quickly. Review frontmatter and body together.
- Successful processed-ledger rows accumulate and currently have no general
  prune policy. Individual rows can still be updated or removed during normal
  retry/resolution flows.
- Google Tasks lacks conditional writes and a creation timestamp; conservative
  conflict detection reduces but cannot eliminate the last-read/write race.
- Status reports when maintenance last ran, not whether it changed anything.
  `LAST CAPTURE` is deliberately `N/A` for maintenance routines.

## First ten minutes for the next agent

1. Read this file and the relevant README section.
2. Run `git status` and do not touch unrelated or untracked files.
3. Run `./memory-daemon-status.sh`; do not assume a historical “green” report is
   still current.
4. Run `python3 daemon.py validate`.
5. Inspect the local gitignored routines to learn the installed scope. Keep all
   identifiers private.
6. Read only the connector prompt needed for the task; prompt bodies are runtime
   policy.
7. Check the latest attributed errors in `logs/run.log` before proposing a fix.
8. Confirm whether the requested action is read-only, a dry run, a wet run, a
   source mutation, or scheduler activation. Those are different authorities.
9. Use a branch and preserve the memory graph, ledger, and user worktree.
10. Leave the system with a healthy status tick—or state plainly why it is not
    healthy.

The core lesson is simple: a memory daemon is not primarily a summarizer. It is
a coverage, ownership, identity, durability, and recovery system that happens to
use a model for judgment. Treat those surrounding guarantees as the product.
