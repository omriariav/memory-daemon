# workspace-daemon

A small scheduled automation that scans Gmail for messages matching declarative
rules, summarizes each match with an LLM, writes a markdown note into an
Obsidian vault, and then triages the message in Gmail (label / mark read /
unstar / archive).

**Adding a new routine is a drop-in YAML file, never a code change.**

```
query ──▶ gws ──▶ LLM (yoetz) ──▶ note ──▶ ledger ──▶ triage ──▶ ledger outcome
```

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

Both binaries are found on `PATH`. To pin them explicitly (useful under
launchd, which does not inherit a login shell's `PATH`):

```sh
export WORKSPACE_DAEMON_GWS_BIN=/path/to/gws
export WORKSPACE_DAEMON_YOETZ_BIN=/path/to/yoetz
```

```sh
pip3 install pyyaml
gws auth login          # Gmail scopes
yoetz models list       # confirm your provider/model resolves
```

> **Model note:** keep `max_output_tokens` at 4096 or above. Reasoning models
> spend the budget on thinking tokens before emitting visible output, and a
> lower cap truncates the summary mid-sentence.

## Usage

```sh
./daemon.py list                              # routines, enabled state, last run
./daemon.py validate                          # check all routine YAML
./daemon.py run --dry-run                     # preview, zero side effects
./daemon.py run --routine weekly-report       # run one routine for real
./daemon.py run                               # run everything enabled
./daemon.py new                               # interactive scaffold
```

`--dry-run` makes **no** LLM call, **no** Gmail mutation, and **no** file or
state write. It still queries Gmail (a read) so the preview reflects reality.

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
  kind: gmail                  # only gmail today
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

Hourly, and once at load. Render the template, add your key, load it:

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
during triage cannot cause a second summary of the same item on the next run.

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

## Operational notes

- A failure on one message is caught, logged, and does not abort the run; the
  same is true for a failing routine.
- `state/processed.json` is the dedupe ledger. Delete an entry to force a
  message to be reprocessed. It is gitignored — it is local runtime data.
- The ledger grows without bound; there is no pruning. At roughly 280 bytes per
  entry and a few dozen items a month, that is a non-issue for years.
- Your own `routines/*.yaml` are gitignored too, since queries, addresses and
  vault paths are personal. Only `_template.yaml` and `_example-*.yaml` ship.
