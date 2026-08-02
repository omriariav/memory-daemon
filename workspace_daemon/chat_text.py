"""Shared rendering and secret redaction for chat source material."""
import datetime
import re


_CODE_RE = re.compile(
    r"(?i)\b("
    r"(?:one[- ]time|verification|activation|security|login|account verification)"
    r"\s+code(?:\s+is|\s*:|\s*=)?\s*"
    r")(\d{4,12})\b"
)
_CREDENTIAL_RE = re.compile(
    r"(?i)\b("
    r"(?:api[-_ ]?(?:key|token)|access[-_ ]?token|auth(?:entication)?[-_ ]?token|"
    r"password|client[-_ ]?secret|user(?:name|[ -]name)|card(?:[ -](?:number|no\.?))|"
    r"pin|מספר\s+כרטיס|שם\s+משתמש|סיסמ[הת]?|קוד\s+(?:אישי|כניסה|הפעלה))"
    r"\s*(?:is|:|=)\s*"
    r")([^\s,;]+)"
)


def redact_secrets(text):
    """Remove obvious credentials before they reach logs, prompts, or memory.

    This is deliberately narrow: product metrics and ordinary identifiers must
    remain intact. The model prompt is still instructed to ignore credentials,
    but source-side redaction is the safety boundary.
    """
    value = str(text or "")
    value = _CODE_RE.sub(r"\1[REDACTED]", value)
    return _CREDENTIAL_RE.sub(r"\1[REDACTED]", value)


def slack_timestamp_iso(value):
    """Convert Slack's epoch timestamp to a stable UTC ISO-8601 value."""
    try:
        parsed = datetime.datetime.fromtimestamp(
            float(value), datetime.timezone.utc
        )
    except (TypeError, ValueError, OSError):
        return str(value or "")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamped_line(timestamp, speaker, text):
    stamp = str(timestamp or "time-unknown")
    return f"[{stamp}] {speaker}: {redact_secrets(text)}"
