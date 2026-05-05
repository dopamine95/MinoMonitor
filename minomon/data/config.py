"""
User configuration at ~/.minomonitor/config.toml.

Tiny intentionally — just two lists today:
    pin   = bundle ids or process names to always protect
    unpin = entries that override the baked-in deny list

We read with stdlib tomllib (Python 3.11+). We write our own minimal
serializer because tomllib is read-only and our data is just two flat
arrays of strings — no need to pull in tomli-w as a dependency.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Iterable

CONFIG_PATH = Path.home() / ".minomonitor" / "config.toml"


def load_user_config() -> dict[str, list[str]]:
    """Returns {'pin': [...], 'unpin': [...]} — empty lists when the file
    is missing or malformed. Never raises; bad config is treated as no
    config so the monitor still boots."""
    if not CONFIG_PATH.exists():
        return {"pin": [], "unpin": []}
    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {"pin": [], "unpin": []}
    pin = [str(x) for x in data.get("pin", []) if isinstance(x, str)]
    unpin = [str(x) for x in data.get("unpin", []) if isinstance(x, str)]
    return {"pin": pin, "unpin": unpin}


def load_automode_config() -> dict:
    """Reads the [automode] section. Default-disabled. Returns:

        {
            "enabled":        bool,
            "max_per_hour":   int,
            "idle_minimum":   int,    # seconds
        }

    Auto-mode only ever fires `calm` (taskpolicy -b), never SIGSTOP or
    quit. The cap fields below are upper bounds — a sensible user could
    tighten them but not loosen them past the safety ceilings.
    """
    defaults = {
        "enabled": False,
        "max_per_hour": 2,        # ceiling: 6
        "idle_minimum": 60 * 60,  # seconds — 1 hour minimum
    }
    if not CONFIG_PATH.exists():
        return defaults
    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return defaults
    am = data.get("automode")
    if not isinstance(am, dict):
        return defaults
    enabled = bool(am.get("enabled", False))
    try:
        max_per_hour = int(am.get("max_per_hour", 2))
        max_per_hour = max(1, min(6, max_per_hour))   # safety ceiling
    except (TypeError, ValueError):
        max_per_hour = 2
    try:
        idle_minimum = int(am.get("idle_minimum_seconds", 60 * 60))
        idle_minimum = max(15 * 60, min(6 * 3600, idle_minimum))  # 15m..6h
    except (TypeError, ValueError):
        idle_minimum = 60 * 60
    return {
        "enabled": enabled,
        "max_per_hour": max_per_hour,
        "idle_minimum": idle_minimum,
    }


def load_advisor_config() -> dict:
    """Reads the [advisor] section. Default-disabled — `minomon advise`
    is a no-op until the user explicitly opts in. Returns:

        {
            "engine":  "none" | "claude-code",
            "timeout_seconds": int,
        }
    """
    defaults = {"engine": "none", "timeout_seconds": 60}
    if not CONFIG_PATH.exists():
        return defaults
    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return defaults
    advisor = data.get("advisor")
    if not isinstance(advisor, dict):
        return defaults
    engine = str(advisor.get("engine", "none")).strip().lower()
    if engine not in {"none", "claude-code"}:
        engine = "none"
    try:
        timeout = int(advisor.get("timeout_seconds", 60))
        timeout = max(10, min(600, timeout))
    except (TypeError, ValueError):
        timeout = 60
    return {"engine": engine, "timeout_seconds": timeout}


def save_user_config(pin: Iterable[str], unpin: Iterable[str]) -> None:
    """Atomic write. Sorts and de-duplicates so the file stays clean
    after many edits."""
    pin_sorted = sorted({p.strip() for p in pin if p and p.strip()})
    unpin_sorted = sorted({u.strip() for u in unpin if u and u.strip()})

    lines = [
        "# ~/.minomonitor/config.toml — user config for Mino Monitor",
        "#",
        "# `pin`   — bundle ids or process names you want to ALWAYS protect",
        "#           from calm/pause/quit (UI greys out the action buttons).",
        "# `unpin` — entries that OVERRIDE the baked-in deny list. Use this",
        "#           when you actually want to manage a default-pinned app",
        "#           like Xcode.",
        "#",
        "# Names match the 'Name' column exactly. Group suffixes like ' ×8'",
        "# are stripped before comparison, so 'Brave Browser' matches the",
        "# whole grouped row.",
        "",
        "pin = [",
        *[f'    "{p}",' for p in pin_sorted],
        "]",
        "",
        "unpin = [",
        *[f'    "{u}",' for u in unpin_sorted],
        "]",
        "",
    ]

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def add_pin(item: str) -> None:
    cfg = load_user_config()
    if item in cfg["pin"]:
        return
    cfg["pin"].append(item)
    # Pinning something also clears it from the unpin list if present
    # (otherwise the lists contradict each other).
    cfg["unpin"] = [u for u in cfg["unpin"] if u != item]
    save_user_config(cfg["pin"], cfg["unpin"])


def remove_pin(item: str) -> None:
    cfg = load_user_config()
    cfg["pin"] = [p for p in cfg["pin"] if p != item]
    save_user_config(cfg["pin"], cfg["unpin"])


def add_unpin(item: str) -> None:
    cfg = load_user_config()
    if item in cfg["unpin"]:
        return
    cfg["unpin"].append(item)
    cfg["pin"] = [p for p in cfg["pin"] if p != item]
    save_user_config(cfg["pin"], cfg["unpin"])


def remove_unpin(item: str) -> None:
    cfg = load_user_config()
    cfg["unpin"] = [u for u in cfg["unpin"] if u != item]
    save_user_config(cfg["pin"], cfg["unpin"])
