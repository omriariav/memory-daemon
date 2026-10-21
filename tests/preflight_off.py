"""Test helper: keep the tick preflight from probing the real machine."""
from unittest import mock

from workspace_daemon import preflight


def disable(testcase):
    """Make preflight.check a no-op for one test (undone by addCleanup)."""
    patcher = mock.patch.object(preflight, "check", return_value=None)
    patcher.start()
    testcase.addCleanup(patcher.stop)
