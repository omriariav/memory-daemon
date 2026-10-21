"""Personal-content flagging and government-ID redaction for memory capture.

Everything here is deterministic (no model). Callers log counts and enum
reasons only, never matched values.
"""
import re

DEFAULT_PERSONAL_TAG = "personal"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
PERSONAL_REASONS = {
    "payroll", "compensation", "hr", "health", "family", "finance", "legal",
    "immigration", "id_number", "other",
}
ID_PLACEHOLDER = "[ID]"

DEFAULT_PERSONAL_KEYWORDS = [
    # English
    "payroll", "salary", "salaries", "payslip", "pay slip", "pension",
    "insurance policy", "medical", "sick leave", "divorce", "lawyer",
    "passport",
    # Hebrew
    "שכר", "משכורת", "תלוש", "פנסיה", "ביטוח", "קרן השתלמות", "מחלה",
    'ת"ז', "תעודת זהות",
]

_HEBREW_RE = re.compile(r"[֐-׿]")


def memory_cfg(routine):
    cfg = (routine or {}).get("memory")
    return cfg if isinstance(cfg, dict) else {}


def personal_tag(cfg):
    """The configured tag, or None when flagging is disabled."""
    if "personal_tag" not in cfg:
        return DEFAULT_PERSONAL_TAG
    value = cfg["personal_tag"]
    if value is None or value is False:
        return None
    return value if isinstance(value, str) and SLUG_RE.fullmatch(value) else None


def redact_enabled(cfg):
    return cfg.get("redact_ids", True) is not False


def keywords(cfg):
    value = cfg.get("personal_keywords")
    if value is None:
        return list(DEFAULT_PERSONAL_KEYWORDS)
    return [k for k in value if isinstance(k, str) and k.strip()]


def validate(cfg, rid):
    problems = []
    if "personal_tag" in cfg:
        value = cfg["personal_tag"]
        if not (
            value is None or value is False
            or (isinstance(value, str) and SLUG_RE.fullmatch(value))
        ):
            problems.append(
                f"{rid}: memory.personal_tag must be a kebab-case slug, "
                "null, or false"
            )
    if "redact_ids" in cfg and type(cfg["redact_ids"]) is not bool:
        problems.append(f"{rid}: memory.redact_ids must be a boolean")
    kws = cfg.get("personal_keywords")
    if kws is not None and (
        not isinstance(kws, list)
        or any(not isinstance(k, str) or not k.strip() for k in kws)
    ):
        problems.append(
            f"{rid}: memory.personal_keywords must be a list of non-empty strings"
        )
    return problems


# --- checksums ---------------------------------------------------------------

def israeli_id_valid(digits):
    """Israeli ID check digit (weights 1,2,1,2..., digit-sum, mod 10)."""
    if not digits.isdigit() or not 5 <= len(digits) <= 9:
        return False
    digits = digits.zfill(9)
    if set(digits) == {"0"}:
        return False
    total = 0
    for i, ch in enumerate(digits):
        n = int(ch) * (1 + i % 2)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def luhn_valid(digits):
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def iban_valid(value):
    value = value.replace(" ", "")
    if not 15 <= len(value) <= 34 or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", value):
        return False
    moved = value[4:] + value[:4]
    num = "".join(str(int(c, 36)) for c in moved)
    return int(num) % 97 == 1


# --- redaction ---------------------------------------------------------------

_LABEL = (
    r"(?:ת[\"״'׳]?\.?ז\.?|תעודת\s+זהות|\bI\.?D\.?(?=\W)|identity|passport"
    r"|\bSSN\b|social\s+security)"
)
_LABELLED_RE = re.compile(
    r"(?P<label>" + _LABEL + r"(?:\s*(?:no\.?|number|num\.?|#|card))?"
    r"\s*[:=#\-–]?\s*)"
    r"(?P<num>[A-Za-z]{0,2}\d[\d\-]{4,14}\d)(?!\d)(?![.,]\d)",
    re.IGNORECASE,
)
_IBAN_RE = re.compile(r"(?<![\w])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{3,4}){3,8}(?: ?[A-Z0-9]{1,2})?(?![\w])")
_CARD_RE = re.compile(r"(?<![\w.+,])\d(?:[ -]?\d){12,18}(?![\w])(?![.,]\d)")
_BARE_ID_RE = re.compile(r"(?<![\w.\-/:+,])\d{9}(?![\w]|[.,\-/:]\w)")


def redact(text):
    """Return (text with ID-like numbers replaced by [ID], replacement count)."""
    if not isinstance(text, str) or not text:
        return text, 0
    count = 0

    def counted(fn):
        def inner(match):
            nonlocal count
            out = fn(match)
            if out != match.group(0):
                count += 1
            return out
        return inner

    def labelled(match):
        if sum(c.isdigit() for c in match.group("num")) < 6:
            return match.group(0)
        return match.group("label") + ID_PLACEHOLDER

    def iban(match):
        return ID_PLACEHOLDER if iban_valid(match.group(0)) else match.group(0)

    def card(match):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            return ID_PLACEHOLDER
        return match.group(0)

    def bare(match):
        return ID_PLACEHOLDER if israeli_id_valid(match.group(0)) else match.group(0)

    for pattern, fn in (
        (_LABELLED_RE, labelled), (_IBAN_RE, iban),
        (_CARD_RE, card), (_BARE_ID_RE, bare),
    ):
        text = pattern.sub(counted(fn), text)
    return text, count


def has_id_numbers(text):
    return redact(text)[1] > 0


def keyword_hit(text, kws):
    """True when any configured keyword appears (case-insensitive)."""
    if not isinstance(text, str) or not text or not kws:
        return False
    folded = text.casefold()
    for kw in kws:
        needle = kw.casefold().strip()
        if _HEBREW_RE.search(needle):
            # Hebrew takes attached prefixes (ה/ב/ל...), so match substrings.
            if needle in folded:
                return True
        elif re.search(r"(?<!\w)" + re.escape(needle) + r"\w*", folded):
            return True
    return False


def deterministic_personal(cfg, *texts):
    """Model-free backstop: keyword or ID-number match in any text."""
    kws = keywords(cfg)
    return any(keyword_hit(t, kws) or has_id_numbers(t) for t in texts if t)
