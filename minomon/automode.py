"""
Conservative auto-mode.

Twice-deferred since v1. Codex's framing in the v3 debate:
"Either ship the conservative version or kill it publicly. Roadmap
theater is worse than either decision."

This is the "ship" version. Off by default; opt-in via config:

    [automode]
    enabled = true
    max_per_hour = 2
    idle_minimum_seconds = 3600

The single rule:

    pressure_level == "CRITICAL"
    AND target.state starts with "idle "
    AND target.seconds_idle >= idle_minimum_seconds
    AND not target.pinned
    AND not target.holds_audio
    AND not target.holds_socket
    AND target hasn't been auto-touched in the last 30 minutes
    AND we've fired ≤ max_per_hour times in the last 60 minutes

The action is always `calm` (taskpolicy -b). Never `freeze` (SIGSTOP),
never `quit`. Calm is reversible, doesn't damage sockets, and the
outcome-feedback machinery will tell us 60 seconds later whether it
helped — building the data that future advisor calls will use to
either tighten the rule, loosen it, or recommend turning auto-mode
off entirely.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

from .data.config import load_automode_config


# Don't auto-touch the same PID more than once in 30 minutes. If our
# first calm didn't help, a second one within minutes won't either —
# the user should review the outcome and act manually.
_PER_PID_COOLDOWN_SECONDS = 30 * 60


class AutoMode:
    """Lives on the App. Consulted on every sampler tick. Fires at most
    one calm per consultation, never re-entering itself, never fighting
    the user-action lock (calls go through the App's standard dispatch
    so outcome feedback applies)."""

    def __init__(self, app):
        self.app = app
        self.enabled = False
        self.max_per_hour = 2
        self.idle_minimum = 60 * 60
        self._fires: deque[float] = deque()
        self._last_fire_per_pid: dict[int, float] = {}
        # Refresh config from disk no more than once every 30s — a
        # config-toggle change should take effect quickly without us
        # stat'ing on every tick.
        self._last_config_load: float = 0.0
        self.refresh_config(force=True)

    def refresh_config(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_config_load < 30.0:
            return
        cfg = load_automode_config()
        self.enabled = cfg["enabled"]
        self.max_per_hour = cfg["max_per_hour"]
        self.idle_minimum = cfg["idle_minimum"]
        self._last_config_load = now

    # ----- Status surface for the UI -----

    def fires_in_last_hour(self) -> int:
        self._evict_old_fires(time.time())
        return len(self._fires)

    def status_line(self) -> Optional[str]:
        """Short human-readable line shown in the meters panel footer
        when auto-mode is on. None when disabled (UI hides the line)."""
        if not self.enabled:
            return None
        used = self.fires_in_last_hour()
        return f"auto-mode on · {used}/{self.max_per_hour} fires in last hour"

    # ----- Decision -----

    async def consider(self, sample) -> None:
        self.refresh_config(force=False)
        if not self.enabled:
            return
        if sample is None:
            return
        if sample.memory.pressure_level != "CRITICAL":
            return

        now = time.time()
        self._evict_old_fires(now)
        if len(self._fires) >= self.max_per_hour:
            return

        target = self._pick_target(sample.processes, now)
        if target is None:
            return

        # Record BEFORE firing. If the dispatch errors out we still
        # want the cooldown so we don't immediately retry the same
        # PID and spam logs.
        self._fires.append(now)
        self._last_fire_per_pid[target.pid] = now

        idle_min = max(1, target.seconds_idle // 60)
        try:
            await self.app._dispatch_many(
                "calm",
                list(target.child_pids) or [(target.pid, target.start_unix)],
                target.name,
            )
            # Distinguish auto-mode actions in the log from manual ones.
            from .actions._common import append_action_log
            append_action_log(
                action="auto.calm",
                pid=target.pid,
                start_unix=target.start_unix,
                success=True,
                message=f"pressure CRITICAL, idle {idle_min}m",
                name=target.name,
            )
            self.app.notify(
                f"auto-mode calmed {target.name} (idle {idle_min}m, pressure CRITICAL)",
                severity="information",
                timeout=8,
            )
        except Exception as e:
            self.app.notify(
                f"auto-mode failed on {target.name}: {e}",
                severity="warning",
                timeout=6,
            )

    def _evict_old_fires(self, now: float) -> None:
        cutoff = now - 3600
        while self._fires and self._fires[0] < cutoff:
            self._fires.popleft()
        # Also prune the per-pid cooldown so the dict doesn't grow
        # forever in a long session.
        stale_cutoff = now - _PER_PID_COOLDOWN_SECONDS
        for pid in [p for p, ts in self._last_fire_per_pid.items() if ts < stale_cutoff]:
            self._last_fire_per_pid.pop(pid, None)

    def _pick_target(self, processes, now: float):
        """Return the best calm candidate, or None. 'Best' = idle longest,
        biggest footprint as tiebreaker — i.e. the most likely thing the
        user has forgotten about and that's also worth quieting."""
        best = None
        for row in processes:
            if row.pinned:
                continue
            if row.holds_audio or row.holds_socket:
                continue
            if not row.state.startswith("idle"):
                continue
            if row.state in ("paused", "calmed"):
                continue
            if row.seconds_idle < self.idle_minimum:
                continue
            last = self._last_fire_per_pid.get(row.pid, 0.0)
            if now - last < _PER_PID_COOLDOWN_SECONDS:
                continue

            score = (row.seconds_idle, row.rss_gb)
            if best is None or score > best[0]:
                best = (score, row)
        return None if best is None else best[1]
