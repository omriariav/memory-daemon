r"""Standard markdown to Slack mrkdwn, for "Copy for Slack" (inbox 0.7.0).

`to_slack_mrkdwn(text)` is a pure function with no I/O. The drafts it gets
usually come from a session that already writes Slack text, where a single
`*x*` is bold and `_x_` is italic. So it converts ONLY constructs that mean
one thing in standard markdown, and leaves everything else exactly as it is:

| markdown                | Slack mrkdwn          |
| ----------------------- | --------------------- |
| `**bold**`, `__bold__`  | `*bold*`              |
| `~~x~~`                 | `~x~`                 |
| `[text](url)`           | `<url\|text>`          |
| `# heading` (1 to 6 #)  | `*heading*`           |

The link label is escaped (`&` `<` `>` become `&amp;` `&lt;` `&gt;`). Single
`*x*` and `_x_`, lists (`-`, `*`, `+`, numbered), quotes (`>`), `***x***`,
inline code, fenced code (``` and ~~~) and indented code blocks are kept
untouched, and nothing inside code is converted. URLs are never altered (an
underscore in a link stays), and a bare URL does not swallow a closing `**` or
`~~`. A `**...**` whose body holds a single `*` is left as it is. Hebrew and any other text pass through unchanged; markers are found by
their neighbours, not by letters.
"""
import re

# Placeholders (private-use characters) keep protected spans out of the
# emphasis rules. They are removed from the input first so text cannot forge one.
_OPEN, _CLOSE = "", ""
_BOLD = ""
_FORGED = re.compile("[-]")

_FENCE = re.compile(r"^\s*(```|~~~)")
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])[ \t]+")
_QUOTE = re.compile(r"^(\s*(?:>[ \t]?)+)(.*)$")
_INDENTED = re.compile(r"^(?: {4}|\t)")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_LINK = re.compile(
    r"!?\[([^\]\n]*)\]\(\s*<?([^()\s<>]+(?:\([^()\s]*\)[^()\s<>]*)*)>?"
    r"(?:\s+\"[^\"\n]*\")?\s*\)")
_BARE_URL = re.compile(r"(?:https?|slack)://[^\s<>]+")
_URL_TAIL = re.compile(r"(?:\*+|~~+|__)+\Z")   # closing markup, not part of a URL
_ANGLE = re.compile(r"<[^<>\n]+>")
# `**x**` but not `***x***` (ambiguous: left alone)
_BOLD_RE = re.compile(
    r"(?<!\*)\*\*(?!\*)(?=\S)(.+?)(?<=\S)(?<!\*)\*\*(?!\*)")
_UNDER_RE = re.compile(r"(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])")
_STRIKE_RE = re.compile(r"(?<!~)~~(?!~)(?=\S)(.+?)(?<=\S)(?<!~)~~(?!~)")


class _Stash:
    def __init__(self):
        self.items = []

    def put(self, text):
        self.items.append(text)
        return f"{_OPEN}{len(self.items) - 1}{_CLOSE}"

    def restore(self, text):
        # a stashed span may itself hold a placeholder
        pattern = re.compile(f"{_OPEN}(\\d+){_CLOSE}")
        while pattern.search(text):
            text = pattern.sub(lambda m: self.items[int(m.group(1))], text)
        return text


def _escape(label):
    return label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(m, stash):
    label, url = _escape(m.group(1).strip()), m.group(2)
    return stash.put(f"<{url}|{label}>" if label else f"<{url}>")


def _bare_url(m, stash):
    url = m.group(0)
    tail = _URL_TAIL.search(url)
    if tail and tail.start() > 0:
        return stash.put(url[:tail.start()]) + url[tail.start():]
    return stash.put(url)


def _bold(m):
    """`*body*`, unless the body holds a single `*` (Slack bold or an
    ambiguous mix): then the outer markers are left as they are."""
    body = m.group(1)
    if "*" in body:
        return m.group(0)
    return f"{_BOLD}{body}{_BOLD}"


def _inline(text, stash):
    """Convert one line's inline markup. Code spans, links, URLs and angle
    tokens are stashed first so no emphasis rule can reach into them."""
    text = _INLINE_CODE.sub(lambda m: stash.put(m.group(0)), text)
    text = _LINK.sub(lambda m: _link(m, stash), text)
    text = _ANGLE.sub(lambda m: stash.put(m.group(0)), text)
    text = _BARE_URL.sub(lambda m: _bare_url(m, stash), text)
    text = _BOLD_RE.sub(_bold, text)
    text = _UNDER_RE.sub(_bold, text)
    text = _STRIKE_RE.sub(lambda m: f"~{m.group(1)}~", text)
    return text.replace(_BOLD, "*")


def to_slack_mrkdwn(text):
    """`text` (standard markdown) as Slack mrkdwn. Never raises on a str; a
    non-string gives ""."""
    if not isinstance(text, str):
        return ""
    text = _FORGED.sub("", text)
    stash = _Stash()
    out = []
    fence = None            # the open fence marker (``` or ~~~)
    in_list = False         # an indented line then continues a list item
    prev_blank, prev_code = True, False
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is not None:
            out.append(line)
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            prev_blank = prev_code = False
            continue
        blank = not line.strip()
        if (not blank and _INDENTED.match(line) and not in_list
                and (prev_blank or prev_code)):
            out.append(line)                  # an indented code block
            prev_blank, prev_code = False, True
            continue
        if not blank:
            if _LIST_ITEM.match(line):
                in_list = True
            elif not line[:1].isspace():
                in_list = False
        out.append(line if blank else _block(line, stash))
        prev_blank, prev_code = blank, False
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
        return quote.group(1) + _inline(quote.group(2), stash)
    return _inline(line, stash)
