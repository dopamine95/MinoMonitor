from __future__ import annotations

import asyncio
import signal

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


async def freeze(pid: int, start_unix: int, auto_resume_seconds: int = 60) -> ActionResult:
    ensure_state_dirs()
    name = process_name(pid)
    if not matches_process_identity(pid, start_unix):
        message = "Process identity check failed."
        append_action_log("freeze", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "freeze")

    auto_resume_seconds = max(1, min(300, int(auto_resume_seconds)))
    sentinel = paused_sentinel_path(pid, start_unix)
    sentinel.touch(exist_ok=True)

    try:
        psutil.Process(pid).send_signal(signal.SIGSTOP)
    except (psutil.Error, OSError) as exc:
        sentinel.unlink(missing_ok=True)
        message = f"Failed to stop process: {exc}"
        append_action_log("freeze", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "freeze")

    task = asyncio.create_task(_auto_resume(pid, start_unix, auto_resume_seconds))
    _AUTO_RESUME_TASKS.add(task)
    task.add_done_callback(_AUTO_RESUME_TASKS.discard)

    message = f"Process stopped. Auto-resume scheduled in {auto_resume_seconds}s."
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
    message = "Process resumed."
    append_action_log("thaw", pid, start_unix, True, message, name=name)
    return ActionResult(True, message, pid, "thaw")


async def resume_orphaned_paused_processes() -> int:
    resumed = 0
    for pid, start_unix, path in list_paused_sentinels():
        if not matches_process_identity(pid, start_unix):
            path.unlink(missing_ok=True)
            append_action_log("thaw", pid, start_unix, False, "PID reused or exited; not resuming.", name=process_name(pid))
            continue
        try:
            psutil.Process(pid).send_signal(signal.SIGCONT)
        except (psutil.Error, OSError) as exc:
            append_action_log("thaw", pid, start_unix, False, f"Failed to continue orphaned process: {exc}", name=process_name(pid))
            continue
        path.unlink(missing_ok=True)
        append_action_log("thaw", pid, start_unix, True, "Resumed orphaned paused process.", name=process_name(pid))
        resumed += 1
    return resumed


async def _auto_resume(pid: int, start_unix: int, auto_resume_seconds: int) -> None:
    await asyncio.sleep(auto_resume_seconds)
    name = process_name(pid)
    if not matches_process_identity(pid, start_unix):
        paused_sentinel_path(pid, start_unix).unlink(missing_ok=True)
        append_action_log("thaw", pid, start_unix, False, "PID reused - not resuming.", name=name)
        return
    try:
        psutil.Process(pid).send_signal(signal.SIGCONT)
    except (psutil.Error, OSError) as exc:
        append_action_log("thaw", pid, start_unix, False, f"Auto-resume failed: {exc}", name=name)
        return
    paused_sentinel_path(pid, start_unix).unlink(missing_ok=True)
    append_action_log("thaw", pid, start_unix, True, "Auto-resumed paused process.", name=name)
