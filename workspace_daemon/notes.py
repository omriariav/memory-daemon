"""Obsidian note rendering and writing."""
import datetime
import re
from pathlib import Path

import yaml

from .shell import utc_now_iso


def slugify(text):
    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text or "").strip().lower()
    return re.sub(r"[\s-]+", "-", text)


def email_date(headers):
    """RFC 2822 header date → YYYY-MM-DD, falling back to today."""
    raw = headers.get("date", "")
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z"):
        try:
            return datetime.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.datetime.strptime(raw[:31], "%a, %d %b %Y %H:%M:%S %z").date().isoformat()
    except ValueError:
        return datetime.date.today().isoformat()


def target_path(routine, headers, message_id):
    """Deterministic note path; suffixed with a short message id on collision."""
    prefix = routine["output"]["slug_prefix"]
    date = email_date(headers)
    output_dir = Path(routine["output"]["vault_dir"])
    path = output_dir / f"{prefix}-{date}.md"
    if path.exists():
        path = output_dir / f"{prefix}-{date}-{message_id[:8]}.md"
    return path


def render(routine, headers, message_id, thread_id, summary, label):
    frontmatter = {
        "kind": routine["output"].get("kind", "email-scoop-summary"),
        "rule_id": routine["id"],
        "source": routine["source"]["kind"],
        "gmail_message_id": message_id,
        "gmail_thread_id": thread_id,
        "gmail_link": f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
        "email_from": headers.get("from", ""),
        "email_subject": headers.get("subject", ""),
        "email_date": headers.get("date", ""),
        "focus_domains": routine["analyze"].get("focus_domains") or [],
        "gmail_label_applied": label,
        "generated_by": f"workspace-daemon (yoetz + {routine['analyze']['model']})",
        "generated_at": utc_now_iso(),
        "tags": routine["output"].get(
            "tags", ["kind/email-scoop-summary", "status/inbox"]
        ),
    }
    out = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True) + "---\n\n"
    out += f"# {headers.get('subject', '(no subject)')}\n\n"
    out += summary.strip() + "\n"
    return out


def write(routine, headers, message_id, thread_id, summary, label):
    path = target_path(routine, headers, message_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(routine, headers, message_id, thread_id, summary, label))
    return path
