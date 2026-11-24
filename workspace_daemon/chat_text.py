"""Shared rendering and secret redaction for chat source material."""
import datetime
import re
import unicodedata

# Markers around the earlier channel messages shown beside a saved message.
CONTEXT_START = (
    "[Preceding channel context: earlier top-level messages from the same "
    "channel, shown only so the saved message can be understood. They are "
    "NOT the saved message; the memory owner saved only the message after "
    "this block. Treat them as background: people, requests, decisions and "
    "commitments belong to the saved message only.]"
)
CONTEXT_END = "[End of preceding context. The saved message follows.]"
_CONTEXT_MARKER_RE = re.compile(
    r"\[(?:Preceding channel context|End of preceding context)[^\]]*\]?",
    re.I,
)


def strip_context_markers(text):
    """`text` without anything that looks like a context marker."""
    return _CONTEXT_MARKER_RE.sub("", str(text or ""))


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
    r"סיסמ[האת]?(?:\s+זמנית)?|"
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


# --- short secret codes and card numbers --------------------------------------
# "קוד 4821", "the door code is 48 21", "PIN: 4-8-2-1", "4821 זה הקוד" and full
# card numbers. These passes match on a shadow copy of the text without
# invisible format characters (RLM, ZWJ, BOM...) and Hebrew niqqud, with
# every digit as ASCII; the shadow keeps a map back to the original, where the
# redaction is applied. Python's \b is not trusted around Hebrew letters:
# keyword edges use explicit "no letter before / after" lookarounds.
_LETTER_BEFORE = r"(?<![^\W\d_])"
_LETTER_AFTER = r"(?![^\W\d_])"
_HE_PREFIX = r"[ובלשהמכ]{0,3}"  # ו ב ל ש ה (and מ כ) prefixes, e.g. "שהקוד"
_HE_KEYWORD = (
    r"(?:קוד(?:\s+(?:ה?(?:סודי|כניסה|אשראי|אישי|גישה|דלת|אזעקה|כספת|בניין"
    r"|שער|אימות|הפעלה|אינטרקום|כספומט|שובר|מימוש|מתנה|חד[\s-]?פעמי)|זמני))?"
    r"|סיסמ[האת](?:\s+(?:ה?(?:כניסה|אשראי|בנק)|זמנית))?"
    r"|ססמ[האת]"
    r"|פין(?:\s+קוד)?"
    r"|מספר\s+ה?סודי"
    r"|ספרות(?:\s+ה?אחוריות)?\s+[שה]?[במ]גב(?:\s+ה?כרטיס)?"
    r"|ספרות\s+ה?אחוריות"
    r"|סי\.?\s?וי\.?\s?וי"
    r"|אינטרקום"
    r"|(?:דלת|שער)(?=\s*:))"  # a bare label only before a colon
)
_COMBO_TARGET = r"(?:safe|lock|locker|padlock|lockbox|כספת|מנעול)"
_EN_KEYWORD = (
    r"(?:pass(?:word|code)|pwd?(?=\s*(?:[:=]|is\b))"
    r"|pin(?:\s*(?:code|number|no\.?))?|cvv2?|cvc2?|otp|code"
    rf"|{_COMBO_TARGET}\s+(?:combo|combination)"
    r"|(?:combo|combination)(?=\s*(?:[:=]|is\b)"
    rf"|\s+(?:for|to|of)\s+(?:the\s+|my\s+)?{_COMBO_TARGET})"
    r"|keypad|intercom|lock\s?box"
    r"|wi-?fi\s+pass|digits\s+on\s+the\s+back(?:\s+of\s+the\s+card)?"
    r"|(?:door|gate)(?=\s*:))"
)
_KEYWORD = (
    rf"(?:{_LETTER_BEFORE}(?:(?:[ובלשה]{{1,2}}-?)?{_EN_KEYWORD}"
    rf"|{_HE_PREFIX}{_HE_KEYWORD}){_LETTER_AFTER})"
)
# Words allowed between a keyword and its number ("הקוד שלי הוא", "code for
# the gate is"), at most five of them.
_FILLER_WORD = (
    r"(?:is|was|are|now|number|no\.?|for|to|of|on|the|my|our|your|new|door"
    r"|gate|building|entrance|lobby|alarm|safe|card|bank|wi-?fi|site|website"
    r"|account|venue|office|house|home|apartment|flat|garage|front|main|this"
    r"|today|הוא|היא|זה|זו|זאת|עכשיו|של|שלי|שלך|שלו|שלה|שלנו|שלכם|שלהם"
    r"|ה?חדשה?|ה?חד[\s-]?פעמי|ה?סודי"
    r"|[ובלשה]{0,2}(?:דלת|כניסה|בניין|בנין|שער|כספת|אזעקה|מחסן|חניה|חנייה"
    r"|בית|דירה|משרד|מוסך|כרטיס|אשראי|בנק|ויפי|וויפי|אתר|אינטרקום|כספומט"
    r"|wi-?fi))"
)
# Punctuation, chat markup (*bold*, _italic_, ~strike~, `mono`), quotes,
# brackets, arrows and emoji may sit between the keyword and the number.
_GAP_CHAR = (r"[\s:=\-–—\"'״׳“”‘’„#(),*_~`«»\[\]<>→⇒➡➜|"
             "☀-➿⬀-⯿\U0001f300-\U0001faff]")
