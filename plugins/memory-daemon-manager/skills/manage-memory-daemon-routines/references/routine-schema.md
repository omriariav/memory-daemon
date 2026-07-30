# Routine schema

Use the checkout's `routines/_template.yaml` and `_example-*.yaml` files as the
canonical shapes. The validator is authoritative.

## Required structure

- `id`: lowercase letters, digits, and hyphens; match the filename for new
  routines.
- `enabled`: boolean.
- Exactly one of:
  - `source`: one Gmail, Drive, Slack, Google Chat, or Mila source.
  - `sources`: a non-empty list combining transports.
- `analyze.provider` and `analyze.model`.
- Exactly one prompt base:
  - `analyze.instruction` for a specialized job.
  - `analyze.instruction_from_connector` for general source-wide judgment.
- At least one sink:
  - `output` for vault notes.
  - `memory` for memory entries.

## Scheduling and ownership

- `schedule.every`: positive integer plus `m`, `h`, or `d`; default `4h`.
- `routing.fallback: true`: broad sweep that loses to every specific routine.
- `routing.priority`: integer; lower wins among routines in the same routing
  class. Equal-ranked matches are errors.

Keep a channel or space in one specific routine. A fallback may overlap because
the router assigns each candidate to one owner.

For `source.kind: mila`, keep Mila's storage read-only. Point
`recordings_file` at its absolute `recordings.json` path and use
`exclude_recording_ids` to baseline an existing library. `manual_recordings`
may name an explicit transcript plus the old Mila index that owns its UUID and
timestamps. Calendar matching is mandatory at runtime and only a
high-confidence match may proceed to memory. Outcome receipts belong under the
daemon's `state/transcriptions/`, never inside Mila's directory.

An `all_spaces: true` Google Chat fallback automatically excludes explicit
spaces configured by domain routines, including disabled routines. For frequent
fallback sweeps, `batch_messages: daily` gives all messages and replies in one
space/UTC-day a stable digest identity. The source re-fetches each discovered
UTC day completely before analysis, so replacement updates cannot lose earlier
content. Use `batch_unthreaded: daily` only when multi-message threads should
remain separate; the two batch modes are mutually exclusive.

When migrating an existing routine to `batch_messages`, set a quoted RFC3339
`batch_messages_after` boundary equal to the latest source timestamp covered by
the prior mode. The boundary is exclusive and prevents old thread/day memories
from being captured again under the new daily namespace. Keep the boundary and
legacy ledger rows after cutover.

For queue-style delivery on a broad GChat sweep, set `catch_up: true` and
`catch_up_overlap: 1h`. Catch-up currently requires `all_spaces: true`,
`batch_messages: daily`, `max_results: 0`, and `max_per_space: 0`. Its durable
last-successful-scan checkpoint makes the source window expand across sleep or
outages; the overlap is deduped by candidate version. Do not remove
`batch_messages_after`: it remains the bootstrap boundary if cursor state must
be rebuilt.

## Actions

Only Gmail supports `apply_label`, `mark_read`, `unstar`, and `archive`.

- A legacy single `source:` routine may use top-level `actions`.
- In a multi-source routine, put `actions` on the Gmail source block.
- Keep `actions: []` for a pilot until the user explicitly arms mutations.
- Treat `archive` as materially destructive to inbox state and call it out in
  the approval summary.

## Prompts and sinks

Use an inline instruction when the routine performs a specialized
transformation, such as extracting a recurring report or restructuring meeting
notes. Use a connector prompt for a broad sweep whose judgment should match
interactive use of that source.

Do not invent memory types. Use a type accepted by
`workspace_daemon/memory_sink.py`, and keep store and vault paths absolute.

## Safe editing

Copy an existing routine to a scratch file before editing so comments, quoting,
and key order survive. Never regenerate an existing routine through a generic
YAML serializer merely to change one field.
