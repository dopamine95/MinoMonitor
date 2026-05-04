"""
Insight engine. Pure rules, no LLM. Reads the current Sample and the
per-process growth deltas the sampler computes, returns a list of
Insight objects the UI renders in the bottom panel.

Two classes of insight:

1. **State-based** — driven by the current snapshot (pressure level,
   swap rate, Cassie idle time, orphan resumes from startup). One per
   condition; no cooldown needed because the conditions are sticky.

2. **Anomaly-based** — driven by changes over time (sudden surge, slow
   leak). Each (pid, kind) gets a 90-second cooldown so we don't spam
   the panel with the same finding every tick.
"""

from __future__ import annotations

import time
from typing import Optional

from .sample import CassieStatus, Insight, MemorySample, ProcessRow


# Per-(pid, kind) cooldown so the same anomaly doesn't fire every tick
# while it's still rolling through the 5-min / 15-min windows. 90s gives
# the user a chance to read it; longer than that and a real second
# anomaly might be missed.
_ANOMALY_COOLDOWN_SECONDS = 90.0
_recent_anomalies: dict[tuple[int, str], float] = {}

# Cap how many growth anomalies we surface in one tick so the panel
# stays scannable. If many things are growing at once the user is
# already in a memory event — surface the worst three.
_MAX_ANOMALIES_PER_TICK = 3

# Thresholds — tuned for a 64 GB Mac running an LLM workload. May need
# downward adjustment for smaller machines (TODO: scale by total RAM).
_SURGE_1M_GB        = 1.0   # warn-level
_SURGE_1M_CRIT_GB   = 3.0   # critical-level
_SURGE_5M_GB        = 2.0   # warn-level
_LEAK_15M_GB        = 1.5   # combined-window leak shape
_LEAK_5M_GB         = 0.5
_LEAK_1M_GB         = 0.05


def _detect_growth_anomaly(now: float, row: ProcessRow) -> Optional[Insight]:
    """Return an Insight if `row` has anomalous recent growth, else None.

    Three patterns are detected, in priority order:
    - Sudden surge (≥1 GB in 1 min): caught immediately
    - 5-minute surge (≥2 GB): often a model load or browser-tab burst
    - Slow leak (positive growth across all three windows + ≥1.5 GB
      over 15 min): the kind of thing you don't notice until it hurts
    """
    if row.pinned:
        # User has explicitly trusted this process; don't nag.
        return None

    d1 = row.delta_1m_gb
    d5 = row.delta_5m_gb
    d15 = row.delta_15m_gb

    kind: Optional[str] = None
    severity = "warn"
    detail = ""

    if d1 is not None and d1 >= _SURGE_1M_GB:
        kind = "surge_1m"
        detail = f"+{d1:.1f} GB in 1 min"
        if d1 >= _SURGE_1M_CRIT_GB:
            severity = "critical"
    elif d5 is not None and d5 >= _SURGE_5M_GB:
        kind = "surge_5m"
        detail = f"+{d5:.1f} GB in 5 min"
    elif (d15 is not None and d15 >= _LEAK_15M_GB
          and d5 is not None and d5 >= _LEAK_5M_GB
          and d1 is not None and d1 >= _LEAK_1M_GB):
        kind = "leak"
        detail = f"+{d15:.1f} GB over 15 min, still growing"

    if kind is None:
        return None

    cooldown_key = (row.pid, kind)
    last_fire = _recent_anomalies.get(cooldown_key, 0.0)
    if now - last_fire < _ANOMALY_COOLDOWN_SECONDS:
        return None
    _recent_anomalies[cooldown_key] = now

    # Calm action targets every child pid in the group so a Brave-helper
    # leak doesn't leave seven other helpers untouched.
    targets = [
        {"pid": pid, "start_unix": start, "name": row.name}
        for pid, start in (row.child_pids or [(row.pid, row.start_unix)])
    ]
    action = (
        f"Calm {row.name}",
        {"action": "calm_many", "targets": targets},
    )

    if kind == "leak":
        msg = (
            f"{row.name} looks like it might be leaking — {detail}. "
            f"Now at {row.rss_gb:.1f} GB."
        )
    else:
        msg = (
            f"{row.name} just grew {detail}. Now at {row.rss_gb:.1f} GB. "
            "Was this expected?"
        )

    return Insight(severity=severity, message=msg, actions=[action])