_GAP = (rf"(?:{_GAP_CHAR}{{0,8}}{_LETTER_BEFORE}{_FILLER_WORD}{_LETTER_AFTER})"
        rf"{{0,5}}{_GAP_CHAR}{{0,8}}")
_KEYWORD_RE = re.compile(rf"(?i)(?P<kw>{_KEYWORD})(?P<gap>{_GAP})(?=\d)")
# A bare "code" / "קוד" is a common word in engineering chatter ("deployed
# the code to 1200 hosts"), so only a narrow set of words may sit between it
# and the number: is / the / my..., and places ("code for the gate is").
_PLACE = (
    r"(?:door|gate|building|entrance|lobby|alarm|safe|bank|card|wi-?fi|venue"
    r"|office|house|home|apartment|flat|garage|front|main|intercom|keypad"
    r"|[ובלשה]{0,2}(?:דלת|כניסה|בניין|בנין|שער|כספת|אזעקה|מחסן|חניה|חנייה"
    r"|בית|דירה|משרד|מוסך|כרטיס|אשראי|ויפי|וויפי|אינטרקום|כספומט|wi-?fi))"
)
_BARE_FILLER = (
    r"(?:is|was|the|my|your|our|הוא|היא|זה|זו|זאת|של|שלי|שלך|שלו|שלה|שלנו"
    rf"|שלכם|שלהם|ה?חדשה?|ה?סודי|{_PLACE}"
    rf"|(?:for|to|of)\s+(?:(?:the|my|our|your)\s+)?{_PLACE})"
)
_BARE_GAP = re.compile(
    rf"(?i)(?:{_GAP_CHAR}*{_LETTER_BEFORE}{_BARE_FILLER}{_LETTER_AFTER})*"
    rf"{_GAP_CHAR}*")
# Labels that are also ordinary words need 4+ digits and no unit after them
# ("intercom 26" is a flat, "Gate: 482" a boarding gate, "דלת: 1200 מטר").
_LABEL_KEYWORD = re.compile(
    r"(?i)(?:[ובלשה]{0,2}-?)?(?:intercom|keypad|door|gate|אינטרקום|דלת|שער)")
_UNIT_AFTER = re.compile(
    r"(?i)\s*(?:מטר|מ'|ק\"מ|ס\"מ|ס״מ|ק״מ|m|km|cm|ft|feet|meters?|metres?"
    rf"|minutes?|min|דקות|דק'){_LETTER_AFTER}")
_CURRENCY_AFTER = re.compile(
    r"(?i)\s*(?:₪|ש[\"״׳']?ח|שקל|\$|€|£|%|אחוז"
    rf"|(?:nis|ils|usd|eur|dollars?){_LETTER_AFTER})")
