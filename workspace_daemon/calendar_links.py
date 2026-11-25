"""Source-id schemes that link a daemon entry to its calendar event.

The store CLI targets an entry by ANY source id it is given, and a same-type
match has its body replaced. The plain `gcal:<event id>` scheme belongs to
hand-logged entries, so a daemon write must never carry it. Each producer
uses its own scheme instead, and a reader looks for exactly the scheme it
means. This module only holds the names, so the Mila and MI sources can both
import them without importing each other.
"""

# An MI-only meeting entry: carries `mi:<meeting>:<session>` and this link.
# The Mila source treats a recording whose event carries this link as already
# captured, so the link must only ever name the event's own recording: the MI
# source writes it only for a session whose start is within the match window
# of the event start (never for a title-and-date fallback match).
MI_GCAL_SCHEME = "mi-gcal"
# The cross-check note beside a primary entry: a note-only link, so a second
# session of the same event can be recognised as already handled.
MI_CHECK_SCHEME = "mi-check"


def mi_gcal_id(event_id):
    return f"{MI_GCAL_SCHEME}:{event_id}"


def mi_check_id(event_id):
    return f"{MI_CHECK_SCHEME}:{event_id}"
