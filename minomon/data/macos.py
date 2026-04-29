from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Optional


_VM_STAT_CACHE: tuple[float, dict[str, int]] | None = None
_MEMORY_PRESSURE_CACHE: tuple[float, tuple[str, int]] | None = None
_FRONT_APP_CACHE: tuple[float, Optional[str]] | None = None
_RUNNING_APPS_CACHE: tuple[float, set[str]] | None = None


def vm_stat() -> dict[str, int]:
    global _VM_STAT_CACHE

    now = time.monotonic()
    if _VM_STAT_CACHE and now - _VM_STAT_CACHE[0] < 0.5:
        return dict(_VM_STAT_CACHE[1])

    output = _run_command(["vm_stat", "-c", "1", "1"])
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if len(lines) < 3:
        raise RuntimeError("vm_stat output was shorter than expected")

    header = lines[1].split()
    values = lines[2].split()
    if len(header) != len(values):
        raise RuntimeError("vm_stat header/value mismatch")

    parsed = {key: _parse_int_token(value) for key, value in zip(header, values)}
    _VM_STAT_CACHE = (now, parsed)
    return dict(parsed)


def memory_pressure() -> tuple[str, int]:
    global _MEMORY_PRESSURE_CACHE

    now = time.monotonic()
    if _MEMORY_PRESSURE_CACHE and now - _MEMORY_PRESSURE_CACHE[0] < 1.0:
        return _MEMORY_PRESSURE_CACHE[1]

    output = _run_command(["memory_pressure"])
    free_match = re.search(r"System-wide memory free percentage:\s*(\d+)%", output)
    free_pct = int(free_match.group(1)) if free_match else 100
    pressure_pct = max(0, min(100, 100 - free_pct))
    if pressure_pct >= 90:
        level = "CRITICAL"
    elif pressure_pct >= 80:
        level = "WARN"
    else:
        level = "NORMAL"

    result = (level, pressure_pct)
    _MEMORY_PRESSURE_CACHE = (now, result)
    return result


def lsappinfo_front() -> Optional[str]:
    global _FRONT_APP_CACHE

    now = time.monotonic()
    if _FRONT_APP_CACHE and now - _FRONT_APP_CACHE[0] < 5.0:
        return _FRONT_APP_CACHE[1]

    bundle_id = _lsappinfo_front()
    if not bundle_id:
        bundle_id = _osascript_front()

    _FRONT_APP_CACHE = (now, bundle_id)
    return bundle_id


def running_apps() -> set[str]:
    global _RUNNING_APPS_CACHE

    now = time.monotonic()
    if _RUNNING_APPS_CACHE and now - _RUNNING_APPS_CACHE[0] < 5.0:
        return set(_RUNNING_APPS_CACHE[1])

    apps = _lsappinfo_running_apps()
    if not apps:
        apps = _osascript_running_apps()

    _RUNNING_APPS_CACHE = (now, apps)
    return set(apps)


def _lsappinfo_front() -> Optional[str]:
    try:
        output = _run_command(["lsappinfo", "front"])
    except Exception:
        return None

    cleaned = output.strip()
    if not cleaned or "[ NULL ]" in cleaned:
        return None

    match = re.search(r'"bundleID"\s*=\s*"([^"]+)"', cleaned)
    if match:
        return match.group(1)

    token_match = re.search(r"\b([A-Za-z0-9][A-Za-z0-9._-]+\.[A-Za-z0-9._-]+)\b", cleaned)
    return token_match.group(1) if token_match else None


def _lsappinfo_running_apps() -> set[str]:
    candidates = [
        ["lsappinfo", "visibleProcessList"],
        ["lsappinfo", "list"],
    ]
    for command in candidates:
        try:
            output = _run_command(command)
        except Exception:
            continue
        bundle_ids = set(re.findall(r"\b([A-Za-z0-9][A-Za-z0-9._-]+\.[A-Za-z0-9._-]+)\b", output))
        if bundle_ids:
            return bundle_ids
    return set()


def _osascript_front() -> Optional[str]:
    script = (
        'tell application "System Events" to get the bundle identifier '
        "of first application process whose frontmost is true"
    )
    try:
        output = _run_command(["osascript", "-e", script]).strip()
    except Exception:
        return None
    return output or None


def _osascript_running_apps() -> set[str]:
    script = (
        'tell application "System Events" to get bundle identifier '
        "of every application process"
    )
    try:
        output = _run_command(["osascript", "-e", script])
    except Exception:
        return set()

    return {
        token.strip()
        for token in output.split(",")
        if token.strip()
    }


def _run_command(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    return completed.stdout


def _parse_int_token(token: str) -> int:
    cleaned = token.strip().rstrip(".")
    match = re.fullmatch(r"(\d+)([KMB]?)", cleaned)
    if not match:
        raise ValueError(f"unrecognized numeric token: {token!r}")

    value = int(match.group(1))
    suffix = match.group(2)
    if suffix == "K":
        return value * 1_000
    if suffix == "M":
        return value * 1_000_000
    if suffix == "B":
        return value * 1_000_000_000
    return value
