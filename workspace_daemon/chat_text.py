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
    r"(?im)\b("
    r"(?:api[-_ ]?(?:key|token)|access[-_ ]?token|auth(?:entication)?[-_ ]?token|"
    r"password(?:\s+(?:temporary|temp))?|client[-_ ]?secret|"
    r"user(?:name|[ -]name)|card(?:[ -](?:number|no\.?|#))|"
    r"pin(?:\s+(?:code|number))?|מספר\s+כרטיס|שם\s+משתמש|"
    r"סיסמ[הת]?(?:\s+זמנית)?|"
    r"קוד\s+(?:אישי|כניסה|הפעלה)(?:\s+זמני(?:ת)?)?)"
    r"\s*(?:is|:|=)\s*"
    r")([^\r\n]+)"
)
_TOKEN_RE = re.compile(
    r"(?i)\b(?:"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"xox[a-z]-[A-Za-z0-9-]{20,}|"
    r"AIza[A-Za-z0-9_-]{30,}|"
    r"bearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}"
    r")\b"
)


# --- short secret codes: a keyword next to a 3-8 digit run -------------------
# "קוד 4821", "the door code is 48 21", "PIN: 4-8-2-1", "4821 זה הקוד".
# Python's \b is not trusted around Hebrew letters here: keyword edges use
# explicit "no letter before / after" lookarounds ([^\W\d_] is any letter).
_NOT_AFTER_LETTER = r"(?<![^\W\d_])"
_NOT_BEFORE_LETTER = r"(?![^\W\d_])"
_HE_PREFIX = r"[ובלשהמכ]{0,3}"  # ו ב ל ש ה (and מ כ) prefixes, e.g. "שהקוד"
_HE_KEYWORD = (
    r"(?:קוד(?:\s+(?:ה?סודי|ה?כניסה|ה?אשראי|ה?אישי|ה?גישה|ה?דלת|ה?אזעקה"
    r"|ה?כספת|ה?בניין|ה?שער|ה?אימות|ה?הפעלה|זמני|חד[\s-]?פעמי))?"
    r"|סיסמ[האת](?:\s+(?:ה?כניסה|ה?אשראי|ה?בנק|זמנית))?"
    r"|ססמ[האת]"
    r"|פין(?:\s+קוד)?"
    r"|ספרות(?:\s+ה?אחוריות)?\s+[שה]?[במ]גב\s+ה?כרטיס"
    r"|ספרות\s+ה?אחוריות)"
)
_EN_KEYWORD = (
    r"(?:pass(?:word|code)|pin(?:\s+(?:code|number|no\.?))?"
    r"|cvv2?|cvc2?|otp|code)"
)
_KEYWORD = (
    rf"(?:{_NOT_AFTER_LETTER}(?:{_EN_KEYWORD}|{_HE_PREFIX}{_HE_KEYWORD})"
    rf"{_NOT_BEFORE_LETTER})"
)
# Words allowed between a keyword and its number ("הקוד שלי הוא", "code for
# the gate is"), at most four of them.
_FILLER_WORD = (
    r"(?:is|was|are|now|number|no\.?|for|to|of|the|my|our|your|door|gate"
    r"|building|entrance|lobby|alarm|safe|card|bank"
    r"|הוא|היא|זה|זו|זאת|עכשיו|של|שלי|שלך|שלו|שלה|שלנו|שלכם|שלהם"
    r"|ה?חדשה?"
    r"|[ובלשה]{0,2}(?:דלת|כניסה|בניין|בנין|שער|כספת|אזעקה|מחסן|חניה|חנייה"
    r"|בית|דירה|כרטיס|אשראי|בנק))"
)
_GAP_SEP = r"[\s:=\-–—\"'״׳“”‘’#(),]"
_GAP = (rf"(?:{_GAP_SEP}*{_NOT_AFTER_LETTER}{_FILLER_WORD}"
        rf"{_NOT_BEFORE_LETTER}){{0,4}}{_GAP_SEP}*")
# 3-8 digits, optionally split by single spaces or dashes ("48 21", "4-8-2-1"),
# never part of a longer number, a time, a date, a decimal or an amount.
_CURRENCY = (r"(?:₪|ש[\"״׳']?ח|שקל|nis\b|ils\b|usd\b|eur\b|dollars?\b|\$|€|£"
             r"|%|אחוז)")
