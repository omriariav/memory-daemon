"""`run --count-only`: list what a run would process, change nothing.

The runner lists and routes candidates against a read-only ledger and cursor
store, then hands the owned claims here instead of fetching or analysing
them. Output is counts and an estimate only; no item text is printed.
"""
from pathlib import Path

from . import gmail_mcp, slack_mcp
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
            default = mcp[0]["source"].get(
                "estimate_analyze_usd",
                slack_mcp.DEFAULTS["estimate_analyze_usd"],
            )
            sessions, cost = slack_mcp.plan_cost(
                len(mcp), usage, rid, default
            )
            saved = sum(
                1 for c in mcp if c["candidate"]["raw"].get("saved")
            )
            line += (
                f" ({len(mcp)} Slack thread(s), {saved} of them saved by "
                f"reaction, {sessions} planned thread "
                f"read session(s), estimated cost ${cost:.2f}; an estimate "
                "from past sessions, or estimate_analyze_usd per thread "
                "without history)"
            )
        mail = [c for c in claims if c["source"].get("kind") == "gmail_mcp"]
        if mail:
            source = mail[0]["source"]
            cfg = gmail_mcp.settings(source)
            reads = sum(
                len(claim["candidate"]["raw"]["fetch_ids"]) for claim in mail
            )
            sessions, cost = gmail_mcp.plan_cost(
                reads, len(mail), cfg["calls_per_session"], usage, rid,
                cfg["estimate_analyze_usd"],
            )
            line += (
                f" ({len(mail)} Gmail thread(s), {sessions} planned message "
                f"read session(s), estimated cost ${cost:.2f}; an estimate "
                "from past sessions, or estimate_analyze_usd per thread "
                "without history)"
            )
        log(line)
    log("count-only: no ledger, cursor or store was written")
    return totals
