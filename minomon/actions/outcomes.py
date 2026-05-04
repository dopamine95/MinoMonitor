"""
Action outcome feedback.

After a calm/freeze/quit action fires, we want to know whether the
intervention actually helped. Did pressure tier drop? Did swap-out
slow down? Did compressor activity quiet? Or did nothing change —
or worse, did we make it worse?

This module computes a three-bucket verdict from a baseline snapshot
captured at action time and a follow-up snapshot taken N seconds later
(default 60). The verdict fires as a toast notification and gets
appended to actions.log so future advice (`minomon advise`) has
labeled outcomes to learn from instead of just raw action history.

Three buckets — deliberately coarse:
    helped    pressure tier dropped, OR swap-out fell by >1 MB/s,
              OR (for quit) App Memory dropped by the target's footprint
    worsened  pressure tier rose, OR swap-out rose by >1 MB/s
    neutral   nothing meaningful changed in the window

We don't try to attribute causally — the user gets an observation, not
a controlled experiment. The signal is still useful in aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..data.sample import MemorySample


_PRESSURE_TIER = {"NORMAL": 0, "WARN": 1, "CRITICAL": 2}

# Thresholds. Calibrated for a 64 GB Mac with a real workload — small
# enough that genuine improvements register, big enough that random
# tick-to-tick wobble doesn't fire a verdict.
_SWAP_DELTA_THRESHOLD_MBPS = 1.0
_QUIT_FOOTPRINT_DROP_FRACTION = 0.5  # quit "helped" if at least half the
                                      # target's previous RAM is gone.


Verdict = Literal["helped", "neutral", "worsened"]


@dataclass(frozen=True)
class OutcomeBaseline:
    """Snapshot captured at action-time. Compared against the follow-up
    sample later. Lightweight enough to copy by value into asyncio
    futures without worrying about lifecycle."""
    action: str                          # "calm" | "freeze" | "quit" | "calm_many"
    target_name: str                     # display name shown in the toast
    target_rss_gb: float                 # combined footprint of target(s) at t0
    memory: MemorySample
    cpu_total_pct: float


@dataclass(frozen=True)
class OutcomeVerdict:
    bucket: Verdict
    summary: str


def evaluate(baseline: OutcomeBaseline, current: MemorySample, current_cpu_total: float) -> OutcomeVerdict:
    """Compare baseline → current and return a verdict."""
    base_tier = _PRESSURE_TIER.get(baseline.memory.pressure_level, 0)
    cur_tier = _PRESSURE_TIER.get(current.pressure_level, 0)

    swap_delta = current.swap_out_rate_mbps - baseline.memory.swap_out_rate_mbps
    app_delta = current.app_gb - baseline.memory.app_gb
    cpu_delta = current_cpu_total - baseline.cpu_total_pct

    # ---- Worsened checks first — a bigger problem trumps any wins ----
    if cur_tier > base_tier:
        return OutcomeVerdict(
            "worsened",
            f"pressure rose: {baseline.memory.pressure_level} → {current.pressure_level}",
        )
    if swap_delta > _SWAP_DELTA_THRESHOLD_MBPS:
        return OutcomeVerdict(
            "worsened",
            f"swap-out rose to {current.swap_out_rate_mbps:.1f} MB/s "
            f"(was {baseline.memory.swap_out_rate_mbps:.1f})",
        )

    # ---- Helped checks ----
    if cur_tier < base_tier:
        return OutcomeVerdict(
            "helped",
            f"pressure dropped: {baseline.memory.pressure_level} → {current.pressure_level}",
        )
    if swap_delta < -_SWAP_DELTA_THRESHOLD_MBPS:
        return OutcomeVerdict(
            "helped",
            f"swap-out fell from {baseline.memory.swap_out_rate_mbps:.1f} "
            f"to {current.swap_out_rate_mbps:.1f} MB/s",
        )
    # Quit-specific: did App Memory actually drop close to the target's footprint?
    if baseline.action == "quit" and baseline.target_rss_gb > 0.5:
        # app_delta is current - baseline. A drop of -X means app shrank by X.
        wanted_drop = baseline.target_rss_gb * _QUIT_FOOTPRINT_DROP_FRACTION
        if -app_delta >= wanted_drop:
            return OutcomeVerdict(
                "helped",
                f"App Memory dropped {-app_delta:.1f} GB "
                f"({baseline.target_rss_gb:.1f} GB target)",
            )

    # ---- Otherwise: neutral. Add a small CPU note when relevant so the
    # user learns the secondary effect of calm/freeze (CPU) when memory
    # didn't move. ----
    if baseline.action in ("calm", "freeze", "calm_many") and cpu_delta < -5.0:
        return OutcomeVerdict(
            "neutral",
            f"memory unchanged but CPU dropped {-cpu_delta:.0f} pp "
            f"({baseline.cpu_total_pct:.0f}% → {current_cpu_total:.0f}%)",
        )

    return OutcomeVerdict("neutral", "no meaningful change in the window")