_DIGITS = (
    r"(?P<code>(?<!\d)\d(?:[ -]?\d){2,7})"
    rf"(?![ -]?\d)(?!\s*[:/.,]\d)(?!\s*{_CURRENCY})"
)
_SHORT_CODE_RE = re.compile(rf"(?i)(?P<kw>{_KEYWORD}){_GAP}{_DIGITS}")
# The keyword after the number: "4821 is the code", "4821 זה הקוד".
_LINK = r"(?:is|was|=|זה|זו|זאת|הוא|היא)"
_TRAILING_CODE_RE = re.compile(
    r"(?i)(?P<code>(?<![\w$₪€£.:/-])\d(?:[ -]?\d){2,7})"
    rf"(?![ -]?\d)(?!\s*[:/.,]\d)(?!\s*{_CURRENCY})"
    rf"\s*[-–:,]?\s*(?:(?P<link>{_LINK}){_NOT_BEFORE_LETTER}\s*)?"
    r"(?:(?:the|my|your|our)\s+)?"
    r"(?:(?:door|gate|building|entry|entrance|alarm|safe|card|lobby)\s+)?"
    rf"(?P<kw>{_KEYWORD})"
)
# Codes that are fine to keep because they are not secrets. English
# qualifiers come before "code" ("zip code"), Hebrew ones after it ("קוד
# הנחה"). Coupon, discount and promo codes are kept on purpose: they are
# shared offers, not credentials.
_PUBLIC_CODE_BEFORE = re.compile(
    r"(?i)(?:^|[^\w])(?:area|dress|qr|zip|postal|post|promo|promotional"
    r"|discount|coupon|voucher|country|product|error|status|tracking|source"
    r"|morse|airport|iata|sku|item)[\s-]*$"
)
_PUBLIC_CODE_AFTER = re.compile(
    r"(?i)\s*(?:ה?(?:הנחה|קופון|קופונים|מוצר|מבצע|שובר|איתור|מעקב|אזור|לבוש"
    r"|מדינה|פריט|הזמנה|שגיאה|מקור)|qr)" + _NOT_BEFORE_LETTER
)
_DASH_DATE = re.compile(
    r"\d{4}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])"
    r"|(?:0?[1-9]|[12]\d|3[01])-(?:0?[1-9]|1[0-2])-(?:\d{2}|\d{4})"
)
_CLAUSE_END = re.compile(
    rf"(?i)\s*(?:$|[.,;!?)\n]|{_FILLER_WORD}{_NOT_BEFORE_LETTER})")


def _public_code(match):
    """True when the keyword names a non-secret code (zip, QR, coupon...)."""
    if match.group("kw").lower() == "code" and _PUBLIC_CODE_BEFORE.search(
            match.string[:match.start("kw")]):
        return True
    if _PUBLIC_CODE_AFTER.match(match.string, match.end("kw")):
        return True
    return bool(_DASH_DATE.fullmatch(match.group("code")))


def _without_code(match):
    return (match.string[match.start():match.start("code")] + "[REDACTED]"
            + match.string[match.end("code"):match.end()])


def _short_code(match):
    return match.group(0) if _public_code(match) else _without_code(match)


def _trailing_code(match):
    if _public_code(match):
        return match.group(0)
    # Without "is" / "זה" the keyword must close the clause, so "250 code
    # reviews" keeps its number while "4821 הקוד." does not.
    if not match.group("link") and not _CLAUSE_END.match(
            match.string, match.end()):
        return match.group(0)
    return _without_code(match)


# Full payment card numbers: 13-19 digits (single spaces or dashes allowed)
# that pass the Luhn check, wherever they appear.
_CARD_RE = re.compile(r"(?<!\d)(?<!\d[ -])\d(?:[ -]?\d){12,18}(?![ -]?\d)")


def _luhn_ok(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _card_number(match):
    digits = re.sub(r"\D", "", match.group(0))
    return "[REDACTED]" if _luhn_ok(digits) else match.group(0)


def redact_secrets(text):
    """Remove obvious credentials before they reach logs, prompts, or memory.

    This is deliberately narrow: product metrics and ordinary identifiers must
    remain intact. The model prompt is still instructed to ignore credentials,
    but source-side redaction is the safety boundary.
    """
    value = str(text or "")
    value = _CODE_RE.sub(r"\1[REDACTED]", value)
    value = _CREDENTIAL_RE.sub(r"\1[REDACTED]", value)
    value = _CARD_RE.sub(_card_number, value)
    value = _SHORT_CODE_RE.sub(_short_code, value)
    value = _TRAILING_CODE_RE.sub(_trailing_code, value)
    return _TOKEN_RE.sub("[REDACTED]", value)


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
