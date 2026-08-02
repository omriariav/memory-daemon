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


def npx_bin():
    return _resolve(
        "npx", "WORKSPACE_DAEMON_NPX_BIN",
        "Install Node.js 20 or newer and ensure its bin directory is on PATH.",
    )


_log_file = None
LOG_ROTATE_BYTES = 20 * 1024 * 1024
LOG_BACKUPS = 5


def _rotate_log(path):
    try:
        if not path.exists() or path.stat().st_size < LOG_ROTATE_BYTES:
            return
        oldest = path.with_name(f"{path.name}.{LOG_BACKUPS}")
        if oldest.exists():
            oldest.unlink()
        for index in range(LOG_BACKUPS - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                destination = path.with_name(f"{path.name}.{index + 1}")
                source.replace(destination)
                destination.chmod(0o600)
        destination = path.with_name(f"{path.name}.1")
        path.replace(destination)
        destination.chmod(0o600)
    except OSError:
        # Logging must remain best-effort; the console still carries the run.
        return


def set_log_file(path):
    global _log_file
    _log_file = Path(path)
    _log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(_log_file.parent, 0o700)
    if _log_file.exists():
        os.chmod(_log_file, 0o600)
    for index in range(1, LOG_BACKUPS + 1):
        backup = _log_file.with_name(f"{_log_file.name}.{index}")
        if backup.exists():
            os.chmod(backup, 0o600)
    _rotate_log(_log_file)
    if _log_file.exists():
        os.chmod(_log_file, 0o600)


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    line = f"{utc_now_iso()} {msg}"
    print(line, flush=True)
    if _log_file:
        fd = os.open(_log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
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
