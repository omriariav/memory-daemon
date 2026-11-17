"""Unified terminal inbox: item format, sources, local state and the UI.

Everything here except `app` (and the widgets it uses) is plain stdlib so it
can be tested without Textual. See docs/inbox.md.
"""

# The inbox component version (SemVer). Single source of truth: the CLI,
# the header, the help overlay and docs/inbox-changelog.md all follow this.
__version__ = "0.9.0"
