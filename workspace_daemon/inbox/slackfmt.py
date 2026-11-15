"""Render Slack mrkdwn into Rich Text: bold, italic, strike, code, links and
mention chips. Used for Slack items only; other sources render as plain text."""
import re

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from . import bidi

_TOKEN = re.compile(
    r"(?P<angle><[^>\n]+>)|(?P<code>`[^`\n]+`)|(?P<bold>\*[^*\n]+\*)"
    r"|(?P<ital>(?<!\w)_[^_\n]+_(?!\w))|(?P<strike>~[^~\n]+~)"
)


def _angle(tok):
    """(label, kind) for <...> tokens: link | user | channel | special."""
    inner = tok[1:-1]
    target, _, label = inner.partition("|")
    if target.startswith("@"):
        return "@" + (label or target[1:]), "user"
    if target.startswith("#"):
        return "#" + (label or target[1:]), "channel"
    if target.startswith("!"):
        return "@" + (label or target[1:]), "special"
    return label or target, "link"


def plain(text):
    """Strip mrkdwn to plain text (mentions become @name, links their label)."""
    def sub(m):
        t = m.group(0)
        if m.lastgroup == "angle":
            return _angle(t)[0]
        return t[1:-1]
    return _TOKEN.sub(sub, text)


def render_plain(text, pal, width=80, visual=False):
    """Plain text body (no mrkdwn), with RTL handling when `visual`."""
    out = Text(no_wrap=False)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        _rtl_or_plain(out, line, pal, width, visual)
        if i < len(lines) - 1:
            out.append("\n")
    return out


def _rtl_or_plain(out, line, pal, width, visual):
    if visual and bidi.has_rtl(line):
        right = bidi.is_rtl_paragraph(line)
        vls = bidi.visual_lines(line, width - 2, True)
        for k, vl in enumerate(vls):
            pad = max(0, width - 2 - cell_len(vl)) if right else 0
            out.append(" " * pad + vl, Style(color=pal["fg"]))
            if k < len(vls) - 1:
                out.append("\n")
    else:
        out.append(line)


def render(text, pal, width=80, visual=False):
    """Text for a Slack message body. `pal` holds colour strings."""
    out = Text(no_wrap=False)
    in_block = False
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_block = not in_block
            rest = line.strip()[3:]
            if not rest:
                continue
            line = rest
            if rest.endswith("```"):
                in_block = False
                line = rest[:-3]
        if in_block:
            out.append("  " + line, Style(color=pal["fg"], bgcolor=pal["code_bg"]))
        elif visual and bidi.has_rtl(line):
            _rtl_or_plain(out, plain(line), pal, width, True)
        else:
            _styled(out, line, pal)
        if i < len(lines) - 1:
            out.append("\n")
    return out


def _styled(out, line, pal):
    if line.startswith("> "):
        out.append("▎ ", Style(color=pal["muted"]))
        line = line[2:]
    pos = 0
    for m in _TOKEN.finditer(line):
        out.append(line[pos:m.start()])
        tok, kind = m.group(0), m.lastgroup
        if kind == "angle":
            label, k = _angle(tok)
            if k == "link":
                out.append(label, Style(color=pal["link"], underline=True))
            else:
                out.append(f" {label} ", Style(color=pal["chip_fg"],
                                               bgcolor=pal["chip_bg"], bold=True))
        elif kind == "code":
            out.append(f" {tok[1:-1]} ", Style(color=pal["code_fg"],
                                               bgcolor=pal["code_bg"]))
        elif kind == "bold":
            out.append(tok[1:-1], Style(bold=True))
        elif kind == "ital":
            out.append(tok[1:-1], Style(italic=True))
        else:
            out.append(tok[1:-1], Style(strike=True, color=pal["muted"]))
        pos = m.end()
    out.append(line[pos:])
