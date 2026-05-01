from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from typing import Optional


_VM_STAT_CACHE: tuple[float, dict[str, int]] | None = None
_MEMORY_PRESSURE_CACHE: tuple[float, str] | None = None
_FRONT_APP_CACHE: tuple[float, Optional[str]] | None = None
_RUNNING_APPS_CACHE: tuple[float, set[str]] | None = None
_PERF_LEVEL_CACHE: tuple[float, tuple[int, int] | None] | None = None

_LIBSYSTEM = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
_KERN_SUCCESS = 0
_MACH_PORT_NULL = 0
_TASK_VM_INFO = 22

# proc_pid_rusage flavors. RUSAGE_INFO_V6 is the latest macOS 12.0+ struct
# and contains ri_phys_footprint at offset 19 * sizeof(uint64). This is the
# same number Activity Monitor / footprint(1) / top -stats mem display.
_RUSAGE_INFO_V6 = 6


class _RUsageInfoV6(ctypes.Structure):
    """rusage_info_v6 from <sys/resource.h>. Field order matters — must match
    the kernel's exact layout. Fields after ri_phys_footprint are needed for
    correct struct size but we only read ri_phys_footprint."""
    _fields_ = [
        ("ri_uuid",                       ctypes.c_uint8 * 16),
        ("ri_user_time",                  ctypes.c_uint64),
        ("ri_system_time",                ctypes.c_uint64),
        ("ri_pkg_idle_wkups",             ctypes.c_uint64),
        ("ri_interrupt_wkups",            ctypes.c_uint64),
        ("ri_pageins",                    ctypes.c_uint64),
        ("ri_wired_size",                 ctypes.c_uint64),
        ("ri_resident_size",              ctypes.c_uint64),
        ("ri_phys_footprint",             ctypes.c_uint64),  # ← what we want
        ("ri_proc_start_abstime",         ctypes.c_uint64),
        ("ri_proc_exit_abstime",          ctypes.c_uint64),
        ("ri_child_user_time",            ctypes.c_uint64),
        ("ri_child_system_time",          ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups",       ctypes.c_uint64),
        ("ri_child_interrupt_wkups",      ctypes.c_uint64),
        ("ri_child_pageins",              ctypes.c_uint64),
        ("ri_child_elapsed_abstime",      ctypes.c_uint64),
        ("ri_diskio_bytesread",           ctypes.c_uint64),
        ("ri_diskio_byteswritten",        ctypes.c_uint64),
        ("ri_cpu_time_qos_default",       ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance",   ctypes.c_uint64),
        ("ri_cpu_time_qos_background",    ctypes.c_uint64),
        ("ri_cpu_time_qos_utility",       ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy",        ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated",ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time",         ctypes.c_uint64),
        ("ri_serviced_system_time",       ctypes.c_uint64),
        ("ri_logical_writes",             ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint",ctypes.c_uint64),
        ("ri_instructions",               ctypes.c_uint64),
        ("ri_cycles",                     ctypes.c_uint64),
        ("ri_billed_energy",              ctypes.c_uint64),
        ("ri_serviced_energy",            ctypes.c_uint64),
        ("ri_interval_max_phys_footprint",ctypes.c_uint64),
        ("ri_runnable_time",              ctypes.c_uint64),
        ("ri_flags",                      ctypes.c_uint64),
        # V5/V6 padding fields — present so sizeof() matches kernel expectation
        ("ri_user_ptime",                 ctypes.c_uint64),
        ("ri_system_ptime",               ctypes.c_uint64),
        ("ri_pinstructions",              ctypes.c_uint64),
        ("ri_pcycles",                    ctypes.c_uint64),
        ("ri_energy_nj",                  ctypes.c_uint64),
        ("ri_penergy_nj",                 ctypes.c_uint64),
        ("ri_secure_time_in_system",      ctypes.c_uint64),
        ("ri_secure_ptime_in_system",     ctypes.c_uint64),
        ("ri_neural_footprint",           ctypes.c_uint64),
        ("ri_lifetime_max_neural_footprint", ctypes.c_uint64),
        ("ri_interval_max_neural_footprint", ctypes.c_uint64),
        ("ri_reserved",                   ctypes.c_uint64 * 9),
    ]


class _TimeValue(ctypes.Structure):
    _fields_ = [
        ("seconds", ctypes.c_int),
        ("microseconds", ctypes.c_int),
    ]


class _TaskVmInfo(ctypes.Structure):
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("region_count", ctypes.c_int),
        ("page_size", ctypes.c_int),
        ("resident_size", ctypes.c_uint64),
        ("resident_size_peak", ctypes.c_uint64),
        ("device", ctypes.c_uint64),
        ("device_peak", ctypes.c_uint64),
        ("internal", ctypes.c_uint64),
        ("internal_peak", ctypes.c_uint64),
        ("external", ctypes.c_uint64),
        ("external_peak", ctypes.c_uint64),
        ("reusable", ctypes.c_uint64),
        ("reusable_peak", ctypes.c_uint64),
        ("purgeable_volatile_pmap", ctypes.c_uint64),
        ("purgeable_volatile_resident", ctypes.c_uint64),
        ("purgeable_volatile_virtual", ctypes.c_uint64),
        ("compressed", ctypes.c_uint64),
        ("compressed_peak", ctypes.c_uint64),
        ("compressed_lifetime", ctypes.c_uint64),
        ("phys_footprint", ctypes.c_uint64),
        ("min_address", ctypes.c_uint64),
        ("max_address", ctypes.c_uint64),
    ]


_TASK_VM_INFO_COUNT = ctypes.sizeof(_TaskVmInfo) // ctypes.sizeof(ctypes.c_int)

_LIBSYSTEM.task_for_pid.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
_LIBSYSTEM.task_for_pid.restype = ctypes.c_int
_LIBSYSTEM.task_info.argtypes = [
    ctypes.c_uint,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint),
]
_LIBSYSTEM.task_info.restype = ctypes.c_int
_LIBSYSTEM.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
_LIBSYSTEM.mach_port_deallocate.restype = ctypes.c_int
_LIBSYSTEM.sysctlbyname.argtypes = [
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.c_void_p,
    ctypes.c_size_t,
]
_LIBSYSTEM.sysctlbyname.restype = ctypes.c_int

# proc_pid_rusage works for any process the user owns — no entitlements
# required. This is what footprint(1) uses internally.
_LIBSYSTEM.proc_pid_rusage.argtypes = [
    ctypes.c_int,         # pid
    ctypes.c_int,         # flavor (RUSAGE_INFO_V6)
    ctypes.c_void_p,      # buffer (rusage_info_t)
]
_LIBSYSTEM.proc_pid_rusage.restype = ctypes.c_int


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


def memory_pressure() -> str:
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

    result = level
    _MEMORY_PRESSURE_CACHE = (now, result)
    return result


def process_phys_footprint(pid: int) -> int | None:
    """Return the per-process physical footprint in bytes — the same number
    Activity Monitor's "Memory" column shows. Uses `proc_pid_rusage` with
    RUSAGE_INFO_V6, which works for any process owned by the current user
    (no entitlements required, unlike Mach `task_for_pid`).

    Returns None if the call fails (process gone, denied, etc.) so callers
    can fall back to whatever they like."""
    info = _RUsageInfoV6()
    rc = _LIBSYSTEM.proc_pid_rusage(pid, _RUSAGE_INFO_V6, ctypes.byref(info))
    if rc != 0:
        return None
    footprint = int(info.ri_phys_footprint)
    if footprint == 0:
        return None  # let caller fall back rather than show "0 B"
    return footprint


def perf_levels() -> tuple[int, int] | None:
    global _PERF_LEVEL_CACHE

    now = time.monotonic()
    if _PERF_LEVEL_CACHE and now - _PERF_LEVEL_CACHE[0] < 60.0:
        return _PERF_LEVEL_CACHE[1]

    perf = _sysctl_uint("hw.perflevel0.physicalcpu")
    eff = _sysctl_uint("hw.perflevel1.physicalcpu")
    result = None
    if perf is not None and eff is not None and perf >= 0 and eff >= 0:
        result = (perf, eff)

    _PERF_LEVEL_CACHE = (now, result)
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
        return value * 1024
    if suffix == "M":
        return value * 1024 * 1024
    if suffix == "B":
        return value * 1024 * 1024 * 1024
    return value


def _sysctl_uint(name: str) -> int | None:
    value = ctypes.c_uint()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    result = _LIBSYSTEM.sysctlbyname(
        name.encode("utf-8"),
        ctypes.byref(value),
        ctypes.byref(size),
        None,
        0,
    )
    if result != 0:
        return None
    return int(value.value)
