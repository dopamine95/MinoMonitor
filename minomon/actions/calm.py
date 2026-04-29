from __future__ import annotations

from ._common import (
    append_action_log,
    calmed_sentinel_path,
    ensure_state_dirs,
    matches_process_identity,
    process_name,
    run_command,
)
from minomon.data.sample import ActionResult


async def calm(pid: int, start_unix: int) -> ActionResult:
    ensure_state_dirs()
    name = process_name(pid)
    if not matches_process_identity(pid, start_unix):
        message = "Process identity check failed."
        append_action_log("calm", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "calm")

    code, _, stderr = await run_command(["taskpolicy", "-b", str(pid)])
    if code != 0:
        message = stderr.strip() or f"taskpolicy exited with status {code}"
        append_action_log("calm", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "calm")

    calmed_sentinel_path(pid, start_unix).touch(exist_ok=True)
    message = "Background QoS applied."
    append_action_log("calm", pid, start_unix, True, message, name=name)
    return ActionResult(True, message, pid, "calm")


async def uncalm(pid: int, start_unix: int) -> ActionResult:
    ensure_state_dirs()
    name = process_name(pid)
    if not matches_process_identity(pid, start_unix):
        message = "Process identity check failed."
        append_action_log("uncalm", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "uncalm")

    code, _, stderr = await run_command(["taskpolicy", "-B", str(pid)])
    if code != 0:
        message = stderr.strip() or f"taskpolicy exited with status {code}"
        append_action_log("uncalm", pid, start_unix, False, message, name=name)
        return ActionResult(False, message, pid, "uncalm")

    calmed_sentinel_path(pid, start_unix).unlink(missing_ok=True)
    message = "Default QoS restored."
    append_action_log("uncalm", pid, start_unix, True, message, name=name)
    return ActionResult(True, message, pid, "uncalm")
