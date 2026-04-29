from __future__ import annotations

import asyncio
import signal
import time

import psutil

from ._common import append_action_log, matches_process_identity, process_name, resolve_bundle_id, run_command
from minomon.data.sample import ActionResult


async def quit_app(pid: int, start_unix: int) -> ActionResult:
    name = process_name(pid)
    if not matches_process_identity(pid, start_unix):
        message = "Process identity check failed."
        append_action_log("quit", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "quit")

    bundle_id = resolve_bundle_id(pid)
    if bundle_id:
        script = f'tell application id "{bundle_id}" to quit'
        code, _, stderr = await run_command(["osascript", "-e", script])
        if code == 0 and await _wait_for_exit(pid, start_unix, timeout_seconds=10):
            message = f"Requested quit for {bundle_id}."
            append_action_log("quit", pid, start_unix, True, message, name=name)
            return ActionResult(True, message, pid, "quit")
        append_action_log("quit", pid, start_unix, False, stderr.strip() or "Graceful quit timed out.", name=name)

    try:
        psutil.Process(pid).send_signal(signal.SIGTERM)
    except (psutil.Error, OSError) as exc:
        message = f"Failed to terminate process: {exc}"
        append_action_log("quit", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "quit")

    if await _wait_for_exit(pid, start_unix, timeout_seconds=10):
        message = "Sent SIGTERM."
        append_action_log("quit", pid, start_unix, True, message, name=name)
        return ActionResult(True, message, pid, "quit")

    message = "Process did not exit after quit request."
    append_action_log("quit", pid, start_unix, False, message, name=name)
    return ActionResult(False, message, pid, "quit")


async def _wait_for_exit(pid: int, start_unix: int, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not matches_process_identity(pid, start_unix):
            return True
        await asyncio.sleep(0.25)
    return False
