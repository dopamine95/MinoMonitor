from __future__ import annotations

import os
import plistlib
import re
from pathlib import Path
from typing import Optional

import psutil

from .macos import lsappinfo_front


# System processes that are dangerous to suspend or quit. Pinned in the UI
# (action buttons greyed out). This list is intentionally conservative —
# anything in flight that the kernel watchdogs care about, anything that
# parents can restart on a timeout, anything that owns shared infrastructure.
PINNED_NAMES = {
    "kernel_task",
    "WindowServer",
    "Finder",
    "Dock",
    "SystemUIServer",
    "launchd",
    "loginwindow",
    # Spotlight + metadata
    "mds",
    "mds_stores",
    "mdworker",
    "mdworker_shared",
    "fseventsd",
    # Daemons that other things assume are alive
    "distnoted",
    "cfprefsd",
    "runningboardd",
    "backupd",
    "bird",
    "cloudd",
    "nsurlsessiond",
    "photolibraryd",
    "fileproviderd",
    "sharingd",
    "rapportd",
    "coreduetd",
    "locationd",
    "tccd",
    "coreaudiod",
    "bluetoothd",
    "controlcenter",
    # Common dev tooling. Remove if you want these manageable.
    "Xcode",
}

PINNED_BUNDLE_IDS = {
    "com.apple.finder",
    "com.apple.dock",
    "com.apple.systemuiserver",
    "com.apple.dt.Xcode",
    # Common terminals — auto-detection in add_terminal_app() also handles
    # whichever terminal the user actually launched the monitor from.
    "com.apple.Terminal",
    "com.googlecode.iterm2",
    "net.kovidgoyal.kitty",
    "io.alacritty",
    "co.zeit.hyper",
    "com.mitchellh.ghostty",
}

AUDIO_BUNDLE_IDS = {
    "com.spotify.client",
    "com.hnc.Discord",
    "com.tinyspeck.slackmac",
    "us.zoom.xos",
    "com.apple.Music",
    "com.apple.QuickTimePlayerX",
    "com.cockos.reaper",
    "com.ableton.live",
}

SOCKET_HEAVY_BUNDLE_IDS = {
    "com.tinyspeck.slackmac",
    "com.hnc.Discord",
    "com.spotify.client",
    "com.microsoft.teams2",
    "com.apple.MobileSMS",
    "com.apple.mail",
    "com.apple.Safari",
    "com.google.Chrome",
}

_TERMINAL_BUNDLE_IDS: set[str] = set()
_TERMINAL_NAMES: set[str] = set()

# User config: pins added/removed via the `p` keybinding, persisted at
# ~/.minomonitor/config.toml. Loaded on demand so changes from another
# session show up next refresh without a server-side poll.
_USER_CONFIG_LOAD_INTERVAL = 5.0  # seconds — cheap file stat on each call
_USER_PINS: set[str] = set()
_USER_UNPINS: set[str] = set()
_LAST_USER_CONFIG_LOAD: float = 0.0


def _strip_group_suffix(name: str) -> str:
    """Group rows render as 'Brave Browser ×8'. When matching against the
    user pin/unpin lists we want the bare app name."""
    return re.sub(r"\s*[×x]\s*\d+$", "", name).strip()


def _refresh_user_config() -> None:
    global _USER_PINS, _USER_UNPINS, _LAST_USER_CONFIG_LOAD
    import time as _t
    now = _t.monotonic()
    if now - _LAST_USER_CONFIG_LOAD < _USER_CONFIG_LOAD_INTERVAL:
        return
    _LAST_USER_CONFIG_LOAD = now
    try:
        from .config import load_user_config
        cfg = load_user_config()
        _USER_PINS = set(cfg.get("pin", []))
        _USER_UNPINS = set(cfg.get("unpin", []))
    except Exception:
        # Bad config => no user pins; never break the sampler over it.
        _USER_PINS = set()
        _USER_UNPINS = set()


def add_terminal_app() -> None:
    bundle_id = lsappinfo_front()
    term_program = os.environ.get("TERM_PROGRAM", "").strip()
    if bundle_id:
        _TERMINAL_BUNDLE_IDS.add(bundle_id)
    if term_program:
        _TERMINAL_NAMES.add(term_program)
    for candidate in _parent_chain_bundle_ids():
        _TERMINAL_BUNDLE_IDS.add(candidate)
    _refresh_user_config()


def is_pinned(name: str, bundle_id: Optional[str]) -> bool:
    _refresh_user_config()
    normalized_name = name.strip()
    bare_name = _strip_group_suffix(normalized_name)

    # 1. User unpin overrides the baked-in deny list. Always wins.
    if normalized_name in _USER_UNPINS or bare_name in _USER_UNPINS:
        return False
    if bundle_id and bundle_id in _USER_UNPINS:
        return False

    # 2. User pin always pins, regardless of system list.
    if normalized_name in _USER_PINS or bare_name in _USER_PINS:
        return True
    if bundle_id and bundle_id in _USER_PINS:
        return True

    # 3. Baked-in protections.
    if normalized_name in PINNED_NAMES or bare_name in PINNED_NAMES:
        return True
    if bundle_id and bundle_id in PINNED_BUNDLE_IDS:
        return True
    if bundle_id and bundle_id in _TERMINAL_BUNDLE_IDS:
        return True
    if normalized_name and normalized_name in _TERMINAL_NAMES:
        return True
    return normalized_name.lower().endswith("terminal")


def is_user_pinned(name: str, bundle_id: Optional[str]) -> bool:
    """True only when the pin comes from the user's config — used by the
    `p` toggle to choose between 'add to user pin' and 'remove from user
    pin'."""
    _refresh_user_config()
    bare_name = _strip_group_suffix(name.strip())
    if name in _USER_PINS or bare_name in _USER_PINS:
        return True
    if bundle_id and bundle_id in _USER_PINS:
        return True
    return False


def _parent_chain_bundle_ids() -> set[str]:
    bundle_ids: set[str] = set()
    try:
        process = psutil.Process(os.getpid())
    except (psutil.Error, OSError):
        return bundle_ids

    try:
        parents = process.parents()
    except (psutil.Error, OSError, PermissionError):
        return bundle_ids

    for parent in parents:
        try:
            exe = Path(parent.exe()).resolve()
        except (psutil.Error, OSError, PermissionError):
            continue
        bundle_root = next((node for node in (exe,) + tuple(exe.parents) if node.suffix == ".app"), None)
        if not bundle_root:
            continue
        info_plist = bundle_root / "Contents" / "Info.plist"
        try:
            with info_plist.open("rb") as handle:
                payload = plistlib.load(handle)
        except Exception:
            continue
        bundle_id = payload.get("CFBundleIdentifier")
        if bundle_id:
            bundle_ids.add(str(bundle_id))
    return bundle_ids
