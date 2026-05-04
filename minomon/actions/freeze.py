from __future__ import annotations

import asyncio
import json
import signal
import time
from typing import Optional

import psutil

from ._common import (
    append_action_log,
    ensure_state_dirs,
    list_paused_sentinels,
    matches_process_identity,
    paused_sentinel_path,
    process_name,
)
from minomon.data.sample import ActionResult


_AUTO_RESUME_TASKS: set[asyncio.Task] = set()
# Hard cap on a single pause window so a forgotten pause never holds
# something hostage indefinitely. Indefinite pauses (auto_resume_seconds=
# None) bypass the cap because the user explicitly opted out.
_PAUSE_HARD_CAP_SECONDS = 30 * 60


def _meta_path(pid: int, start_unix: int):
    return paused_sentinel_path(pid, start_unix).with_suffix(".meta")


def _write_meta(pid: int, start_unix: int, paused_at: float, resume_at: Optional[float]) -> None:
    """Sidecar JSON next to the empty sentinel — lets the UI compute a
    visible countdown and lets `R` (resume all) discover the pause set
    without re-deriving timing. The empty sentinel remains the canonical
    'is paused' signal for the watchdog and crash-recovery."""
    payload = {
        "paused_at": paused_at,
        "resume_at": resume_at,  # null = indefinite, no auto-resume scheduled
    }
    try:
        _meta_path(pid, start_unix).write_text(json.dumps(payload))
    except OSError:
        pass


def _clear_meta(pid: int, start_unix: int) -> None:
    try:
        _meta_path(pid, start_unix).unlink(missing_ok=True)
    except OSError:
        pass


def read_pause_meta(pid: int, start_unix: int) -> Optional[dict]:
    """Read the sidecar metadata for a currently-paused process, or None
    if it doesn't exist (older sentinel, or never had one)."""
    try:
        raw = _meta_path(pid, start_unix).read_text()
    except (OSError, FileNotFoundError):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def freeze(
    pid: int,
    start_unix: int,
    auto_resume_seconds: Optional[int] = 60,
) -> ActionResult:
    """Pause a process via SIGSTOP. `auto_resume_seconds` is the number
    of seconds to wait before auto-CONT. Pass None for an indefinite
    pause — the user (or `R` Resume-All) must thaw it manually."""
    ensure_state_dirs()
    name = process_name(pid)
    if not matches_process_identity(pid, start_unix):
        message = "Process identity check failed."
        append_action_log("freeze", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "freeze")

    indefinite = auto_resume_seconds is None
    if not indefinite:
        auto_resume_seconds = max(1, min(_PAUSE_HARD_CAP_SECONDS, int(auto_resume_seconds)))

    sentinel = paused_sentinel_path(pid, start_unix)
    sentinel.touch(exist_ok=True)

    paused_at = time.time()
    resume_at = None if indefinite else paused_at + float(auto_resume_seconds)
    _write_meta(pid, start_unix, paused_at, resume_at)

    try:
        psutil.Process(pid).send_signal(signal.SIGSTOP)
    except (psutil.Error, OSError) as exc:
        sentinel.unlink(missing_ok=True)
        _clear_meta(pid, start_unix)
        message = f"Failed to stop process: {exc}"
        append_action_log("freeze", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "freeze")

    if not indefinite:
        task = asyncio.create_task(_auto_resume(pid, start_unix, auto_resume_seconds))
        _AUTO_RESUME_TASKS.add(task)
        task.add_done_callback(_AUTO_RESUME_TASKS.discard)
        message = f"Process stopped. Auto-resume in {auto_resume_seconds}s."
    else:
        message = "Process stopped. No auto-resume — use 'u' or Shift-R to resume."

    append_action_log("freeze", pid, start_unix, True, message, name=name)
    return ActionResult(True, message, pid, "freeze")


async def thaw(pid: int, start_unix: int) -> ActionResult:
    ensure_state_dirs()
    name = process_name(pid)
    if not matches_process_identity(pid, start_unix):
        message = "Process identity check failed."
        append_action_log("thaw", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "thaw")

    try:
        psutil.Process(pid).send_signal(signal.SIGCONT)
    except (psutil.Error, OSError) as exc:
        message = f"Failed to continue process: {exc}"
        append_action_log("thaw", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "thaw")

    paused_sentinel_path(pid, start_unix).unlink(missing_ok=True)
    _clear_meta(pid, start_unix)
    message = "Process resumed."
    append_action_log("thaw", pid, start_unix, True, message, name=name)
    return ActionResult(True, message, pid, "thaw")


async def thaw_all() -> tuple[int, int]:
    """Resume every process this monitor currently has paused. Returns
    (resumed, failed). Used by the global Shift-R 'resume all' hotkey."""
    ensure_state_dirs()
    resumed = 0
    failed = 0
    for pid, start_unix, _path in list_paused_sentinels():
        result = await thaw(pid, start_unix)
        if result.success:
            resumed += 1
        else:
            failed += 1
    return resumed, failed


async def resume_orphaned_paused_processes() -> int:
    resumed = 0
    for pid, start_unix, path in list_paused_sentinels():
        if not matches_process_identity(pid, start_unix):
            path.unlink(missing_ok=True)
            _clear_meta(pid, start_unix)
            append_action_log("thaw", pid, start_unix, False, "PID reused or exited; not resuming.", name=process_name(pid))
            continue
        try:
            psutil.Process(pid).send_signal(signal.SIGCONT)
        except (psutil.Error, OSError) as exc:
            append_action_log("thaw", pid, start_unix, False, f"Failed to continue orphaned process: {exc}", name=process_name(pid))
            continue
        path.unlink(missing_ok=True)
        _clear_meta(pid, start_unix)
        append_action_log("thaw", pid, start_unix, True, "Resumed orphaned paused process.", name=process_name(pid))
        resumed += 1
    return resumed


async def _auto_resume(pid: int, start_unix: int, auto_resume_seconds: int) -> None:
    await asyncio.sleep(auto_resume_seconds)
    name = process_name(pid)
    if not matches_process_identity(pid, start_unix):
        paused_sentinel_path(pid, start_unix).unlink(missing_ok=True)
        _clear_meta(pid, start_unix)
        append_action_log("thaw", pid, start_unix, False, "PID reused - not resuming.", name=name)
        return
    try:
        psutil.Process(pid).send_signal(signal.SIGCONT)
    except (psutil.Error, OSError) as exc:
        append_action_log("thaw", pid, start_unix, False, f"Auto-resume failed: {exc}", name=name)
        return
    paused_sentinel_path(pid, start_unix).unlink(missing_ok=True)
    _clear_meta(pid, start_unix)
    append_action_log("thaw", pid, start_unix, True, "Auto-resumed paused process.", name=name)
