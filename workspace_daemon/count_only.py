"""`run --count-only`: list what a run would process, change nothing.

The runner lists and routes candidates against a read-only ledger and cursor
store, then hands the owned claims here instead of fetching or analysing
them. Output is counts and an estimate only; no item text is printed.
"""
from pathlib import Path

from . import slack_mcp
from .shell import log


def _is_new(processed, item_id):
    """New, changed, or a failed sink that the next run would retry."""
    entry = processed.get(item_id)
    return (
        entry is None
        or "memory_error" in entry
        or "expand_fallback" in entry
    )


def report(owned, processed, base_dir, totals):
    usage = Path(base_dir) / "state" / "claude-usage.jsonl"
    for rid in sorted(owned):
        claims = [
            claim for claim in owned[rid]
            if _is_new(processed, claim["candidate"]["id"])
        ]
        totals["matched"] += len(owned[rid])
        totals["processed"] += len(claims)
        totals["skipped"] += len(owned[rid]) - len(claims)
        mcp = [c for c in claims if c["source"].get("kind") == "slack_mcp"]
        line = f"count-only routine={rid}: {len(claims)} new or changed item(s)"
        if mcp:
            sessions, cost = slack_mcp.plan_cost(len(mcp), usage, rid)
            line += (
                f" ({len(mcp)} Slack thread(s), {sessions} planned thread "
                "read session(s), estimated cost "
                + ("unknown, no usage history" if cost is None else f"${cost:.2f}")
                + ")"
            )
        log(line)
    log("count-only: no ledger, cursor or store was written")
    return totals
