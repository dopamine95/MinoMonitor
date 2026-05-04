"""
Stub sampler that returns plausible fake data. Used while the real sampler
(Codex) is being built so the UI can develop in parallel. Will be replaced
by the real Sampler in minomon/data/sampler.py.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections import deque
from typing import Awaitable, Callable

from .sample import (
    BatterySample, CassieStatus, CPUSample, GPUSample, Insight, MemorySample,
    ProcessRow, Sample,
)


_FAKE_PROCS = [
    # name, rss_gb, cpu_pct, pinned, bundle, state
    ("python3 (cassie_server)", 15.4, 28.0, True,  "com.threetrees.cassie",   "active"),
    ("Google Chrome",            4.2,  8.0, False, "com.google.Chrome",       "foreground"),
    ("Xcode",                    3.1,  1.0, True,  "com.apple.dx.Xcode",      "idle 23m"),
    ("Slack",                    1.8,  0.2, False, "com.tinyspeck.slackmac",  "idle 47m"),
    ("Discord",                  1.6,  0.0, False, "com.hnc.Discord",         "idle 2h"),
    ("Spotify",                  0.9,  3.0, False, "com.spotify.client",      "playing"),
    ("Terminal",                 0.4,  0.5, True,  "com.apple.Terminal",      "active"),
    ("Notion",                   1.1,  0.0, False, "notion.id",               "idle 1h"),
    ("Figma",                    2.3,  0.0, False, "com.figma.Desktop",       "idle 3h"),
    ("Mail",                     0.6,  0.0, False, "com.apple.mail",          "foreground"),
]


class StubSampler:
    """Same interface as the real Sampler — produces oscillating fake data."""

    def __init__(self, top_n: int = 30, history_seconds: int = 60):
        self.top_n = top_n
        self.history_seconds = history_seconds
        self.history: deque[Sample] = deque(maxlen=history_seconds)
        self._subscribers: list[Callable[[Sample], Awaitable[None] | None]] = []
        self._task: asyncio.Task | None = None
        self._t0 = time.time()

    @property
    def latest(self) -> Sample | None:
        return self.history[-1] if self.history else None

    def subscribe(self, callback: Callable[[Sample], Awaitable[None] | None]) -> None:
        self._subscribers.append(callback)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            sample = self._make_sample()
            self.history.append(sample)
            for cb in self._subscribers:
                result = cb(sample)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(1.0)

    def _make_sample(self) -> Sample:
        t = time.time() - self._t0
        # Make the meters breathe so the UI looks alive
        ram_pct = 60 + 20 * math.sin(t / 8)
        ram_pct += random.uniform(-2, 2)
        ram_pct = max(40, min(95, ram_pct))

        total = 64.0
        app = total * (ram_pct / 100) * 0.55
        wired = 3.9 + random.uniform(-0.1, 0.1)
        compressed = total * (ram_pct / 100) * 0.18
        cached = total * 0.20
        free = max(0.2, total - app - wired - compressed - cached)

        if ram_pct < 70:
            pressure = ("NORMAL", int(ram_pct))
        elif ram_pct < 85:
            pressure = ("WARN", int(ram_pct))
        else:
            pressure = ("CRITICAL", int(ram_pct))

        memory = MemorySample(
            total_gb=total, app_gb=app, wired_gb=wired,
            compressed_gb=compressed, cached_gb=cached, free_gb=free,
            swap_in_rate_mbps=max(0, 4 * math.sin(t / 5)),
            swap_out_rate_mbps=max(0, 1.5 * math.sin(t / 5 + 0.5)),
            pressure_level=pressure[0], pressure_pct=pressure[1],
        )

        cpu_total = 18 + 12 * math.sin(t / 6) + random.uniform(-3, 3)
        cpu = CPUSample(
            total_pct=max(2, cpu_total),
            perf_pct=max(2, cpu_total * 0.7),
            eff_pct=max(1, cpu_total * 0.3),
            load_avg_1=2.5 + math.sin(t / 10),
        )

        gpu = GPUSample(
            gpu_pct=52 + 20 * math.sin(t / 4),
            ane_pct=3 + 5 * max(0, math.sin(t / 9)),
            soc_temp_c=62 + 4 * math.sin(t / 12),
            fan_rpm=int(2300 + 400 * math.sin(t / 12)),
            powermetrics_available=True,
        )

        cassie = CassieStatus(
            available=True,
            fast_loaded=True,
            deep_loaded=True,
            fast_resident_gb=14.2,
            deep_resident_gb=12.8,
            in_flight=(int(t) % 20) < 4,
            tts_in_flight=False,
            last_request_unix=int(time.time() - 60),
            seconds_idle=60,
        )

        processes = []
        for i, (name, rss, cpu_p, pinned, bundle, state) in enumerate(_FAKE_PROCS):
            jitter = 1 + 0.05 * math.sin(t / 7 + i)
            processes.append(ProcessRow(
                pid=10000 + i,
                start_unix=int(self._t0),
                name=name,
                rss_gb=rss * jitter,
                cpu_pct=max(0, cpu_p + random.uniform(-1, 1)),
                state=state,
                pinned=pinned,
                bundle_id=bundle,
                holds_audio=(name == "Spotify"),
                holds_socket=(name in ("Slack", "Discord")),
                seconds_idle={"idle 23m": 23 * 60, "idle 47m": 47 * 60,
                              "idle 2h": 7200, "idle 1h": 3600,
                              "idle 3h": 10800}.get(state, 0),
            ))

        # Demo insights so the UI has something to render
        insights: list[Insight] = []
        if pressure[0] != "NORMAL":
            insights.append(Insight(
                severity="warn" if pressure[0] == "WARN" else "critical",
                message=f"Memory pressure {pressure[0]}. Compressor moving "
                        f"{compressed:.1f} GB. Calming idle apps may reduce churn (not free RAM).",
                actions=[
                    ("Calm Slack", {"action": "calm", "pid": 10003, "start_unix": int(self._t0)}),
                    ("Calm Discord", {"action": "calm", "pid": 10004, "start_unix": int(self._t0)}),
                ],
            ))
        if cassie.deep_loaded:
            insights.append(Insight(
                severity="info",
                message=f"Cassie deep model loaded · idle {cassie.seconds_idle // 60} min. "
                        "Setting CASSIE_DISABLE_DEEP=1 next restart frees ~13 GB.",
            ))
        insights.append(Insight(
            severity="ok",
            message="No SIGSTOP'd processes orphaned from prior runs.",
        ))

        battery = BatterySample(
            available=True,
            percent=72 + 10 * math.sin(t / 30),
            plugged_in=(int(t) % 60) > 30,
            seconds_remaining=int(2 * 3600 + 30 * 60),
        )

        return Sample(
            timestamp=time.time(),
            memory=memory, cpu=cpu, gpu=gpu,
            processes=processes,
            cassie=cassie,
            battery=battery,
            insights=insights,
            paused_pids=[],
            calmed_pids=[],
        )
