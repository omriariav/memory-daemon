"""Cheap checks before a scheduled tick spends anything.

A laptop with the lid closed is either asleep (launchd runs nothing) or in a
brief DarkWake with no usable network. Running routines then only produces
timeouts. Both guards here skip the whole tick quietly; they never touch
counters or cursors, because nothing was attempted.

Optional `state/daemon.yaml` (every key optional, safe defaults):

    skip_when_lid_closed: true
    network_check:
      enabled: true
      hosts: ["api.anthropic.com:443"]
      timeout_seconds: 3
    offline_warn_hours: 6

The lid guard fails open: any detection error means "do not skip". A broken
config file also falls back to the defaults.
"""

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import yaml

DEFAULT_HOSTS = ("api.anthropic.com:443",)
DEFAULT_TIMEOUT = 3.0
DEFAULT_OFFLINE_WARN_HOURS = 6.0
DETECT_TIMEOUT = 2.0
REASON_LID = "lid closed"
REASON_DARK_WAKE = "dark wake"
REASON_OFFLINE = "offline"
# IOPMrootDomain "System Capabilities" bits (kIOPMSystemCapability*).
_CAP_CPU = 0x1
_CAP_GRAPHICS = 0x2


def config_path(base_dir):
    return Path(base_dir) / "state" / "daemon.yaml"


def defaults():
    return {
        "skip_when_lid_closed": True,
        "network_check": {
            "enabled": True,
            "hosts": list(DEFAULT_HOSTS),
            "timeout_seconds": DEFAULT_TIMEOUT,
        },
        "offline_warn_hours": DEFAULT_OFFLINE_WARN_HOURS,
    }


def _number(value, fallback):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return float(value) if value > 0 else fallback


def _split_host(value):
    host, sep, port = str(value).strip().rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return host.strip("[]"), int(port)


def load_config(base_dir):
    """The optional state/daemon.yaml merged over the defaults; never raises."""
    cfg = defaults()
    try:
        data = yaml.safe_load(config_path(base_dir).read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    if isinstance(data.get("skip_when_lid_closed"), bool):
        cfg["skip_when_lid_closed"] = data["skip_when_lid_closed"]
    net = data.get("network_check")
    if isinstance(net, dict):
        if isinstance(net.get("enabled"), bool):
            cfg["network_check"]["enabled"] = net["enabled"]
        hosts = net.get("hosts")
        if isinstance(hosts, list):
            clean = [h.strip() for h in hosts
                     if isinstance(h, str) and _split_host(h) is not None]
            if clean:
                cfg["network_check"]["hosts"] = clean
        cfg["network_check"]["timeout_seconds"] = _number(
            net.get("timeout_seconds"), DEFAULT_TIMEOUT)
    cfg["offline_warn_hours"] = _number(
        data.get("offline_warn_hours"), DEFAULT_OFFLINE_WARN_HOURS)
    return cfg


def _ioreg(args, run):
    out = run(
        ["ioreg"] + args, capture_output=True, text=True,
        timeout=DETECT_TIMEOUT, check=False,
    )
    if out.returncode != 0:
        raise OSError("ioreg failed")
    return out.stdout


def lid_skip_reason(platform=None, run=subprocess.run):
    """REASON_LID / REASON_DARK_WAKE when the tick should be skipped, else None.

    Commands (each tens of milliseconds, bounded at 2 s):
      ioreg -r -k AppleClamshellState -d 4          lid state
      ioreg -r -c IOMobileFramebufferShim -d 2 -w0  external display ("external" = Yes)
      ioreg -r -n IOPMrootDomain -d 1 -w0           "System Capabilities" (dark wake)
    Any error, an unparseable answer, or a non-macOS host returns None.
    """
    if (platform or sys.platform) != "darwin":
        return None
    try:
        clam = _ioreg(["-r", "-k", "AppleClamshellState", "-d", "4"], run)
        closed = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)', clam)
        if closed and closed.group(1) == "Yes":
            shim = _ioreg(
                ["-r", "-c", "IOMobileFramebufferShim", "-d", "2", "-w0"], run)
            # No framebuffer data at all (e.g. Intel) cannot prove the lack
            # of a display; only an answer without an external one skips.
            if "IOMobileFramebufferShim" in shim \
                    and not re.search(r'"external"\s*=\s*Yes', shim):
                return REASON_LID
        root = _ioreg(["-r", "-n", "IOPMrootDomain", "-d", "1", "-w0"], run)
        caps = re.search(r'"System Capabilities"\s*=\s*(\d+)', root)
        if caps:
            flags = int(caps.group(1))
            if flags & _CAP_CPU and not flags & _CAP_GRAPHICS:
                return REASON_DARK_WAKE
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def offline(hosts, timeout, connect=socket.create_connection):
    """True only when every host refuses a TCP connect within `timeout`."""
    for entry in hosts:
        target = _split_host(entry)
        if target is None:
            continue
        try:
            connect(target, timeout=timeout).close()
            return False
        except OSError:
            continue
    return True


def check(base_dir, platform=None, run=subprocess.run,
          connect=socket.create_connection):
    """The reason to skip this tick, or None to run it.

    MEMORY_DAEMON_PREFLIGHT=0 disables every check (used by the test suite).
    """
    if os.environ.get("MEMORY_DAEMON_PREFLIGHT") == "0":
        return None
    cfg = load_config(base_dir)
    if cfg["skip_when_lid_closed"]:
        reason = lid_skip_reason(platform, run)
        if reason:
            return reason
    net = cfg["network_check"]
    if net["enabled"] and offline(net["hosts"], net["timeout_seconds"], connect):
        return REASON_OFFLINE
    return None
