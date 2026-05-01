from __future__ import annotations

import os
import plistlib
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


def add_terminal_app() -> None:
    bundle_id = lsappinfo_front()
    term_program = os.environ.get("TERM_PROGRAM", "").strip()
    if bundle_id:
        _TERMINAL_BUNDLE_IDS.add(bundle_id)
    if term_program:
        _TERMINAL_NAMES.add(term_program)
    for candidate in _parent_chain_bundle_ids():
        _TERMINAL_BUNDLE_IDS.add(candidate)


def is_pinned(name: str, bundle_id: Optional[str]) -> bool:
    normalized_name = name.strip()
    if normalized_name in PINNED_NAMES:
        return True
    if bundle_id and bundle_id in PINNED_BUNDLE_IDS:
        return True
    if bundle_id and bundle_id in _TERMINAL_BUNDLE_IDS:
        return True
    if normalized_name and normalized_name in _TERMINAL_NAMES:
        return True
    lowered = normalized_name.lower()
    return lowered.endswith("terminal")


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