def _prune_anomaly_cooldowns(now: float, current_pids: set[int]) -> None:
    """Drop cooldown entries for PIDs that aren't in the current sample
    (process exited, dropped out of top-N) and for entries that are
    well past their cooldown window."""
    to_drop = [
        key for key, fired_at in _recent_anomalies.items()
        if key[0] not in current_pids
        or now - fired_at > _ANOMALY_COOLDOWN_SECONDS * 4
    ]
    for key in to_drop:
        _recent_anomalies.pop(key, None)


def build_insights(
    memory: MemorySample,
    processes: list[ProcessRow],
    cassie: CassieStatus,
    orphan_resumed_count: int,
) -> list[Insight]:
    insights: list[Insight] = []
    now = time.time()
    current_pids = {row.pid for row in processes}
    _prune_anomaly_cooldowns(now, current_pids)

    # ---- Anomalies first: a sudden growth event is more time-sensitive
    # than the steady-state pressure banner. The user wants to see "X
    # just doubled" before "memory is tight". ----
    anomalies: list[Insight] = []
    for row in processes:
        ins = _detect_growth_anomaly(now, row)
        if ins is not None:
            anomalies.append(ins)
        if len(anomalies) >= _MAX_ANOMALIES_PER_TICK:
            break
    insights.extend(anomalies)

    # ---- State-based insights ----
    if memory.pressure_level in {"WARN", "CRITICAL"}:
        candidates = [
            process
            for process in processes
            if not process.pinned
            and not process.holds_audio
            and not process.holds_socket
            and process.state.startswith("idle")
        ]
        candidates.sort(key=lambda row: (row.seconds_idle, row.rss_gb), reverse=True)
        targets = candidates[:3]
        actions = []
        if targets:
            label = "Calm " + " + ".join(target.name for target in targets)
            actions.append(
                (
                    label,
                    {
                        "action": "calm_many",
                        "targets": [
                            {
                                "pid": target.pid,
                                "start_unix": target.start_unix,
                                "name": target.name,
                            }
                            for target in targets
                        ],
                    },
                )
            )
        insights.append(
            Insight(
                severity="critical" if memory.pressure_level == "CRITICAL" else "warn",
                message=(
                    f"Memory pressure {memory.pressure_level}. "
                    f"Swap out {memory.swap_out_rate_mbps:.1f} MB/s. "
                    "Calming idle apps may reduce churn."
                ),
                actions=actions,
            )
        )

    if memory.swap_out_rate_mbps > 5.0:
        insights.append(
            Insight(
                severity="critical",
                message=(
                    f"Swap out is {memory.swap_out_rate_mbps:.1f} MB/s. "
                    "The system is actively pushing memory to disk."
                ),
            )
        )

    if cassie.available and cassie.deep_loaded and cassie.seconds_idle > 600:
        insights.append(
            Insight(
                severity="info",
                message=(
                    f"Cassie deep model loaded and idle {cassie.seconds_idle // 60} min. "
                    "Set CASSIE_DISABLE_DEEP=1 before the next restart if you do not need it."
                ),
            )
        )

    if orphan_resumed_count > 0:
        insights.append(
            Insight(
                severity="info",
                message=f"Resumed {orphan_resumed_count} orphan paused processes at startup.",
            )
        )

    insights.append(
        Insight(
            severity="ok",
            message="Deny-list protections active for system apps and your terminal.",
        )
    )
    return insights
