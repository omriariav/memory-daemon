"""Read a routine's extraction prompt from a personal-memory connector file.

A connector file in the store is two things at once: frontmatter describing the
source (id scheme, fetch hints) and a body that IS the extraction prompt — "what
is memory-worthy in Slack", "what to ignore in Gmail". The store resolves those
files in two layers, and the web UI edits the private one:

    <store>/memory/connectors/<name>.md   private override — wins when present
    <store>/connectors/<name>.md          git-tracked generic template

Pointing a routine at a connector instead of carrying an inline `instruction`
means the daemon and any interactive agent session apply the SAME judgment, and
tuning it is a browser edit rather than a config change + redeploy.

Resolution order for a routine's instruction (see `resolve_instruction`):

1. `analyze.instruction`               — inline; the routine IS a specialized job
2. `analyze.instruction_from_connector` — the source-level prompt from the store
3. `analyze.instruction_extra`          — appended to either, for stream-specific
                                          guidance ("in this channel, config
                                          flips are always worth keeping")

Deliberately NOT supported: per-routine prompt files inside the store. The store
has one prompt per source by design; routine-level specialization belongs in the
routine, which is what layers 1 and 3 are for.
"""
import os

from .shell import log

# Body text below this length is a stub, not a prompt — a connector whose body
# was emptied in the UI would otherwise silently degrade every capture to
# "summarize this", which reads as a model problem rather than a config one.
MIN_BODY_CHARS = 40


class PromptError(Exception):
    """The configured connector prompt could not be resolved."""


def connector_paths(store, name):
    """(override, template) candidate paths, in resolution order."""
    return (
        os.path.join(store, "memory", "connectors", f"{name}.md"),
        os.path.join(store, "connectors", f"{name}.md"),
    )


def _strip_frontmatter(raw):
    """Return the body of a `---`-delimited frontmatter document.

    Hand-rolled rather than pulled from a YAML lib: the frontmatter itself is
    the store's business (it validates it on load), and the daemon only needs
    the prose after it.
    """
    if not raw.startswith("---"):
        return raw.strip()
    end = raw.find("\n---", 3)
    if end == -1:
        return raw.strip()
    return raw[end + 4:].strip()


def read_connector_body(store, name):
    """The prompt body for one connector, override layer first.

    Returns (body, origin). Raises PromptError when neither layer exists or the
    resolved body is too short to be a real prompt — both are config mistakes
    that must fail loudly at routine start rather than quietly produce vague
    entries for hours.
    """
    override, template = connector_paths(store, name)
    for path, origin in ((override, "override"), (template, "template")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError as exc:
            raise PromptError(f"connector {name!r} at {path} is unreadable: {exc}") from exc
        body = _strip_frontmatter(raw)
        if len(body) < MIN_BODY_CHARS:
            raise PromptError(
                f"connector {name!r} ({origin}) has no usable prompt body "
                f"({len(body)} chars) — add extraction guidance to {path}"
            )
        return body, origin
    raise PromptError(
        f"connector {name!r} not found in the store at {store} "
        f"(looked for memory/connectors/{name}.md then connectors/{name}.md)"
    )


def resolve_instruction(routine):
    """The instruction text for a routine, resolving a connector reference.

    Pure lookup + concatenation; the caller passes the result to the LLM layer
    exactly as if it had been written inline.
    """
    analyze = routine.get("analyze") or {}
    inline = analyze.get("instruction")
    ref = analyze.get("instruction_from_connector")
    extra = (analyze.get("instruction_extra") or "").strip()

    if inline:
        base = inline.strip()
    elif ref:
        store = (routine.get("memory") or {}).get("store")
        if not store:
            raise PromptError(
                "analyze.instruction_from_connector needs memory.store — "
                "that is the personal-memory instance the connector lives in"
            )
        base, origin = read_connector_body(store, ref)
        log(f"routine={routine.get('id')} prompt from connector {ref!r} ({origin}, "
            f"{len(base)} chars)")
    else:  # config.validate rejects this; belt and braces for direct callers
        raise PromptError("routine has neither analyze.instruction nor "
                          "analyze.instruction_from_connector")

    return f"{base}\n\n{extra}" if extra else base


def validate(routine):
    """Config problems for the instruction block; empty list means valid."""
    rid = routine.get("id", "<missing id>")
    analyze = routine.get("analyze")
    if not isinstance(analyze, dict):
        return []  # the core validator already reports a missing analyze block
    inline = analyze.get("instruction")
    ref = analyze.get("instruction_from_connector")
    problems = []

    if inline and ref:
        problems.append(
            f"{rid}: set analyze.instruction OR analyze.instruction_from_connector, "
            f"not both — an inline instruction would shadow the connector prompt"
        )
    if not inline and not ref:
        problems.append(
            f"{rid}: analyze needs an `instruction` or an "
            f"`instruction_from_connector` (the connector body is the prompt)"
        )
    if ref:
        if not isinstance(ref, str) or "/" in ref or ref.startswith("."):
            problems.append(
                f"{rid}: analyze.instruction_from_connector must be a bare "
                f"connector name (got {ref!r})"
            )
        elif not (routine.get("memory") or {}).get("store"):
            problems.append(
                f"{rid}: analyze.instruction_from_connector requires memory.store "
                f"— the connector prompt is read from that store"
            )
        elif os.path.isdir(routine["memory"]["store"]):
            # Resolve at validate time so a typo fails on `daemon.py validate`
            # rather than mid-run, after the source has already been queried.
            # Only when the store actually exists: a template routine carrying a
            # placeholder path has a store problem, not a connector problem, and
            # reporting a missing connector there would be misleading.
            try:
                read_connector_body(routine["memory"]["store"], ref)
            except PromptError as exc:
                problems.append(f"{rid}: {exc}")
    if analyze.get("instruction_extra") is not None and not isinstance(
        analyze["instruction_extra"], str
    ):
        problems.append(f"{rid}: analyze.instruction_extra must be a string")
    return problems
