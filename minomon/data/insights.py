from __future__ import annotations

from .sample import CassieStatus, Insight, MemorySample, ProcessRow


def build_insights(
    memory: MemorySample,
    processes: list[ProcessRow],
    cassie: CassieStatus,
    orphan_resumed_count: int,
) -> list[Insight]:
    insights: list[Insight] = []

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
            message="Deny-list protections are active for system apps, terminals, Xcode, and Cassie.",
        )
    )
    return insights
