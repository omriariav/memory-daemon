"""Exact timestamp helpers shared by configuration and source runtimes."""
import datetime
import re
from fractions import Fraction


_RFC3339_INSTANT = re.compile(
    r"^(?P<second>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})$"
)


def rfc3339_key(value):
    """Return an exactly comparable RFC3339 instant.

    ``datetime`` stores only microseconds and silently truncates Google's valid
    nanosecond timestamps. Keep the complete fractional component as a rational
    number so exclusive boundaries remain exact.
    """
    match = _RFC3339_INSTANT.fullmatch(str(value))
    if not match:
        raise ValueError(f"invalid RFC3339 timestamp: {value!r}")
    zone = "+00:00" if match.group("zone") == "Z" else match.group("zone")
    second = datetime.datetime.fromisoformat(
        f"{match.group('second')}{zone}"
    ).astimezone(datetime.timezone.utc)
    digits = match.group("fraction") or ""
    fraction = (
        Fraction(int(digits), 10 ** len(digits))
        if digits else Fraction(0, 1)
    )
    return second, fraction


def is_rfc3339_instant(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        rfc3339_key(value)
    except (TypeError, ValueError):
        return False
    return True
