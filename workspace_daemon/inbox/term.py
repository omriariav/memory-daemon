"""Terminal output hygiene: the inbox must never scroll the alternate screen.

Textual writes a frame as cursor moves and text, with a `\\n` after every row
but the last. That is fine while the frame matches the terminal. When the
terminal gets *shorter* a frame built for the old height is still in flight:
its rows land on the new bottom row and the `\\n` there scrolls the screen by
one line. iTerm2 (default "Save lines to scrollback in alternate screen
mode") keeps each scrolled-off top row in its scrollback, so every resize
leaves stacked copies of the header rows behind.

`no_scroll_driver` wraps a Textual driver class so that every `\\n` it writes
becomes cursor-down + column 1 (`ESC [ B`, `ESC [ G`). That moves exactly as a
newline does, but stops at the bottom row instead of scrolling. Every row of a
frame is placed by an absolute cursor move anyway, so a normal frame is
unchanged, and a stale frame is simply painted over by the resize repaint.
"""

# Newline without the scroll: cursor down (stops at the bottom margin) and
# back to column 1 (the CR half of the tty's own `\n` -> `\r\n`).
NEWLINE = "\x1b[B\x1b[G"

_cache = {}


def no_scroll(data):
    """`data` with every newline replaced by the non-scrolling move."""
    return data.replace("\n", NEWLINE) if "\n" in data else data


def no_scroll_driver(driver_class):
    """A subclass of `driver_class` whose `write` never emits a newline."""
    cached = _cache.get(driver_class)
    if cached is not None:
        return cached

    class NoScrollDriver(driver_class):
        def write(self, data):
            super().write(no_scroll(data))

    NoScrollDriver.__name__ = "NoScroll" + driver_class.__name__
    NoScrollDriver.__qualname__ = NoScrollDriver.__name__
    _cache[driver_class] = NoScrollDriver
    return NoScrollDriver
