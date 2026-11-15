"""Theme names and palettes. Plain data; Textual objects are built lazily so
the settings loader works without Textual."""

DEFAULT = "dark"
NAMES = ("dark", "light")

PALETTES = {
    "dark": dict(primary="#7aa2f7", secondary="#bb9af7", accent="#7dcfff",
                 warning="#e0af68", error="#ff6b6b", success="#9ece6a",
                 background="#14151f", surface="#1a1b26", panel="#24283b",
                 foreground="#c8d0f5", dark=True),
    "light": dict(primary="#2c55c9", secondary="#7a3fc4", accent="#0b7a9e",
                  warning="#a45a00", error="#c92a2a", success="#2b8a3e",
                  background="#f4f5fa", surface="#ffffff", panel="#e3e6f2",
                  foreground="#1f2335", dark=False),
}


def theme_id(name):
    return f"inbox-{name}"


def build_textual_themes():
    """`[Theme, ...]` for every palette (imports Textual)."""
    from textual.theme import Theme
    return [Theme(name=theme_id(n), **p) for n, p in PALETTES.items()]
