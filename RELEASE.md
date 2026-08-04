# Releasing changes

How a change moves from a working tree to the live daemon. The procedure is
deliberately boring: every step below exists because skipping it once caused a
real problem.

## Deployment model: merging is deploying

There is no build, package, or version cut. launchd starts a **fresh
`daemon.py tick` process from this checkout every 15 minutes** (see
`launchd/com.memory-daemon.plist.template`), so whatever code the checkout has
at tick time is what runs in production. Two consequences:

- Pulling `main` in the live checkout deploys the change at the next tick.
  Nothing needs restarting for code-only changes — the coordinator process is
  not long-lived.
- **While the checkout sits on a feature branch, the daemon runs that branch's
  work-in-progress.** Either return the checkout to `main` after pushing a
  branch, or accept that unreviewed code handles the next ticks.

## 1. Develop on a branch

Branch off a fresh `main` (`git checkout -b fix/<slug>`). Never commit
directly to `main`: CI runs on pull requests, and the PR is where the review
gate lives.

Personal `routines/*.yaml` are gitignored. If a change alters the routine
schema or a source contract, the shipped `_template.yaml` / `_example-*.yaml`
files must be updated in the same PR — `tools/validate_examples.py` is the
only thing keeping documented examples honest.

## 2. Local gate (before opening a PR)

Run all of these; CI repeats the first four, but a red PR wastes a round trip:

```sh
python3 -m unittest discover -s tests
python3 -m pyflakes daemon.py workspace_daemon/ tests/ tools/ \
    plugins/memory-daemon-manager/scripts/
python3 tools/validate_examples.py
python3 daemon.py validate            # YOUR live routines vs the new schema
```

`daemon.py validate` is the one step CI cannot do for you: it checks the
personal, gitignored routine files on this machine against the changed code.
A schema change that passes CI can still break the live config.

For behavior changes, preview a real routine without side effects:

```sh
python3 daemon.py run --routine <id> --dry-run
```

Then run a code review pass (the `pr-reviewer` agent over `git diff`) and
apply or explicitly dismiss its findings **before** opening the PR. This gate
caught real bugs in past releases; it is not optional ceremony.

## 3. Pull request

- Open against `main`. CI must be green: pyflakes + full suite on Python 3.9
  and 3.12, a CLI smoke test, and example validation.
- The body states the root cause (for fixes) or the behavior change, what was
  tested and how, and — when the change came from a filed bug — how each
  acceptance criterion is covered.
- Note in the PR whether deploying needs anything beyond a pull (plist
  changes, routine config updates, state cutovers — see below).

## 4. Deploy

```sh
git checkout main && git pull
```

The next scheduled tick (≤15 minutes) runs the new code. Extra steps only
when the change touches more than code:

- **launchd plists or environment** (`launchd/*.template`, provider keys,
  Node link): re-render the plist per the README, then `./run.sh`. A loaded
  LaunchAgent keeps its old ProgramArguments and environment until it is
  re-bootstrapped; editing the plist on disk alone does nothing.
- **Personal routine config**: apply the matching edits to `routines/*.yaml`
  now and re-run `python3 daemon.py validate` — the next tick will load them.
- **Candidate-identity or catch-up changes**: if recurring candidate
  construction changed, bump `CATCH_UP_SCHEMA` (in
  `workspace_daemon/slack_source.py`) or set the routine's `catch_up_after`
  cutover in the same release, or the daemon will replay or skip history.
- **Immediate tick wanted**: `./run.sh` (validates, reloads both
  LaunchAgents, kickstarts one capture and one maintenance tick). Safe to run
  repeatedly.

## 5. Verify

```sh
./memory-daemon-status.sh     # ARMED/STATUS/ISSUES per routine, NOT WORTHY column
tail -f logs/run.log          # watch the first post-deploy tick end to end
```

A routine flagged `attention`, an unfinished last run, or a memory-sink
failure right after a deploy is the deploy's problem until proven otherwise.

## 6. Monitor and roll back

Behavioral changes (prompt edits, gates, classification) do not fail loudly —
they mis-capture quietly. Watch the next few daily digests and the status
output for the specific behavior the change targeted before considering it
done.

Rollback is `git revert` on `main` (via PR when time permits, directly when
the daemon is actively misbehaving) plus a pull in the live checkout; the
next tick runs the reverted code. Two caveats:

- `state/processed.json` entries written by the bad code remain. Delete the
  affected source-id entries to force reprocessing under the fixed code.
- Memory entries already written to the store are versioned in the store's
  own git; clean up there, not here.
