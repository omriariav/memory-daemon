"""LLM analysis over the `yoetz` CLI, plus label extraction/validation."""
from .connector_prompts import resolve_instruction
from .shell import run_json, yoetz_bin

DEFAULT_MAX_OUTPUT_TOKENS = 4096


def source_header_lines(item):
    """Render source metadata that materially changes how the model reads an item."""
    meta = item.get("frontmatter", {})
    lines = []

    if item.get("source_kind") == "gchat" or meta.get("gchat_space"):
        space = meta.get("gchat_space", "")
        display_name = meta.get("gchat_space_display_name", "")
        space_type = meta.get("gchat_space_type", "")
        members = meta.get("gchat_space_members") or []
        member_count = meta.get("gchat_space_member_count")
        participants = meta.get("gchat_participants") or []

        lines.append("Source: Google Chat")
        if display_name:
            lines.append(f"Space: {display_name}")
        elif space:
            lines.append(f"Space: {space}")
        if space_type:
            lines.append(f"Conversation type: {space_type}")

        # Membership identifies otherwise nameless DMs and group chats. For a
        # large named room, the roster costs tokens without adding useful
        # context, so provide only its size.
        if members:
            if (
                space_type == "DIRECT_MESSAGE"
                or (space_type == "GROUP_CHAT" and len(members) <= 20)
                or len(members) <= 12
            ):
                lines.append(f"Members: {', '.join(members)}")
            else:
                lines.append(f"Member count: {len(members)}")
        elif member_count:
            lines.append(f"Member count: {member_count}")
        if participants:
            lines.append(f"Participants in supplied messages: {', '.join(participants)}")
        if meta.get("message_count") is not None:
            lines.append(f"Messages in supplied window: {meta['message_count']}")
        if meta.get("reaction_count"):
            lines.append(
                f"Emoji reactions on supplied messages: {meta['reaction_count']} "
                "(annotated inline; a reaction is Chat's acknowledgement/"
                "feedback signal on the message it decorates)"
            )
        if meta.get("first_message_at"):
            lines.append(f"First supplied message: {meta['first_message_at']}")
        if meta.get("latest_message_at"):
            lines.append(f"Latest supplied message: {meta['latest_message_at']}")

    if item.get("source_kind") == "slack" or meta.get("slack_channel"):
        lines.append("Source: Slack")
        channel = meta.get("slack_channel_name") or meta.get("slack_channel")
        if channel:
            lines.append(f"Channel: {channel}")
        if meta.get("slack_capture_mode"):
            lines.append(f"Capture mode: {meta['slack_capture_mode']}")
        if meta.get("slack_summary_period"):
            lines.append(f"Supplied period: {meta['slack_summary_period']}")
        if meta.get("slack_participants"):
            lines.append(
                "Participants in supplied messages: "
                + ", ".join(meta["slack_participants"])
            )
        if meta.get("message_count") is not None:
            lines.append(f"Messages in supplied window: {meta['message_count']}")
        if meta.get("first_message_at"):
            lines.append(f"First supplied message: {meta['first_message_at']}")
        if meta.get("latest_message_at"):
            lines.append(f"Latest supplied message: {meta['latest_message_at']}")
        if meta.get("message_limit_reached"):
            lines.append(
                "Coverage warning: the upstream Slack summary reached its message cap."
            )

    if meta.get("email_from"):
        lines.append(f"From: {meta['email_from']}")
    if meta.get("email_to"):
        lines.append(f"To: {meta['email_to']}")
    if meta.get("email_cc"):
        lines.append(f"Cc: {meta['email_cc']}")
    if meta.get("gmail_labels"):
        lines.append(f"Gmail labels: {', '.join(meta['gmail_labels'])}")
    if meta.get("gmail_chat_followup_managed"):
        if meta.get("gmail_chat_followup_active"):
            lines.append(
                "Attention signal: the memory owner manually forwarded this "
                "Chat message to their own Inbox and it remains open."
            )
        else:
            lines.append(
                "Attention signal: this manually forwarded Chat message is no "
                "longer in the memory owner's Inbox."
            )
    if meta.get("gmail_thread_message_count") is not None:
        included = meta.get(
            "gmail_thread_messages_included",
            meta["gmail_thread_message_count"],
        )
        lines.append(
            "Messages in supplied thread: "
            f"{included} of {meta['gmail_thread_message_count']}"
        )
        if meta.get("gmail_thread_truncated"):
            lines.append(
                "Coverage warning: older thread messages were omitted by the "
                "configured safety limit."
            )
    related = meta.get("related_memory_entries") or []
    if related:
        lines.append(
            "Durable memories already captured from this conversation:"
        )
        for entry in related:
            lines.append(
                f"- {entry.get('id')} ({entry.get('type')}, "
                f"{entry.get('date')}): {entry.get('title')}"
            )
        lines.append(
            'A terse acknowledgement in this source (e.g. "done", '
            '"approved", "looks good", or an emoji reaction) that resolves '
            "or confirms one of these captured items IS durable: summarize "
            "it as the resolution or state change of that item, naming the "
            "item, instead of dismissing the message as non-durable. "
            "Reactions are aggregate counts without reactor identity — "
            "treat a reaction alone as a weaker signal than an explicit "
            "reply, and never attribute a reaction to a specific person."
        )

    if item.get("source_kind") == "mila" or meta.get("mila_recording_id"):
        lines.append("Source: Mila transcription")
        if meta.get("mila_recording_start"):
            lines.append(f"Recording started: {meta['mila_recording_start']}")
        if meta.get("mila_recording_end"):
            lines.append(f"Recording ended: {meta['mila_recording_end']}")
        if meta.get("calendar_event_title"):
            lines.append(
                f"Matched Calendar event: {meta['calendar_event_title']}"
            )
        if meta.get("calendar_event_start"):
            lines.append(
                f"Calendar event start: {meta['calendar_event_start']}"
            )
        if meta.get("calendar_match_confidence"):
            lines.append(
                f"Calendar match confidence: "
                f"{meta['calendar_match_confidence']}"
            )
    return lines


