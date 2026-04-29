"""
Snapshot dataclasses produced by the Sampler and consumed by the UI.

This file is the contract between the data layer (Codex) and the UI layer
(Claude). Both sides depend only on these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MemorySample:
    """Apple-Silicon-honest memory accounting (gigabytes unless noted)."""
    total_gb: float
    app_gb: float           # active + inactive app memory (resident, uncompressed)
    wired_gb: float         # kernel-locked, never compressible
    compressed_gb: float    # bytes the compressor has shrunk
    cached_gb: float        # file-backed cache (purgeable under pressure)
    free_gb: float
    swap_in_rate_mbps: float
    swap_out_rate_mbps: float
    pressure_level: str     # "NORMAL" | "WARN" | "CRITICAL"
    pressure_pct: int       # 0-100, kernel-derived utilization estimate


@dataclass
class CPUSample:
    total_pct: float
    perf_pct: float         # P-cores
    eff_pct: float          # E-cores
    load_avg_1: float


@dataclass
class GPUSample:
    """Apple Silicon SoC telemetry. Requires powermetrics (sudo). If
    powermetrics_available is False, all numbers are 0 and the UI shows
    a 'not enabled' message."""
    gpu_pct: float
    ane_pct: float
    soc_temp_c: float
    fan_rpm: int
    powermetrics_available: bool


@dataclass
class ProcessRow:
    pid: int
    start_unix: int           # used as identity guard; PID alone is reused
    name: str                 # human-friendly app/proc name
    rss_gb: float
    cpu_pct: float
    state: str                # "active" | "foreground" | "idle Xm" | "playing" | "paused" | "calmed"
    pinned: bool              # in deny list — UI greys out actions
    bundle_id: Optional[str] = None
    holds_audio: bool = False   # heuristic: in audio_clients set
    holds_socket: bool = False  # heuristic: known WebSocket-heavy app
    seconds_idle: int = 0


@dataclass
class CassieStatus:
    """Read from ~/.cassie/status.json. available=False when file missing."""
    available: bool
    fast_loaded: bool = False
    deep_loaded: bool = False
    fast_resident_gb: float = 0.0
    deep_resident_gb: float = 0.0
    in_flight: bool = False
    tts_in_flight: bool = False
    last_request_unix: int = 0
    seconds_idle: int = 0


@dataclass
class Insight:
    """A deterministic, rules-engine-produced suggestion or status note."""
    severity: str            # "info" | "warn" | "critical" | "ok"
    message: str
    actions: list[tuple[str, dict]] = field(default_factory=list)
    # actions are [(label, payload)] where payload describes the action,
    # e.g. ("Calm Slack", {"action": "calm", "pid": 9134, "start_unix": 17304...})


@dataclass
class Sample:
    timestamp: float
    memory: MemorySample
    cpu: CPUSample
    gpu: GPUSample
    processes: list[ProcessRow]
    cassie: CassieStatus
    insights: list[Insight]
    paused_pids: list[int] = field(default_factory=list)   # currently STOP'd by us
    calmed_pids: list[int] = field(default_factory=list)   # currently taskpolicy -b'd by us


@dataclass
class ActionResult:
    """Returned by every action in minomon.actions.*"""
    success: bool
    message: str
    pid: int
    action: str   # "calm" | "uncalm" | "freeze" | "thaw" | "quit"
