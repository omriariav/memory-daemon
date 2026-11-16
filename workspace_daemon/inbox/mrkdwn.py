r"""Standard markdown to Slack mrkdwn, for "Copy for Slack" (inbox 0.7.0).

`to_slack_mrkdwn(text)` is a pure function with no I/O. It converts what a
reply draft realistically holds and leaves everything else alone:

| markdown                | Slack mrkdwn          |
| ----------------------- | --------------------- |
| `**bold**`              | `*bold*`              |
| `*italic*`              | `_italic_`            |
| `__x__`                 | `_x_`                 |
| `***x***`               | `*_x_*`               |
| `~~x~~`                 | `~x~`                 |
| `[text](url)`           | `<url\|text>`          |
| `# heading` (1 to 6 #)  | `*heading*`           |
| `* item`, `+ item`      | `- item`              |

Lists (`-`, numbered), quotes (`>`), inline code and fenced code blocks are
kept as they are, and nothing inside code is touched. URLs are never altered
(an underscore in a link stays). Hebrew and any other text pass through
unchanged; markers are found by their neighbours, not by letters.
"""
import re

# Placeholders (private-use characters) keep protected spans out of the
# emphasis rules. They are removed from the input first so text cannot forge one.
_OPEN, _CLOSE = "", ""
_BOLD = ""
_FORGED = re.compile("[-]")

_FENCE = re.compile(r"^\s*```")
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_BULLET = re.compile(r"^(\s*)[*+][ \t]+(.*)$")
_QUOTE = re.compile(r"^(\s*(?:>[ \t]?)+)(.*)$")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_LINK = re.compile(
    r"!?\[([^\]\n]*)\]\(\s*<?([^()\s<>]+(?:\([^()\s]*\)[^()\s<>]*)*)>?"
    r"(?:\s+\"[^\"\n]*\")?\s*\)")
_BARE_URL = re.compile(r"(?:https?|slack)://[^\s<>]+")
_ANGLE = re.compile(r"<[^<>\n]+>")
_BOLD_ITALIC = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC_RE = re.compile(r"(?<![*\w])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![*\w])")
_UNDER_RE = re.compile(r"(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


class _Stash:
    def __init__(self):
        self.items = []

    def put(self, text):
        self.items.append(text)
        return f"{_OPEN}{len(self.items) - 1}{_CLOSE}"

    def restore(self, text):
        # a stashed span may itself hold a placeholder (a link in a link label)
        pattern = re.compile(f"{_OPEN}(\\d+){_CLOSE}")
        while pattern.search(text):
            text = pattern.sub(lambda m: self.items[int(m.group(1))], text)
        return text


def _link(m, stash):
    label, url = m.group(1).strip(), m.group(2)
    return stash.put(f"<{url}|{label}>" if label else f"<{url}>")


def _inline(text, stash):
    """Convert one line's inline markup. Code spans, links, URLs and angle
    tokens are stashed first so no emphasis rule can reach into them."""
    text = _INLINE_CODE.sub(lambda m: stash.put(m.group(0)), text)
    text = _LINK.sub(lambda m: _link(m, stash), text)
    text = _ANGLE.sub(lambda m: stash.put(m.group(0)), text)
    text = _BARE_URL.sub(lambda m: stash.put(m.group(0)), text)
    text = _BOLD_ITALIC.sub(lambda m: f"{_BOLD}_{m.group(1)}_{_BOLD}", text)
    text = _BOLD_RE.sub(lambda m: f"{_BOLD}{m.group(1)}{_BOLD}", text)
    text = _ITALIC_RE.sub(lambda m: f"_{m.group(1)}_", text)
    text = _UNDER_RE.sub(lambda m: f"_{m.group(1)}_", text)
    text = _STRIKE_RE.sub(lambda m: f"~{m.group(1)}~", text)
    return text.replace(_BOLD, "*")


def to_slack_mrkdwn(text):
    """`text` (standard markdown) as Slack mrkdwn. Never raises on a str; a
    non-string gives ""."""
    if not isinstance(text, str):
        return ""
    text = _FORGED.sub("", text)
    stash = _Stash()
    out, in_fence = [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        out.append(_block(line, stash))
    return stash.restore("\n".join(out))


def _block(line, stash):
    if _HR.match(line):
        return line
    head = _HEADING.match(line)
    if head:
        body = head.group(1)
        if not body.strip():
            return line
        body = re.sub(r"\*\*|(?<![\w])__|__(?![\w])", "", body)
        body = _inline(body, stash).strip()
        return f"*{body}*" if body else line
    quote = _QUOTE.match(line)
    if quote:
        return quote.group(1) + _block_rest(quote.group(2), stash)
    return _block_rest(line, stash)


def _block_rest(line, stash):
    bullet = _BULLET.match(line)
    if bullet:
        return f"{bullet.group(1)}- " + _inline(bullet.group(2), stash)
    return _inline(line, stash)