def build_prompt(routine, item, label_catalog):
    """Assemble the analysis prompt. Label catalog is only injected when asked for."""
    analyze = routine["analyze"]
    parts = ["You are triaging source material for a product leader."]

    domains = analyze.get("focus_domains") or []
    if domains:
        parts.append(f"Focus domains: {', '.join(domains)}")

    parts.append(f"\nInstruction: {resolve_instruction(routine)}")

    header_lines = [f"Title: {item.get('title', '')}", f"Date: {item.get('date', '')}"]
    header_lines = source_header_lines(item) + header_lines
    parts.append(
        "\n--- Source ---\n" + "\n".join(header_lines) + f"\n\n{item['body']}"
    )

    if analyze.get("pick_label") and label_catalog:
        labels_block = "\n".join(f"- {name}" for name in label_catalog)
        parts.append(
            "\n--- Labeling task ---\n"
            "Here is the full catalog of existing Gmail labels in this mailbox:\n"
            f"{labels_block}\n\n"
            "After the summary above, on its own final line, output exactly one line "
            "in the form `LABEL: <name>` where <name> is copied verbatim (exact spelling "
            "and casing) from the catalog above — the single best-fitting existing label "
            "for this email. If none fit reasonably well, output `LABEL: NONE`. Do not "
            "invent a new label name. Do not add markdown formatting to that line."
        )

    return "\n".join(parts) + "\n"


def analyze(routine, prompt):
    """Call yoetz and return the raw generated content."""
    cfg = routine["analyze"]
    cmd = [
        yoetz_bin(), "ask",
        "-p", prompt,
        "--provider", cfg["provider"],
        "--model", cfg["model"],
        "--max-output-tokens", str(cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)),
        "--format", "json",
    ]
    result = run_json(cmd, timeout=300)
    return result["content"]


def split_label(content, valid_labels):
    """Strip the trailing `LABEL: <name>` line and resolve it against the real catalog.

    Returns (summary_text, label_or_None). An LLM-invented name resolves to None —
    an unvalidated name is never handed to Gmail.
    """
    valid_by_lower = {name.lower(): name for name in valid_labels}
    lines = content.strip().splitlines()
    chosen = None
    body_lines = lines
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.upper().startswith("LABEL:"):
            candidate = stripped.split(":", 1)[1].strip().strip("`*_")
            if candidate.upper() != "NONE":
                chosen = valid_by_lower.get(candidate.lower())
            body_lines = lines[:i] + lines[i + 1:]
            break
    return "\n".join(body_lines).strip(), chosen