_DIGIT_RUN = re.compile(r"\d+(?:(?: {1,2}|[-./:,])\d+)*")
_RUN_SEP = re.compile(r"( {1,2}|[-./:,])")
_DASH_DATE = re.compile(
    r"\d{4}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])"
    r"|(?:0?[1-9]|[12]\d|3[01])-(?:0?[1-9]|1[0-2])-(?:\d{2}|\d{4})"
)
# Codes that are fine to keep because they are not secrets. English
# qualifiers come before "code" ("zip code"), Hebrew ones after "קוד" ("קוד
# הנחה"). Coupon, discount and promo codes are kept on purpose (shared
# offers); gift card, voucher and redemption codes are bearer values and are
# redacted, as are confirmation, verification, access and entry codes.
_PUBLIC_CODE_BEFORE = re.compile(
    rf"(?i){_LETTER_BEFORE}(?:area|dress|qr|zip|postal|post|promo|promotional"
    r"|discount|coupon|country|product|error|status|tracking|source|morse"
    r"|airport|iata|sku|item|reference|ref|booking|order|reservation|invoice"
    r"|ticket|customer|client|member|employee|course|class|room|flight"
    r"|transaction|case|claim|exit|response|return|returned|iso|dialing"
    r"|dialling|http|unicode|bank|branch|swift|sort|lines\s+of)[\s-]*$"
)
_PUBLIC_CODE_AFTER = re.compile(
    r"(?i)\s*(?:ה?(?:הנחה|קופון|קופונים|מוצר|מבצע|איתור|מעקב|אזור|לבוש|מדינה"
    r"|פריט|הזמנה|שגיאה|מקור|פתוח|מוסד|סמל|מקצוע|קורס|כיתה|חדר|לקוח|עובד|חבר"
    r"|אסמכתא|טיסה|עסקה|תיק|תביעה|יציאה|חיוג|חשבונית|כרטיסיה|בנק|סניף|דואר"
    rf"|סטטוס)|qr){_LETTER_AFTER}"
)
_BARE_CODE = re.compile(r"(?i)code|[ובלשמכ]{0,2}קוד")
_BARE_KEYWORD = re.compile(r"(?i)(?:[ובלשה]{0,2}-?)?code|[ובלשהמכ]{0,3}קוד")
_PUBLIC_WINDOW = 40  # characters before a keyword checked for a qualifier
_STATUS_AFTER = re.compile(r"(?i)\s+from\s+(?:the\s+)?(?:server|api|backend)")


def _shadow(text):
    """(text for matching, map from its positions to the original's)."""
    if text.isascii():
        return text, None
    chars, index = [], []
    for position, char in enumerate(text):
        category = unicodedata.category(char)
        if category in ("Cf", "Mn"):
            continue  # bidi marks, joiners, BOM, niqqud, variation selectors
        if category == "Nd" and not "0" <= char <= "9":
            char = str(unicodedata.digit(char, 0))
        chars.append(char)
        index.append(position)
    return "".join(chars), index


def _public_code(text, start, end):
    """True when the keyword text[start:end] names a non-secret code."""
    if _BARE_CODE.fullmatch(text, start, end) and text[start] in "cC" \
            and _PUBLIC_CODE_BEFORE.search(
                text, max(0, start - _PUBLIC_WINDOW), start):
        return True
    return bool(_PUBLIC_CODE_AFTER.match(text, end))


def _code_end(text, start):
    """End of the 3-8 digit secret code starting at text[start], or None.

    A split code is joined only when its groups look like one code: single
    digits ("4 8 2 1", redacted whole even past 8), groups as long as the
    first ("48 21", "482 113"), or dash-joined groups of 2-4 digits
    ("48-213"). A group of another shape ("PIN 4821 300 people", "code 4821
    12 November") ends the code. A time, date, decimal or amount in the code
    itself cancels it; one after the code only ends it.
    """
    run = _DIGIT_RUN.match(text, start)
    parts = _RUN_SEP.split(run.group(0))
    groups, seps = parts[0::2], parts[1::2]
    if (len(groups) >= 3 and set(seps) == {"."}
            and all(len(group) == 1 for group in groups)):
        length = len(run.group(0))  # 4.8.2.1
    else:
        compound = [False] * len(groups)  # part of 17:30, 12/27, 3.12, 1,500
        for index, sep in enumerate(seps):
            if sep in "./:,":
                compound[index] = compound[index + 1] = True
        if compound[0] or len(groups[0]) > 8:
            return None
        first = len(groups[0])
        digits, length = first, first
        steps = [(digits, length)]
        for index in range(1, len(groups)):
            sep, group = seps[index - 1], groups[index]
            if compound[index] or sep not in (" ", "  ", "-") or (
                    index > 1 and sep != seps[0]):
                break
            if first == 1:
                fits = len(group) == 1  # single digits: no 8-digit cap
            elif sep == "-":
                fits = 2 <= len(group) <= 4 and digits + len(group) <= 8
                if not fits:
                    return None  # a phone number or a long dashed id
            else:
                fits = len(group) == first and digits + len(group) <= 8
            if not fits:
                break
            digits += len(group)
            length += len(sep) + len(group)
            steps.append((digits, length))
        # "code 4821 1500 NIS": the amount ends the code, it does not join it
        while len(steps) > 1 and _CURRENCY_AFTER.match(text, start + length):
            steps.pop()
            digits, length = steps[-1]
        if digits < 3:
            return None
        if _DASH_DATE.fullmatch(text, start, start + length):
            return None
    if _CURRENCY_AFTER.match(text, start + length):
        return None
    return start + length


