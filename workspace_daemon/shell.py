"""Subprocess helpers, binary resolution, and logging shared by adapters."""
import datetime
import json
import os
import shutil
import subprocess
from pathlib import Path


class MissingBinary(RuntimeError):
    pass


def _resolve(name, env_var, install_hint):
    """Find a required binary: explicit env override, then PATH.

    launchd does not inherit a login shell's PATH, so the LaunchAgent plist sets
    PATH explicitly (or you can pin the absolute path via the env var).
    """
    override = os.environ.get(env_var)
    if override:
        if not Path(override).exists():
            raise MissingBinary(f"{env_var}={override} does not exist")
        return override
    found = shutil.which(name)
    if not found:
        raise MissingBinary(
            f"required binary '{name}' not found on PATH. {install_hint} "
            f"Or set {env_var} to its absolute path."
        )
    return found


def gws_bin():
    return _resolve(
        "gws", "WORKSPACE_DAEMON_GWS_BIN",
        "Install workspace-cli (https://github.com/omriariav/workspace-cli) "
        "and run `gws auth login`.",
    )


def yoetz_bin():
    return _resolve(
        "yoetz", "WORKSPACE_DAEMON_YOETZ_BIN",
        "Install yoetz: `brew install avivsinai/tap/yoetz`.",
    )


def ada_bin():
    return _resolve(
        "ada", "WORKSPACE_DAEMON_ADA_BIN",
        "Install and authenticate the Ada CLI.",
    )


_log_file = None


def set_log_file(path):
    global _log_file
    _log_file = Path(path)
    _log_file.parent.mkdir(parents=True, exist_ok=True)


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    line = f"{utc_now_iso()} {msg}"
    print(line, flush=True)
    if _log_file:
        with open(_log_file, "a") as f:
            f.write(line + "\n")


def run(cmd, timeout=120, check=True):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd[:3])}... -> {result.stderr.strip()[:400]}"
        )
    return result


def run_json(cmd, timeout=120):
    result = run(cmd, timeout=timeout)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"expected JSON from {' '.join(cmd[:3])}..., got: {result.stdout.strip()[:300]!r}"
        ) from exc
