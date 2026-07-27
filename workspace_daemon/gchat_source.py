"""Google Chat source: sweep configured spaces via the `gws` CLI, grouped by thread.

Routine config:

    source:
      kind: gchat
      spaces:
        - spaces/AAAA000000A     # #my-team-space
      hours: 26                  # lookback window; overlap the schedule slightly
      max_results: 50            # cap per space

One `gws chat messages --after` call per space returns full message texts, so
candidates are grouped client-side by thread. Fetch resolves the space name,
conversation type, and members once per space per process. `gws chat members`
uses gws's persistent user cache for display names; this module adds a small
in-process cache so several threads from one space do not repeat API calls.

Identity has two layers (this is what keeps re-sweeps of an active thread from
freezing at first capture — the ledger dedupes on the *candidate* id, while the
memory store dedupes on the stable *source* id):

- candidate id:  gchat:<space>:<thread>@<latest_message_ts>  — changes when a
  thread gains replies, so the runner reprocesses it
- source id:     gchat:<space>:<thread>                      — stable anchor;
  `memory add` updates the same entry in place
"""
import datetime
import json
import subprocess

from .chat_text import redact_secrets, timestamped_line
from .shell import log

GWS = "gws"
MAX_CONTEXT_MEMBERS = 20
_member_cache = {}
_space_cache = {}


def _gws(args, timeout=60):
    r = subprocess.run([GWS, *args, "--format", "json"],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"gws {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:200]}")
    return json.loads(r.stdout)


def _after_iso(hours):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=float(hours))
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _space_id(space):
    return space.split("/")[-1]


def _thread_id(message):
    return (message.get("thread") or message.get("name", "")).split("/")[-1]


def candidates(source):
    """One candidate per thread that saw traffic inside the window.

    The candidate carries every windowed message of its thread in `raw`, so
    fetch never re-queries. Messages that arrived before the window (a thread's
    earlier history) are not included — the summary covers the active slice,
    and the stable source id folds successive slices into one memory entry.
    """
    after = _after_iso(source.get("hours", 26))
    per_space = int(source.get("max_results", 50))
    threads = {}

    for space in source.get("spaces", []):
        d = _gws(["chat", "messages", space, "--after", after, "--max", str(per_space)])
        for m in d.get("messages") or []:
            sid = f"gchat:{_space_id(space)}:{_thread_id(m)}"
            t = threads.setdefault(sid, {"space": space, "messages": []})
            t["messages"].append(m)

    out = []
    daily = {}
    for sid, t in threads.items():
        msgs = sorted(t["messages"], key=lambda m: m.get("create_time", ""))
        # Empty attachment/system messages cannot be analyzed. Dropping them
        # here also prevents titles and dry-run logs from leaking non-content.
        if not any((m.get("text") or "").strip() for m in msgs):
            continue

        if source.get("batch_unthreaded") == "daily" and len(msgs) == 1:
            day = (msgs[0].get("create_time") or "")[:10] or "date-unknown"
            key = (t["space"], day)
            daily.setdefault(key, []).extend(msgs)
            continue

        latest = msgs[-1].get("create_time", "")
        first_text = redact_secrets(
            (msgs[0].get("text") or "").replace("\n", " ")
        )
        out.append({
            "id": f"{sid}@{latest}",       # version-aware: new replies => new candidate
            "title": first_text[:90],
            "raw": {"source_id": sid, "space": t["space"], "messages": msgs},
        })

    # Google Chat assigns every unthreaded room message its own thread id. A
    # conversational burst would therefore cost one LLM call per sentence.
    # Opt-in daily batching gives those messages a stable space/day identity;
    # rerunning later the same day updates one memory entry as the digest grows.
    for (space, day), msgs in daily.items():
        msgs.sort(key=lambda m: m.get("create_time", ""))
        sid = f"gchat:{_space_id(space)}:day:{day}"
        latest = msgs[-1].get("create_time", "")
        first_text = next(
            (
                redact_secrets((m.get("text") or "").replace("\n", " "))
                for m in msgs
                if (m.get("text") or "").strip()
            ),
            "",
        )
        out.append({
            "id": f"{sid}@{latest}",
            "title": first_text[:90] or f"Google Chat digest for {day}",
            "raw": {
                "source_id": sid,
                "space": space,
                "messages": msgs,
                "digest_day": day,
            },
        })
    return out


def _member_context(space):
    """Return sender-name mapping plus the space's member names, cached per run."""
    if space in _member_cache:
        return _member_cache[space]
    names = {}
    members = []
    try:
        d = _gws(["chat", "members", space])
        rows = d if isinstance(d, list) else d.get("memberships") or d.get("members") or []
        for r in rows:
            user = r.get("user")
            display = r.get("display_name") or r.get("email") or user
            if user and display:
                names[user] = display
            if display:
                members.append(display)
    except Exception as exc:  # cosmetic — raw user ids still identify speakers
        log(f"gchat members lookup failed for {space} ({exc}); keeping raw ids")
    context = {
        "names": names,
        "members": sorted(set(members)),
    }
    _member_cache[space] = context
    return context


def _space_context(space):
    """Return live space name/type metadata, cached once per process."""
    if space in _space_cache:
        return _space_cache[space]
    context = {}
    try:
        d = _gws(["chat", "get-space", space])
        if isinstance(d, dict):
            context = {
                "display_name": d.get("display_name") or "",
                "type": d.get("type") or "",
            }
    except Exception as exc:  # enrichment only; messages remain processable
        log(f"gchat space lookup failed for {space} ({exc}); keeping space id")
    _space_cache[space] = context
    return context


def fetch(routine, candidate):
    """Render the thread's windowed messages as a readable conversation."""
    raw = candidate["raw"]
    msgs = raw["messages"]
    member_context = _member_context(raw["space"])
    space_context = _space_context(raw["space"])
    names = member_context["names"]
    members = member_context["members"]
    space_type = space_context.get("type", "")
    context_members = (
        members
        if space_type == "DIRECT_MESSAGE" or len(members) <= MAX_CONTEXT_MEMBERS
        else []
    )

    lines = [
        timestamped_line(
            m.get("create_time"),
            names.get(m.get("sender"), m.get("sender", "?")),
            m.get("text", ""),
        )
        for m in msgs
        if (m.get("text") or "").strip()
    ]
    if not lines:
        raise RuntimeError("thread has no text content in the window")

    date = (msgs[0].get("create_time") or "")[:10]
    return {
        "id": candidate["id"],
        "source_id": raw["source_id"],  # stable anchor for the memory store
        "title": candidate["title"] or f"chat thread in {raw['space']}",
        "date": date,
        "body": "\n".join(lines),
        "frontmatter": {
            "gchat_space": raw["space"],
            "gchat_space_display_name": space_context.get("display_name", ""),
            "gchat_space_type": space_type,
            "gchat_space_members": context_members,
            "gchat_space_member_count": len(members),
            "gchat_thread": raw["source_id"],
            "gchat_participants": sorted({names.get(m.get("sender"), m.get("sender", "?"))
                                          for m in msgs}),
            "message_count": len(msgs),
            "first_message_at": msgs[0].get("create_time", ""),
            "latest_message_at": msgs[-1].get("create_time", ""),
            "digest_day": raw.get("digest_day"),
        },
    }
