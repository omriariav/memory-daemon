# Connector prompt layers

For connector name `<name>`, memory-daemon resolves:

1. `<store>/memory/connectors/<name>.md` — private override.
2. `<store>/connectors/<name>.md` — tracked generic template.

This management skill mutates only the private override. Removing it reveals
the generic template when one exists.

## Format

A prompt may be plain Markdown or a frontmatter document:

```markdown
---
name: example
---

Capture durable decisions, commitments, incidents, and facts.
Discard logistics, notifications, repetition, and social chatter.
```

The body is the extraction instruction. It must contain at least 40 characters
of usable guidance. Preserve existing frontmatter and formatting on edit.

Connector names use lowercase letters, digits, and hyphens only. Never place a
slash or relative path in a connector name.

## Source-wide versus specialized

Use a connector prompt for judgment that should apply to every general sweep of
one source: what is durable, what is noise, and what evidence deserves memory.

Keep the instruction inline in a routine when it performs a specialized job on
that source, expects a source-specific output structure, or covers a narrow
recurring report. `analyze.instruction_extra` may add routine-specific guidance
to a connector prompt without changing the source-wide base.

## Privacy

Private overrides may contain sensitive operating context. Keep them in the
memory store's private layer, never in the daemon repository or marketplace
package. Generic templates and examples must use fictional identifiers and
must not contain credentials, personal addresses, internal source IDs, or
organization-specific facts.