def _keyword_spans(text):
    for match in _KEYWORD_RE.finditer(text):
        keyword = match.group("kw")
        if _public_code(text, match.start("kw"), match.end("kw")):
            continue
        if _BARE_KEYWORD.fullmatch(keyword) and not _BARE_GAP.fullmatch(
                match.group("gap")):
            continue
        end = _code_end(text, match.end())
        if end is None or _STATUS_AFTER.match(text, end):
            continue
        if _LABEL_KEYWORD.fullmatch(keyword) and (
                sum(char.isdigit() for char in text[match.end():end]) < 4
                or _UNIT_AFTER.match(text, end)):
            continue
        yield match.end(), end


# The keyword after the number: "4821 is the code", "4821 זה הקוד",
# "482113 is your Instagram code", "4821 - הסיסמה".
_LINK = r"(?:is|was|=|זה|זו|זאת|הוא|היא)"
_TRAILING_RE = re.compile(
    r"(?i)(?:(?<=\bG-)|(?<![\w$₪€£.:/\-,]))"
    r"(?P<code>\d(?:[ -]?\d){2,7})(?![ -]?\d)(?!\s*[:/.,]\d)"
    r"[\s\-–—,*_~`\"'»«)\]]*"
    rf"(?:(?P<link>{_LINK}){_LETTER_AFTER}\s*)?"
    r"(?:your\s+(?:[^\W\d_]+\s+){0,2}?|(?:the|my|our)\s+)?"
    r"(?P<modifier>(?:door|gate|building|entry|entrance|alarm|safe|card|lobby"
    r"|verification|access|security|one-time|login)\s+)?"
    rf"(?P<kw>{_KEYWORD})"
)
_CLAUSE_END = re.compile(
    r"(?i)\s*(?:$|[.,;:!?)\n☀-➿\U0001f300-\U0001faff]"
    rf"|{_FILLER_WORD}{_LETTER_AFTER})")
_USE_TO_RE = re.compile(
    rf"(?i){_LETTER_BEFORE}use\s+(?!(?:19|20)\d\d\s)(?P<code>\d{{4,8}})\s+"
    r"(?:to|for)\s+(?:log\s*in|login|sign\s*in|verify|confirm|reset)")
_G_CODE_RE = re.compile(
    r"(?i)(?<![\w-])G-(?P<code>\d{6})(?!\d)"
    r"(?=[^.!?\n]{0,60}?(?:code|verification|קוד))")


def _trailing_spans(text):
    for match in _TRAILING_RE.finditer(text):
        if _CURRENCY_AFTER.match(text, match.end("code")):
            continue
        if _public_code(text, match.start("kw"), match.end("kw")):
            continue
        # A bare "code" / "קוד" after a number needs "is" / "זה" or a door,
        # gate... word: "apartment 214 code" and "In 2026 the code" keep
        # their numbers, "4821 is the code" and "4821 הקוד." do not.
        bare = _BARE_CODE.fullmatch(match.group("kw"))
        if bare and not match.group("link") and not match.group("modifier"):
            continue
        if not _CLAUSE_END.match(text, match.end()):
            continue
        yield match.span("code")
    for pattern in (_USE_TO_RE, _G_CODE_RE):
        for match in pattern.finditer(text):
            yield match.span("code")


