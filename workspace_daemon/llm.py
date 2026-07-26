"""LLM analysis over the `yoetz` CLI, plus label extraction/validation."""
from .shell import run_json, yoetz_bin

DEFAULT_MAX_OUTPUT_TOKENS = 4096


def build_prompt(routine, headers, body, label_catalog):
    """Assemble the analysis prompt. Label catalog is only injected when asked for."""
    analyze = routine["analyze"]
    parts = ["You are triaging a business email for a product leader."]

    domains = analyze.get("focus_domains") or []
    if domains:
        parts.append(f"Focus domains: {', '.join(domains)}")

    parts.append(f"\nInstruction: {analyze['instruction']}")
    parts.append(
        "\n--- Email ---\n"
        f"From: {headers.get('from', '')}\n"
        f"Date: {headers.get('date', '')}\n"
        f"Subject: {headers.get('subject', '')}\n\n"
        f"{body}"
    )

    if analyze.get("pick_label"):
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
