"""Detect that the machine slept while a bridge call was in flight.

On macOS `time.monotonic()` (mach_absolute_time) stops during sleep while the
wall clock keeps going, so a wall/monotonic gap that grows during a call means
the machine slept (or was frozen) in the middle of it. A call that failed
across such a gap failed because of the laptop, not because of the item, so
callers classify it as a shared-dependency outage and charge nothing.
"""

import time

SLEEP_GAP_SECONDS = 60
# Contains "machine slept", which runner._SHARED_DEPENDENCY_ERRORS matches.
OUTAGE_NOTE = "machine slept or went offline during the call"
# Plain connectivity failures, for callers that cannot import the runner.
_OFFLINE_MARKERS = (
    "timed out", "could not resolve host", "name or service not known",
    "network is unreachable", "connection refused", "connection reset",
    "nodename nor servname provided", "temporary failure in name resolution",
)
# Failures that are the service's answer, never the laptop's: a line that
# carries one stays a real error.
_NOT_OUTAGE_MARKERS = (
    "rate limit", "too many requests", "quota exceeded", "unauthenticated",
    "invalid api key", "auth failed",
)
# Pure network-down failures seen right after a wake. Timeouts and refused or
# reset connections are left out: they also happen on a healthy network.
_NETWORK_DOWN_MARKERS = (
    "could not resolve host", "name or service not known",
    "nodename nor servname provided", "temporary failure in name resolution",
    "network is unreachable",
)


class Span:
    """Wall and monotonic clocks captured at the start of one call."""

    def __init__(self, wall=time.time, mono=time.monotonic):
        self._wall, self._mono = wall, mono
        self.wall0, self.mono0 = wall(), mono()

    def slept_seconds(self):
        """Wall time that passed without monotonic time (0 when none)."""
        gap = (self._wall() - self.wall0) - (self._mono() - self.mono0)
        return gap if gap > 0 else 0.0

    def slept(self, threshold=SLEEP_GAP_SECONDS):
        return self.slept_seconds() > threshold


def is_outage(exc):
    """True for an exception the bridge classified as a sleep/offline outage."""
    return bool(getattr(exc, "outage", False))


def looks_offline(exc):
    text = str(exc).casefold()
    return is_outage(exc) or any(m in text for m in _OFFLINE_MARKERS)


def is_shared_dependency(exc):
    """True for a failure that every later call would share: an outage or an
    offline machine, or the service refusing us (authentication, rate limit,
    quota). A caller with many calls to make stops at the first one."""
    text = str(exc).casefold()
    return looks_offline(exc) or any(m in text for m in _NOT_OUTAGE_MARKERS)


def text_is_outage(text):
    """True when a log or error text names a measured sleep or a dead network.

    Rate-limit, quota and authentication failures are never an outage; nor
    are timeouts and refused or reset connections.
    """
    text = str(text).casefold()
    if any(m in text for m in _NOT_OUTAGE_MARKERS):
        return False
    return OUTAGE_NOTE in text or any(m in text for m in _NETWORK_DOWN_MARKERS)
