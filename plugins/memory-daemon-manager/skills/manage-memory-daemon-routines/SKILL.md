---
name: manage-memory-daemon-routines
description: Safely list, inspect, add, edit, enable, disable, or remove memory-daemon routine YAML files. Use when a user wants to change routine sources, cadence, routing, prompts, sinks, labels, or Gmail actions in a local memory-daemon checkout, with a reviewed diff and post-change validation.
---

# Manage Memory Daemon Routines

Manage one routine file at a time through the bundled transactional helper.
Never run a routine, start the scheduler, or modify captured state as part of
routine administration.

## Resolve paths

1. Resolve the daemon checkout from a user-provided path or the current project.
   Require an absolute path containing `daemon.py`, `routines/`, and
   `workspace_daemon/`.
2. Resolve `../../scripts/memory_daemon_admin.py` relative to this `SKILL.md`
   and use its absolute path as `ADMIN`.
3. Read [routine-schema.md](references/routine-schema.md) before adding or
   editing a routine.

Do not infer a different checkout when the target is ambiguous.

## Inspect without exposing source details

List redacted summaries:

```sh
python3 "$ADMIN" routine list --repo "$DAEMON_REPO"
```

Inspect one redacted summary:

```sh
python3 "$ADMIN" routine inspect --repo "$DAEMON_REPO" --id ROUTINE_ID
```

Read the selected YAML locally when its exact fields are needed. Do not repeat
addresses, channel or space identifiers, absolute personal paths, credentials,
or complete routine content unless the user specifically needs to review that
value. Never put private routine content in public issues, commits, or PR text.

## Add or edit

1. Run `python3 daemon.py validate` in the checkout. Stop if the baseline is
   invalid.
2. For an add, copy `routines/_template.yaml` to a scratch directory outside
   the checkout. For an edit, copy the existing routine to that scratch
   directory so comments and formatting survive.
3. Modify only the scratch candidate. Keep the routine ID equal to the target
   ID. Use `source:` for one source and `sources:` for a domain-spanning
   routine.
4. Plan without mutating:

```sh
python3 "$ADMIN" routine plan \
  --repo "$DAEMON_REPO" \
  --operation add \
  --id ROUTINE_ID \
  --candidate "$CANDIDATE"
```

Use `--operation edit` for an existing routine.

5. Show the user the proposed diff and name the exact routine. Explain any
   Gmail actions separately because they mutate mail. Stop and wait for
   approval before applying.
6. Apply the unchanged plan with the emitted `plan-token`:

```sh
python3 "$ADMIN" routine apply \
  --repo "$DAEMON_REPO" \
  --operation add \
  --id ROUTINE_ID \
  --candidate "$CANDIDATE" \
  --token PLAN_TOKEN
```

The helper rejects stale plans, changes one file atomically, runs
`daemon.py validate`, and rolls back the file if validation fails.

## Remove

Plan removal first:

```sh
python3 "$ADMIN" routine plan \
  --repo "$DAEMON_REPO" \
  --operation remove \
  --id ROUTINE_ID
```

Show the deletion diff and explicitly state that only the routine YAML will be
removed. The processed ledger, schedule state, captured notes, and memory
entries remain. Wait for confirmation of the exact routine ID, then apply:

```sh
python3 "$ADMIN" routine apply \
  --repo "$DAEMON_REPO" \
  --operation remove \
  --id ROUTINE_ID \
  --token PLAN_TOKEN \
  --confirm-target ROUTINE_ID
```

Never delete history unless the user makes a separate, explicit request naming
the history target.

## Finish

Report the changed file and successful validation. Offer the exact
`python3 daemon.py run --routine ROUTINE_ID --dry-run` command as the next
step, but do not execute it without a separate request. Never perform a wet run
or enable scheduling implicitly.
