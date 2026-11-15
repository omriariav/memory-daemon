"""Hebrew/RTL display helpers. Kept in one module so a fix drops in here.

Terminals differ on bidi. Most do not run the Unicode bidi algorithm, so
Hebrew in logical order reads backwards and python-bidi's get_display()
(visual order) fixes it. Terminals that do run bidi (iTerm2 with bidirectional
text on, some VTE/WezTerm setups) reverse a visual-order string back, so they
want logical order.

One setting picks the order: `bidi_mode` = `logical | visual | auto`.

- `logical`: text is passed through untouched (the terminal does bidi).
- `visual`: wrap, then get_display() each line (the terminal does not).
- `auto`: choose by $TERM_PROGRAM from AUTO_TABLE, else AUTO_DEFAULT.

Precedence: $INBOX_BIDI, then `bidi_mode` from inbox.yaml, then `auto`. The
legacy values `on` (= visual) and `off` (= logical) are accepted. iTerm2 does
its own bidi (checked on iTerm 3.7), so auto maps it to logical; unknown
terminals fall back to visual.

Wrapping must happen BEFORE reordering (a reordered line wrapped later breaks
mid-sentence in the wrong place), so callers wrap logical text, then reorder
each wrapped line.

python-bidi is optional: without it `visual` degrades to logical.
"""
import os
import re
import textwrap
import unicodedata

try:  # optional extra
    from bidi import get_display as _get_display
except ImportError:  # pragma: no cover - depends on the environment
    _get_display = None

try:
    from rich.cells import cell_len
except ImportError:  # Rich arrives with Textual; plain use needs no UI
    def cell_len(text):
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else
                   (0 if unicodedata.combining(c) else 1) for c in text)

_RTL = re.compile("[֐-׿؀-ۿݐ-ݿ"
                  "יִ-﷿ﹰ-﻿]")

MODES = ("logical", "visual", "auto")
ENV_VAR = "INBOX_BIDI"

# TERM_PROGRAM -> effective mode. iTerm2 is verified (it runs bidi itself, so
# a visual-order string is double-reversed there). The others are unverified
# guesses; flip an entry, or AUTO_DEFAULT, once a terminal is checked.
AUTO_TABLE = {
    "iTerm.app": "logical",
    "Apple_Terminal": "visual",
    "vscode": "visual",
}
AUTO_DEFAULT = "visual"

_ALIASES = {"on": "visual", "true": "visual", "1": "visual",
            "off": "logical", "false": "logical", "0": "logical"}


def available():
    """True when python-bidi is installed (visual order works)."""
    return _get_display is not None


def normalize_mode(value):
    """Map a raw setting to logical | visual | auto (unknown or empty -> auto)."""
    v = (value or "").strip().lower()
    v = _ALIASES.get(v, v)
    return v if v in MODES else "auto"


def configured_mode(config_value=None, env=None):
    """$INBOX_BIDI wins over the config value; result is logical|visual|auto."""
    e = os.environ if env is None else env
    raw = e.get(ENV_VAR)
    return normalize_mode(raw if raw not in (None, "") else config_value)


def resolve(mode, env=None):
    """Effective mode, `logical` or `visual`. `auto` looks up $TERM_PROGRAM."""
    m = normalize_mode(mode)
    if m == "auto":
        e = os.environ if env is None else env
        m = AUTO_TABLE.get(e.get("TERM_PROGRAM", ""), AUTO_DEFAULT)
    return m


def has_rtl(s):
    return bool(_RTL.search(s))


def is_rtl_paragraph(s):
    """True when the first strong character is RTL (paragraph direction)."""
    for ch in s:
        if _RTL.match(ch):
            return True
        if ch.isalpha():
            return False
    return False


def display(line):
    """get_display() when python-bidi is installed, else the line unchanged."""
    return _get_display(line) if _get_display else line


def wrap_cells(s, width):
    if width <= 0 or cell_len(s) <= width:
        return [s]
    return textwrap.wrap(s, width=width, break_long_words=True,
                         drop_whitespace=True) or [s]


def visual_lines(s, width, visual):
    """Wrap logical text to `width`; when `visual`, reorder each line."""
    lines = wrap_cells(s, width)
    if visual and has_rtl(s):
        return [display(ln) for ln in lines]
    return lines
