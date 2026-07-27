---
name: manage-memory-connector-prompts
description: Safely list, inspect, create, edit, or remove source-wide connector prompt overrides used by memory-daemon. Use when a user wants to tune general Gmail, Slack, Google Chat, Drive, or other connector extraction judgment in a selected personal-memory store without changing specialized inline routine prompts.
---

# Manage Memory Connector Prompts

Manage private connector overrides through the bundled transactional helper.
Connector prompts express general source-wide memory judgment; specialized
jobs on a source should keep their instructions inline in the routine.

## Resolve paths and intent

1. Resolve the daemon checkout from a user-provided path or the current project.
   Require an absolute path containing `daemon.py`, `routines/`, and
   `workspace_daemon/`.
2. Resolve the selected personal-memory store from the user's request or the
   relevant routine's `memory.store`. If several stores are possible, ask.
3. Resolve `../../scripts/memory_daemon_admin.py` relative to this `SKILL.md`
   and use its absolute path as `ADMIN`.
4. Read [connector-prompts.md](references/connector-prompts.md) before creating,
   editing, or removing an override.

Do not move a specialized routine instruction into a connector merely to make
prompt sourcing uniform.

## Inspect safely

List prompt names, layers, and body sizes without printing their bodies:

```sh
python3 "$ADMIN" prompt list --store "$MEMORY_STORE"
```

Inspect one prompt's metadata:

```sh
python3 "$ADMIN" prompt inspect \
  --store "$MEMORY_STORE" \
  --name CONNECTOR_NAME
```

Treat store paths and prompt bodies as private. Do not publish them in issues,
commits, PR descriptions, or examples.

## Create or edit an override

1. Run `python3 daemon.py validate` in the daemon checkout. Stop if the baseline
   is invalid.
2. Work on a scratch candidate outside both repositories. For an edit, copy
   `<store>/memory/connectors/<name>.md` so formatting and frontmatter survive.
3. If only `<store>/connectors/<name>.md` exists, use `add` to create a private
   override; never edit the tracked generic template by accident.
4. Keep the prompt source-wide: define durable facts to capture and noisy
   material to discard. Do not include credentials or organization-specific
   examples in a generic template.
5. Plan without mutating:

```sh
python3 "$ADMIN" prompt plan \
  --repo "$DAEMON_REPO" \
  --store "$MEMORY_STORE" \
  --operation add \
  --name CONNECTOR_NAME \
  --candidate "$CANDIDATE"
```

Use `--operation edit` when a private override already exists.

6. Show the user the proposed diff and exact connector name. Stop and wait for
   approval.
7. Apply the unchanged plan with the emitted `plan-token`:

```sh
python3 "$ADMIN" prompt apply \
  --repo "$DAEMON_REPO" \
  --store "$MEMORY_STORE" \
  --operation add \
  --name CONNECTOR_NAME \
  --candidate "$CANDIDATE" \
  --token PLAN_TOKEN
```

The helper rejects stale plans, writes one override atomically, runs
`daemon.py validate`, and restores the previous file if validation fails.

## Remove an override

Plan the removal:

```sh
python3 "$ADMIN" prompt plan \
  --repo "$DAEMON_REPO" \
  --store "$MEMORY_STORE" \
  --operation remove \
  --name CONNECTOR_NAME
```

Explain whether a generic template remains and will become active. Wait for
confirmation of the exact connector name, then apply:

```sh
python3 "$ADMIN" prompt apply \
  --repo "$DAEMON_REPO" \
  --store "$MEMORY_STORE" \
  --operation remove \
  --name CONNECTOR_NAME \
  --token PLAN_TOKEN \
  --confirm-target CONNECTOR_NAME
```

Removing an override must not remove the connector template, routines,
processed ledger, captured notes, or memory entries.

## Finish

Report the changed override and successful daemon validation. Offer a targeted
dry run for a routine that consumes the connector, but do not run it. Never
perform a wet run or start the scheduler implicitly.
