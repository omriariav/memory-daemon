"""LLM analysis over the `yoetz` CLI, plus label extraction/validation."""
from .connector_prompts import resolve_instruction
from .shell import run_json, yoetz_bin

DEFAULT_MAX_OUTPUT_TOKENS = 4096


def _source_header_lines(item):
    """Render source metadata that materially changes how the model reads an item."""
    meta = item.get("frontmatter", {})
    lines = []

    if item.get("source_kind") == "gchat" or meta.get("gchat_space"):
        space = meta.get("gchat_space", "")
        display_name = meta.get("gchat_space_display_name", "")
        space_type = meta.get("gchat_space_type", "")
        members = meta.get("gchat_space_members") or []
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
            if space_type in {"DIRECT_MESSAGE", "GROUP_CHAT"} or len(members) <= 12:
                lines.append(f"Members: {', '.join(members)}")
            else:
                lines.append(f"Member count: {len(members)}")
        if participants:
            lines.append(f"Participants in supplied messages: {', '.join(participants)}")
        if meta.get("message_count") is not None:
            lines.append(f"Messages in supplied window: {meta['message_count']}")

    if meta.get("email_from"):
        lines.append(f"From: {meta['email_from']}")
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
    header_lines = _source_header_lines(item) + header_lines
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
