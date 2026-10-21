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