# Full payment card numbers: one unbroken run, or 4-4-4-4(-3) / 4-6-5(4)
# groups split the same way throughout, with a brand prefix and length that
# fit and a valid Luhn check digit. Never inside a URL, a path or a longer
# token, so permalinks, status ids and order numbers keep their digits. An
# expiry date or CVV right after the card goes with it.
_CARD_RE = re.compile(
    r"(?<![\w/#=:.\-])"
    r"(?:[2-6]\d{3}(?P<sep> {1,2}|[.\-])\d{4}(?P=sep)\d{4}(?P=sep)\d{4}"
    r"(?:(?P=sep)\d{3})?"
    r"|3\d{3}(?P<amex> {1,2}|[.\-])\d{6}(?P=amex)\d{4,5}"
    r"|[2-6]\d{12,18})"
    r"(?!\w|/(?!\d{2,4}(?![\w/])))"
)
_TAIL_SEP = r"[\s,;|/\-]{0,3}"
_EXPIRY_RE = re.compile(
    rf"(?i){_TAIL_SEP}(?P<label>(?:exp(?:iry|ires|\.)?|valid(?:\s+thru)?"
    r"|תוקף|ת\.ת)[\s:.\-]{0,3})?"
    r"(?:(?P<slash>(?:0[1-9]|1[0-2])\s*/\s*(?:\d{4}|\d{2}))"
    r"|(?:0[1-9]|1[0-2])\d{2})(?![\w/])")
_CVV_LABELED_RE = re.compile(
    rf"(?i){_TAIL_SEP}(?:cvv2?|cvc2?|cvn|קוד|ספרות\s+בגב)[\s:.\-]{{0,3}}"
    r"\d{3,4}(?![\w/])")
_CVV_BARE_RE = re.compile(r"[\s,;|\-]{0,3}\d{3,4}(?![\w/])")
_CVV_SLASH_RE = re.compile(r"/\d{3,4}(?![\w/])")
_WORD_OR_AMOUNT_AFTER = re.compile(
    r"(?i)\s*(?:[^\W\d_]|₪|\$|€|£|%)")


def _card_tail_end(text, end):
    """End of an expiry date and CVV right after a card number.

    A bare 3-4 digit number counts only right after an expiry date or
    glued on with "/" (and never before a word or an amount: "1500 NIS
    total", "1234 Main st"); otherwise it needs a label (cvv, קוד, exp).
    """
    def plain(match):
        return match and not _WORD_OR_AMOUNT_AFTER.match(text, match.end())

    expiry = _EXPIRY_RE.match(text, end)
    if expiry and (expiry.group("label") or expiry.group("slash")
                   or plain(expiry)):
        end = expiry.end()
    else:
        expiry = None
    cvv = _CVV_LABELED_RE.match(text, end)
    if not cvv:
        bare = (_CVV_BARE_RE if expiry else _CVV_SLASH_RE).match(text, end)
        cvv = bare if plain(bare) else None
    if cvv:
        end = cvv.end()
        if not expiry:
            expiry = _EXPIRY_RE.match(text, end)
            if expiry and (expiry.group("label") or expiry.group("slash")):
                end = expiry.end()
    return end


def _card_brand_fits(digits):
    length, head = len(digits), int(digits[:4])
    if digits[0] == "4":
        return length in (13, 16, 19)                      # Visa
    if 5100 <= head <= 5599 or 2221 <= head <= 2720:
        return length == 16                                # Mastercard
    if digits[:2] in ("34", "37"):
        return length == 15                                # Amex
    if (digits[:4] == "6011" or digits[:3] == "622" or digits[:2] in (
            "64", "65", "35")):
        return 16 <= length <= 19                          # Discover, JCB
    if digits[:2] in ("30", "36", "38", "39"):
        return 14 <= length <= 19                          # Diners
    return False


def _luhn_ok(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _card_spans(text):
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if not (_card_brand_fits(digits) and _luhn_ok(digits)):
            continue
        yield match.start(), _card_tail_end(text, match.end())


def _redact_codes(value):
    """Apply the card, keyword and trailing-keyword passes to one text."""
    shadow, index = _shadow(value)
    spans = sorted(span for spans in (
        _card_spans(shadow), _keyword_spans(shadow), _trailing_spans(shadow))
        for span in spans)
    if not spans:
        return value
    if index is not None:
        spans = [(index[start], index[end - 1] + 1) for start, end in spans]
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, last = [], 0
    for start, end in merged:
        out.append(value[last:start])
        out.append("[REDACTED]")
        last = end
    out.append(value[last:])
    return "".join(out)


def redact_secrets(text):
    """Remove obvious credentials before they reach logs, prompts, or memory.

    This is deliberately narrow: product metrics and ordinary identifiers must
    remain intact. The model prompt is still instructed to ignore credentials,
    but source-side redaction is the safety boundary.
    """
    value = str(text or "")
    value = _CODE_RE.sub(r"\1[REDACTED]", value)
    value = _CREDENTIAL_RE.sub(r"\1[REDACTED]", value)
    value = _redact_codes(value)
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
