# Routine schema

Use the checkout's `routines/_template.yaml` and `_example-*.yaml` files as the
canonical shapes. The validator is authoritative.

## Required structure

- `id`: lowercase letters, digits, and hyphens; match the filename for new
  routines.
- `enabled`: boolean.
- Exactly one of:
  - `source`: one Gmail, Drive, Slack, or Google Chat source.
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

An `all_spaces: true` Google Chat fallback automatically excludes explicit
spaces configured by domain routines, including disabled routines. For frequent
fallback sweeps, `batch_messages: daily` gives all messages and replies in one
space/UTC-day a stable digest identity. Use `batch_unthreaded: daily` only when
multi-message threads should remain separate; the two batch modes are mutually
exclusive.

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
