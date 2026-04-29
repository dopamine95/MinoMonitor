from __future__ import annotations

import asyncio
import datetime as dt
import os
import plistlib
from pathlib import Path
from typing import Optional

import psutil

from minomon.data.sample import ActionResult


STATE_DIR = Path(os.environ.get("MINOMONITOR_HOME", str(Path.home() / ".minomonitor"))).expanduser()
PAUSED_DIR = STATE_DIR / "paused"
CALMED_DIR = STATE_DIR / "calmed"
ACTIONS_LOG = STATE_DIR / "actions.log"
MONITOR_PID_FILE = STATE_DIR / "monitor.pid"


def ensure_state_dirs() -> None:
    PAUSED_DIR.mkdir(parents=True, exist_ok=True)
    CALMED_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def matches_process_identity(pid: int, start_unix: int) -> bool:
    try:
        process = psutil.Process(pid)
        return int(process.create_time()) == int(start_unix)
    except (psutil.Error, OSError):
        return False


def process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, OSError):
        return str(pid)


def paused_sentinel_path(pid: int, start_unix: int) -> Path:
    return PAUSED_DIR / f"{pid}_{int(start_unix)}"


def calmed_sentinel_path(pid: int, start_unix: int) -> Path:
    return CALMED_DIR / f"{pid}_{int(start_unix)}"


def list_paused_sentinels() -> list[tuple[int, int, Path]]:
    ensure_state_dirs()
    entries: list[tuple[int, int, Path]] = []
    for path in PAUSED_DIR.iterdir():
        parsed = _parse_sentinel(path)
        if parsed:
            entries.append((*parsed, path))
    return entries


def list_calmed_sentinels() -> list[tuple[int, int, Path]]:
    ensure_state_dirs()
    entries: list[tuple[int, int, Path]] = []
    for path in CALMED_DIR.iterdir():
        parsed = _parse_sentinel(path)
        if parsed:
            entries.append((*parsed, path))
    return entries


def prune_invalid_calmed_sentinels() -> None:
    for pid, start_unix, path in list_calmed_sentinels():
        if not matches_process_identity(pid, start_unix):
            path.unlink(missing_ok=True)
            append_action_log("uncalm", pid, start_unix, False, "PID reused or exited; removing stale calm sentinel.")


def append_action_log(
    action: str,
    pid: int,
    start_unix: int,
    success: bool,
    message: str,
    name: str | None = None,
) -> None:
    ensure_state_dirs()
    timestamp = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    name = name or process_name(pid)
    status = "success" if success else "fail"
    line = (
        f"{timestamp} action={action} pid={pid} start_unix={int(start_unix)} "
        f"name={name!r} status={status} message={message}\n"
    )
    with ACTIONS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)


def make_result(success: bool, message: str, pid: int, action: str) -> ActionResult:
    append_action_log(action, pid, 0, success, message)
    return ActionResult(success=success, message=message, pid=pid, action=action)


def resolve_bundle_id(pid: int) -> Optional[str]:
    try:
        process = psutil.Process(pid)
        exe = process.exe()
    except (psutil.Error, OSError):
        return None

    bundle_root = _find_app_bundle(Path(exe))
    if not bundle_root:
        return None

    info_plist = bundle_root / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as handle:
            info = plistlib.load(handle)
    except Exception:
        return None
    bundle_id = info.get("CFBundleIdentifier")
    return str(bundle_id) if bundle_id else None


async def run_command(command: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "LC_ALL": "C"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode, stdout.decode(), stderr.decode()


def _find_app_bundle(exe_path: Path) -> Optional[Path]:
    current = exe_path.resolve()
    for parent in (current,) + tuple(current.parents):
        if parent.suffix == ".app":
            return parent
    return None


def _parse_sentinel(path: Path) -> Optional[tuple[int, int]]:
    try:
        pid_text, start_text = path.name.split("_", 1)
        return int(pid_text), int(start_text)
    except ValueError:
        return None
