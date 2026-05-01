from __future__ import annotations

"""Per-process memory matches Activity Monitor's 'Memory' column.
System-wide accounting matches Activity Monitor's Memory tab footer."""

import asyncio
import json
import os
import plistlib
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import psutil

from minomon.actions._common import MONITOR_PID_FILE, ensure_state_dirs, list_calmed_sentinels, list_paused_sentinels, prune_invalid_calmed_sentinels
from minomon.actions.freeze import resume_orphaned_paused_processes
from .insights import build_insights
from .macos import lsappinfo_front, memory_pressure, perf_levels, process_phys_footprint, running_apps, vm_stat
from .pinned import AUDIO_BUNDLE_IDS, SOCKET_HEAVY_BUNDLE_IDS, add_terminal_app, is_pinned
from .sample import CassieStatus, CPUSample, GPUSample, MemorySample, ProcessRow, Sample


_CASSIE_PATH = Path.home() / ".cassie" / "status.json"
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


@dataclass
class _ProcessMeta:
    name: str
    bundle_id: Optional[str]
    start_unix: int


class Sampler:
    def __init__(self, top_n: int = 30, history_seconds: int = 60):
        self.top_n = top_n
        self.history_seconds = history_seconds
        self.history: deque[Sample] = deque(maxlen=history_seconds)
        self._subscribers: list[Callable[[Sample], Awaitable[None] | None]] = []
        self._task: asyncio.Task | None = None
        self._process_meta: dict[int, _ProcessMeta] = {}
        self._last_active: dict[int, float] = {}
        self._previous_vm_stats: dict[str, int] | None = None
        self._previous_vm_time: float | None = None
        self._powermetrics = _PowermetricsReader()
        self._orphan_resumed_count = 0

    @property
    def latest(self) -> Sample | None:
        return self.history[-1] if self.history else None

    def subscribe(self, callback: Callable[[Sample], Awaitable[None] | None]) -> None:
        self._subscribers.append(callback)

    async def start(self) -> None:
        if self._task:
            return
        ensure_state_dirs()
        add_terminal_app()
        MONITOR_PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self._orphan_resumed_count = await resume_orphaned_paused_processes()
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        await self._powermetrics.start()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        await self._powermetrics.stop()

    async def _loop(self) -> None:
        while True:
            sample = self._make_sample()
            self.history.append(sample)
            for callback in self._subscribers:
                result = callback(sample)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(1.0)

    def _make_sample(self) -> Sample:
        timestamp = time.time()
        memory = self._sample_memory(timestamp)
        cpu = self._sample_cpu()
        gpu = self._powermetrics.sample
        cassie = self._sample_cassie(timestamp)
        prune_invalid_calmed_sentinels()
        paused_pids = [pid for pid, _, _ in list_paused_sentinels()]
        calmed_pids = [pid for pid, _, _ in list_calmed_sentinels()]
        processes = self._sample_processes(timestamp, paused_pids, calmed_pids)
        insights = build_insights(memory, processes, cassie, self._orphan_resumed_count)
        self._orphan_resumed_count = 0
        return Sample(
            timestamp=timestamp,
            memory=memory,
            cpu=cpu,
            gpu=gpu,
            processes=processes,
            cassie=cassie,
            insights=insights,
            paused_pids=paused_pids,
            calmed_pids=calmed_pids,
        )

    def _sample_memory(self, timestamp: float) -> MemorySample:
        stats = vm_stat()
        total_bytes = psutil.virtual_memory().total
        total_pages = total_bytes // _PAGE_SIZE

        # Match Activity Monitor's Memory tab footer exactly:
        #   App Memory       = anonymous pages (private memory used by apps)
        #   Wired Memory     = pages wired down (kernel-locked)
        #   Compressed       = pages OCCUPIED by compressor (actual RAM in use), NOT
        #                      pages STORED in compressor (uncompressed equivalent).
        #                      `top` reports this same number as the "compressor" figure.
        #   Cached Files     = file-backed + speculative + purgeable
        #   Free             = pages free (do NOT subtract speculative — it's a
        #                      separate category that vm_stat already breaks out)
        speculative_pages = stats.get("specul", 0)
        free_pages = max(0, stats.get("free", 0))
        wired_pages = stats.get("wired", 0)
        # `cmprssor` (occupied) is what AM/top show; `cmprssed` is original size.
        compressed_pages = stats.get("cmprssor", stats.get("cmprssed", 0))
        cached_pages = stats.get("file-backed", 0) + speculative_pages + stats.get("prgable", 0)
        app_pages = stats.get("anonymous", 0)

        total_gb = total_bytes / (1024 ** 3)
        free_gb = _pages_to_gb(free_pages)
        wired_gb = _pages_to_gb(wired_pages)
        compressed_gb = _pages_to_gb(compressed_pages)
        cached_gb = _pages_to_gb(cached_pages)
        app_gb = _pages_to_gb(app_pages)

        swap_in_rate = 0.0
        swap_out_rate = 0.0
        compress_rate_mbps = 0.0
        if self._previous_vm_stats is not None and self._previous_vm_time is not None:
            dt = max(0.001, timestamp - self._previous_vm_time)
            swap_in_delta = max(0, stats.get("swapins", 0) - self._previous_vm_stats.get("swapins", 0))
            swap_out_delta = max(0, stats.get("swapouts", 0) - self._previous_vm_stats.get("swapouts", 0))
            swap_in_rate = swap_in_delta / (1024 ** 2) / dt
            swap_out_rate = swap_out_delta / (1024 ** 2) / dt
            # Rate at which the kernel is actively COMPRESSING memory right now.
            # `comprs` is the lifetime page-compression counter (see vm_stat).
            comprs_delta = max(0, stats.get("comprs", 0) - self._previous_vm_stats.get("comprs", 0))
            compress_rate_mbps = (comprs_delta * _PAGE_SIZE) / (1024 ** 2) / dt

        self._previous_vm_stats = stats
        self._previous_vm_time = timestamp

        # Derive Activity-Monitor-style pressure level from real activity,
        # NOT from memory_pressure(1)'s "free percentage" (which is unrelated to
        # AM's pressure graph). Apple's algorithm hinges on whether the system
        # is actively reclaiming via compression and swap.
        pressure_level = _derive_pressure_level(compress_rate_mbps, swap_out_rate)

        used_pages = app_pages + wired_pages + compressed_pages
        pressure_pct = max(0, min(100, round((used_pages / total_pages) * 100))) if total_pages else 0
        return MemorySample(
            total_gb=round(total_gb, 2),
            app_gb=round(app_gb, 2),
            wired_gb=round(wired_gb, 2),
            compressed_gb=round(compressed_gb, 2),
            cached_gb=round(cached_gb, 2),
            free_gb=round(free_gb, 2),
            swap_in_rate_mbps=round(swap_in_rate, 2),
            swap_out_rate_mbps=round(swap_out_rate, 2),
            pressure_level=pressure_level,
            pressure_pct=pressure_pct,
        )

    def _sample_cpu(self) -> CPUSample:
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        total_pct = psutil.cpu_percent(interval=None)
        perf_cores, eff_cores = _split_cores(per_core)
        perf_pct = sum(perf_cores) / len(perf_cores) if perf_cores else total_pct
        eff_pct = sum(eff_cores) / len(eff_cores) if eff_cores else 0.0
        return CPUSample(
            total_pct=round(total_pct, 1),
            perf_pct=round(perf_pct, 1),
            eff_pct=round(eff_pct, 1),
            load_avg_1=round(os.getloadavg()[0], 2),
        )

    def _sample_processes(self, now: float, paused_pids: list[int], calmed_pids: list[int]) -> list[ProcessRow]:
        front_bundle = lsappinfo_front()
        app_bundles = running_apps()
        paused_set = set(paused_pids)
        calmed_set = set(calmed_pids)

        rows: list[ProcessRow] = []
        processes = []
        try:
            process_iter = psutil.process_iter(["pid", "name", "create_time"])
        except (psutil.Error, OSError, PermissionError):
            return rows

        try:
            for process in process_iter:
                try:
                    footprint = _process_memory_bytes(process)
                    processes.append((footprint, process))
                except (psutil.Error, OSError, PermissionError):
                    continue
        except (psutil.Error, OSError, PermissionError):
            return rows
        processes.sort(key=lambda item: item[0], reverse=True)

        current_pids = set()
        for footprint, process in processes[: self.top_n]:
            try:
                pid = process.pid
                current_pids.add(pid)
                meta = self._meta_for_process(process)
                cpu_pct = process.cpu_percent(interval=None)
                bundle_id = meta.bundle_id
                pinned = is_pinned(meta.name, bundle_id)
                holds_audio = bool(bundle_id and bundle_id in AUDIO_BUNDLE_IDS)
                holds_socket = bool(bundle_id and bundle_id in SOCKET_HEAVY_BUNDLE_IDS)

                if cpu_pct > 1.0:
                    self._last_active[pid] = now
                last_active = self._last_active.get(pid, now)
                seconds_idle = max(0, int(now - last_active))

                if pinned:
                    state = "active"
                elif pid in calmed_set:
                    state = "calmed"
                elif pid in paused_set:
                    state = "paused"
                elif bundle_id and bundle_id == front_bundle:
                    state = "active"
                elif holds_audio:
                    state = "playing"
                elif bundle_id and bundle_id in app_bundles and (cpu_pct > 1.0 or seconds_idle < 120):
                    state = "foreground"
                else:
                    state = _format_idle(seconds_idle)

                rows.append(
                    ProcessRow(
                        pid=pid,
                        start_unix=meta.start_unix,
                        name=meta.name,
                        rss_gb=round(footprint / (1024 ** 3), 2),
                        cpu_pct=round(cpu_pct, 1),
                        state=state,
                        pinned=pinned,
                        bundle_id=bundle_id,
                        holds_audio=holds_audio,
                        holds_socket=holds_socket,
                        seconds_idle=seconds_idle,
                    )
                )
            except (psutil.Error, OSError):
                continue

        self._process_meta = {pid: meta for pid, meta in self._process_meta.items() if pid in current_pids}
        self._last_active = {pid: ts for pid, ts in self._last_active.items() if pid in current_pids}
        return rows

    def _meta_for_process(self, process: psutil.Process) -> _ProcessMeta:
        cached = self._process_meta.get(process.pid)
        start_unix = int(process.create_time())
        if cached and cached.start_unix == start_unix:
            return cached

        bundle_id = _bundle_id_for_process(process)
        name = _human_name(process, bundle_id)
        meta = _ProcessMeta(name=name, bundle_id=bundle_id, start_unix=start_unix)
        self._process_meta[process.pid] = meta
        return meta

    def _sample_cassie(self, now: float) -> CassieStatus:
        if not _CASSIE_PATH.exists():
            return CassieStatus(available=False)

        try:
            stat = _CASSIE_PATH.stat()
            if now - stat.st_mtime > 10:
                return CassieStatus(available=False)
            payload = json.loads(_CASSIE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return CassieStatus(available=False)

        last_request_unix = int(payload.get("last_request_unix", 0) or 0)
        seconds_idle = max(0, int(now - last_request_unix)) if last_request_unix else 0
        return CassieStatus(
            available=True,
            fast_loaded=bool(payload.get("fast_loaded", False)),
            deep_loaded=bool(payload.get("deep_loaded", False)),
            fast_resident_gb=float(payload.get("fast_resident_gb", 0.0) or 0.0),
            deep_resident_gb=float(payload.get("deep_resident_gb", 0.0) or 0.0),
            in_flight=bool(payload.get("in_flight", False)),
            tts_in_flight=bool(payload.get("tts_in_flight", False)),
            last_request_unix=last_request_unix,
            seconds_idle=seconds_idle,
        )


class _PowermetricsReader:
    def __init__(self) -> None:
        self.sample = GPUSample(
            gpu_pct=0.0,
            ane_pct=0.0,
            soc_temp_c=0.0,
            fan_rpm=0,
            powermetrics_available=False,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._failed = False

    async def start(self) -> None:
        if self._failed or self._task:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                "powermetrics",
                "--samplers",
                "gpu_power,ane_power,thermal,smc",
                "-i",
                "1000",
                "-f",
                "plist",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception:
            self._failed = True
            return
        self.sample = GPUSample(0.0, 0.0, 0.0, 0, True)
        self._task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._process and self._process.returncode is None:
            self._process.terminate()
            await self._process.wait()
        self._process = None

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        buffer = b""
        while True:
            chunk = await self._process.stdout.read(4096)
            if not chunk:
                self._failed = True
                self.sample = GPUSample(0.0, 0.0, 0.0, 0, False)
                return
            buffer += chunk
            while b"</plist>" in buffer:
                raw, buffer = buffer.split(b"</plist>", 1)
                document = raw + b"</plist>"
                try:
                    payload = plistlib.loads(document)
                except Exception:
                    continue
                self.sample = _parse_powermetrics_payload(payload)


def _pages_to_gb(pages: int) -> float:
    return pages * _PAGE_SIZE / (1024 ** 3)


def _derive_pressure_level(compress_rate_mbps: float, swap_out_rate_mbps: float) -> str:
    """Activity-Monitor-style pressure level, derived from real activity.

    Apple's pressure indicator is driven by whether the kernel is actively
    reclaiming memory — compressing pages and swapping out — not by a static
    used/total ratio. macOS routinely sits at 80%+ committed without any
    pressure because compression and file-cache eviction handle bursts
    cheaply. Pressure only registers when those mechanisms are saturating.

    Heuristic thresholds (calibrated for an idle vs working M1 Max):
      - CRITICAL  swap is moving > 5 MB/s out (system is paging to disk)
      - WARN      compressor running > 50 MB/s OR any sustained swap
      - NORMAL    otherwise — compressor and swap are quiet, system is calm
    """
    if swap_out_rate_mbps > 5.0:
        return "CRITICAL"
    if swap_out_rate_mbps > 0.5 or compress_rate_mbps > 50.0:
        return "WARN"
    return "NORMAL"


def _split_cores(per_core: list[float]) -> tuple[list[float], list[float]]:
    count = len(per_core)
    if count <= 4:
        return per_core, []
    levels = perf_levels()
    if levels is not None:
        perf_count, eff_count = levels
        if perf_count + eff_count == count:
            # Apple Silicon reports efficiency cores first in the per-cpu arrays
            # exposed through psutil/host_processor_info.
            eff_cores = per_core[:eff_count]
            perf_cores = per_core[eff_count:]
            return perf_cores, eff_cores

    eff_count = min(4, count // 3)
    perf_count = max(1, count - eff_count)
    return per_core[:perf_count], per_core[perf_count:]


def _process_memory_bytes(process: psutil.Process) -> int:
    footprint = process_phys_footprint(process.pid)
    if footprint is not None:
        return footprint

    try:
        full_info = process.memory_full_info()
    except (psutil.Error, OSError):
        full_info = None
    if full_info is not None and hasattr(full_info, "uss"):
        return int(full_info.uss)

    try:
        info = process.memory_info()
    except (psutil.Error, OSError):
        return 0
    return int(info.rss)


def _format_idle(seconds_idle: int) -> str:
    if seconds_idle >= 3600:
        hours = max(1, round(seconds_idle / 3600))
        return f"idle {hours}h"
    minutes = max(1, round(seconds_idle / 60))
    return f"idle {minutes}m"


def _bundle_display_name(process: psutil.Process) -> Optional[str]:
    """Read CFBundleDisplayName / CFBundleName from a .app bundle's Info.plist.
    This is what Activity Monitor and the Dock show — much friendlier than
    psutil.name() which returns 'Python', 'Helper (Renderer)', or version strings."""
    try:
        exe_path = Path(process.exe()).resolve()
    except (psutil.Error, OSError):
        return None
    bundle_root: Optional[Path] = None
    for parent in (exe_path,) + tuple(exe_path.parents):
        if parent.suffix == ".app":
            bundle_root = parent
            break
    if not bundle_root:
        return None
    info_plist = bundle_root / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as handle:
            payload = plistlib.load(handle)
    except Exception:
        return None
    for key in ("CFBundleDisplayName", "CFBundleName"):
        v = payload.get(key)
        if v:
            return str(v)
    # Fall back to the .app dir's stem
    return bundle_root.stem


# Bundle ids whose CFBundleName is generic (the language name) and whose
# real identity comes from the script in argv. Bypass the bundle lookup for
# these and always derive the name from the cmdline.
_INTERPRETER_BUNDLE_PREFIXES = (
    "org.python.",
    "org.nodejs.",
    "ruby-lang.org.",
    "com.oracle.java.",
)


def _script_from_cmdline(process: psutil.Process) -> Optional[str]:
    """Pick the most informative argv entry for an interpreter process —
    the first non-flag argument that looks like a script path."""
    try:
        cmd = process.cmdline() or []
    except (psutil.Error, OSError):
        return None
    for arg in cmd[1:]:
        if arg.startswith("-"):
            continue
        base = os.path.basename(arg)
        if base and ("." in base or "/" in arg):
            return base
    return None


def _human_name(process: psutil.Process, bundle_id: Optional[str]) -> str:
    """Best-effort human-readable process name.

    Resolution order:
      1. If process is a generic interpreter (Python, Node, etc.), prefer the
         script name from its cmdline. Otherwise an mlx-lm chat server just
         reads as 'Python'.
      2. App bundle's CFBundleDisplayName / CFBundleName (Activity Monitor's value)
      3. psutil.Process.name() — final fallback
    """
    is_interpreter = bool(bundle_id) and any(
        bundle_id.startswith(p) for p in _INTERPRETER_BUNDLE_PREFIXES
    )
    try:
        raw_name = process.name() or ""
    except (psutil.Error, OSError):
        raw_name = ""

    if is_interpreter or raw_name.lower().startswith(("python", "node", "ruby", "perl", "java")):
        script = _script_from_cmdline(process)
        if script:
            interp = raw_name or (bundle_id.split(".")[-1] if bundle_id else "interpreter")
            return f"{interp} · {script}"

    bundle_name = _bundle_display_name(process)
    if bundle_name and not _looks_like_version_string(bundle_name):
        return bundle_name

    # If raw_name is a version string (some CLIs install per-version dirs and
    # the executable filename is the version), use cmdline[0] basename instead.
    if raw_name and _looks_like_version_string(raw_name):
        try:
            cmd = process.cmdline() or []
        except (psutil.Error, OSError):
            cmd = []
        if cmd:
            base = os.path.basename(cmd[0])
            if base and not _looks_like_version_string(base):
                return base
        return bundle_id or raw_name or f"pid {process.pid}"

    return raw_name or (bundle_id or f"pid {process.pid}")


_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}([a-z0-9.-]*)?$")


def _looks_like_version_string(name: str) -> bool:
    """Some bundles' CFBundleName is literally the version (e.g. Slack's
    helpers come back as '2.1.116'). When that happens, fall through to a
    different field rather than show the version to the user."""
    return bool(_VERSION_RE.match(name.strip()))


def _bundle_id_for_process(process: psutil.Process) -> Optional[str]:
    try:
        exe_path = Path(process.exe()).resolve()
    except (psutil.Error, OSError):
        return None

    bundle_root: Optional[Path] = None
    for parent in (exe_path,) + tuple(exe_path.parents):
        if parent.suffix == ".app":
            bundle_root = parent
            break
    if not bundle_root:
        return None

    info_plist = bundle_root / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as handle:
            payload = plistlib.load(handle)
    except Exception:
        return None

    bundle_id = payload.get("CFBundleIdentifier")
    return str(bundle_id) if bundle_id else None


def _parse_powermetrics_payload(payload: object) -> GPUSample:
    gpu_pct = _pick_numeric(payload, ["gpu", "gpu active", "gpu_busy"])
    ane_pct = _pick_numeric(payload, ["ane", "ane active", "ane_busy"])
    temp_c = _pick_numeric(payload, ["soc die temperature", "soc temperature", "temperature"])
    fan_rpm = int(_pick_numeric(payload, ["fan", "rpm"]))
    available = True
    return GPUSample(
        gpu_pct=round(gpu_pct, 1),
        ane_pct=round(ane_pct, 1),
        soc_temp_c=round(temp_c, 1),
        fan_rpm=fan_rpm,
        powermetrics_available=available,
    )


def _pick_numeric(payload: object, needles: list[str]) -> float:
    matches: list[float] = []
    lowered_needles = [needle.lower() for needle in needles]

    def visit(node: object, parent_key: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key).lower()
                combined = f"{parent_key} {key_text}".strip()
                if any(needle in combined for needle in lowered_needles):
                    number = _coerce_number(value)
                    if number is not None:
                        matches.append(number)
                visit(value, combined)
        elif isinstance(node, list):
            for item in node:
                visit(item, parent_key)

    visit(payload)
    if not matches:
        return 0.0

    normalized = [min(100.0, value) for value in matches if value >= 0]
    return normalized[0] if normalized else 0.0


def _coerce_number(value: object) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit() or ch in ".-")
        if not digits or digits in {"-", ".", "-."}:
            return None
        try:
            return float(digits)
        except ValueError:
            return None
    return None
