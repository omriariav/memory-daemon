"""Declarative Gmail triage actions.

A routine's `actions:` list is applied in order. `apply_label` is skipped when the
LLM returned no label (or an unvalidated one).
"""
from . import gmail
from .shell import log

_HANDLERS = {
    "mark_read": gmail.mark_read,
    "mark_unread": gmail.mark_unread,
    "star": gmail.star,
    "unstar": gmail.unstar,
    "archive": gmail.archive,
}

# Also exported for config validation so the two can never drift.
VALID_ACTIONS = set(_HANDLERS) | {"apply_label"}


def describe(action, label):
    if action == "apply_label":
        return f"apply_label {label!r}" if label else "apply_label (skipped — no label)"
    return action


def apply(message_id, action_list, label):
    """Run the routine's actions against one message. Returns the applied action names."""
    applied = []
    for action in action_list:
        if action == "apply_label":
            if not label:
                log(f"message_id={message_id} apply_label skipped (no validated label)")
                continue
            gmail.apply_label(message_id, label)
        else:
            handler = _HANDLERS.get(action)
            if handler is None:
                log(f"message_id={message_id} unknown action '{action}' ignored")
                continue
            handler(message_id)
        applied.append(action)
    if applied:
        log(f"message_id={message_id} applied actions: {', '.join(applied)}")
    return applied
