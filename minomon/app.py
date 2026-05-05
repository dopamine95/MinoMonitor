"""
The Textual app — wires meters, processes, and insights to the sampler
and routes UI action requests to the action layer.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header

from .data.sample import Sample
from .ui.dialog import ConfirmAction
from .ui.insights import InsightActionRequested, InsightsPanel
from .ui.meters import MetersPanel
from .ui.processes import ActionRequested, ProcessesPanel
from .ui import theme


_PID_FILE = Path.home() / ".minomonitor" / "monitor.pid"


def _write_pid_file() -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def _clear_pid_file() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


class MinoMonitorApp(App):
    """Single-screen dashboard."""

    CSS = f"""
    Screen {{
        background: {theme.PALETTE['bg']};
    }}
    MetersPanel {{
        height: 14;
        margin: 0 1;
    }}
    ProcessesPanel {{
        margin: 0 1;
        height: 1fr;
    }}
    InsightsPanel {{
        height: auto;
        max-height: 12;
        margin: 0 1 1 1;
    }}
    DataTable {{
        height: 1fr;
    }}
    DataTable > .datatable--header {{
        background: {theme.PALETTE['bg_panel']};
        color: {theme.PALETTE['primary']};
        text-style: bold;
    }}
    DataTable > .datatable--cursor {{
        background: {theme.PALETTE['bg_row']};
    }}
    #processes-title {{
        height: 1;
        padding: 0 0 0 1;
    }}
    #processes-footer {{
        height: 1;
    }}
    """

    BINDINGS = [
        Binding("?", "help", "Help"),
        Binding("q", "quit_app", "Quit"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("R", "resume_all_paused", "Resume all", show=True),
        Binding("v", "toggle_vibe", "Vibe view"),
        # Apply insight actions from anywhere in the UI. `a` runs the first
        # available action, the digits 1-9 target a specific one.
        Binding("a", "apply_first_insight", "Apply", show=True),
        *[Binding(str(d), f"apply_insight({d})", show=False) for d in range(1, 10)],
    ]

    TITLE = "Mino Monitor"

    def __init__(self, sampler, vibe_mode: bool = False):
        super().__init__()
        self.sampler = sampler
        self._action_lock = asyncio.Lock()
        self._vibe_mode = vibe_mode
        self.sub_title = "vibe view" if vibe_mode else "live system telemetry"
        # Pending outcome-check coroutines, kept alive so asyncio doesn't
        # GC them mid-sleep. Each task removes itself from the set when
        # it finishes (see _schedule_outcome_check).
        self._outcome_tasks: set[asyncio.Task] = set()
        # Conservative auto-mode (off unless user opted in via config).
        # Consulted on every sample; runs the rule check inline.
        from .automode import AutoMode
        self.automode = AutoMode(self)
        self._automode_tasks: set[asyncio.Task] = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Vertical(
            MetersPanel(id="meters"),
            ProcessesPanel(id="processes"),
            InsightsPanel(id="insights"),
        )
        yield Footer()

    async def on_mount(self) -> None:
        _write_pid_file()
        # Apply initial vibe mode to all panels that support it
        self._apply_vibe_mode(self._vibe_mode)
        # Wire sampler updates → UI panels.
        self.sampler.subscribe(self._on_sample)
        await self.sampler.start()

    def _apply_vibe_mode(self, on: bool) -> None:
        try:
            self.query_one("#processes", ProcessesPanel).vibe_mode = on
            self.query_one("#meters", MetersPanel).vibe_mode = on
            self.query_one("#insights", InsightsPanel).vibe_mode = on
        except Exception:
            pass

    def action_toggle_vibe(self) -> None:
        self._vibe_mode = not self._vibe_mode
        self.sub_title = "vibe view" if self._vibe_mode else "live system telemetry"
        self._apply_vibe_mode(self._vibe_mode)
        self.notify(
            "Vibe view: ON — plain English mode."
            if self._vibe_mode else
            "Techie view: ON — full detail.",
            timeout=3,
        )

    async def on_unmount(self) -> None:
        try:
            await self.sampler.stop()
        except Exception:
            pass
        _clear_pid_file()

    def _on_sample(self, sample: Sample) -> None:
        # The sampler may call us from any task; use call_from_thread when
        # invoked off the event loop. In practice it's always on the same loop.
        try:
            meters = self.query_one("#meters", MetersPanel)
            meters.push(sample)
            meters.automode_status = self.automode.status_line()
            self.query_one("#processes", ProcessesPanel).push(sample)
            self.query_one("#insights", InsightsPanel).push(sample)
        except Exception:
            # During teardown the queries may fail — silently ignore.
            pass

        # Let auto-mode look at this sample. It runs async (calm is async)
        # but we don't await it — it lives in its own task so the sampler
        # callback stays cheap. Auto-mode is no-op unless the user opted in.
        if self.automode.enabled:
            task = asyncio.create_task(self.automode.consider(sample))
            self._automode_tasks.add(task)
            task.add_done_callback(self._automode_tasks.discard)

    async def on_action_requested(self, message: ActionRequested) -> None:
        """Routed from the process table. Fans out across all child pids
        when the row represents a group (Brave + 8 helpers, Xcode + 4
        XPC services, etc.) so the action covers the whole app, not just
        the parent."""
        sample = self.sampler.latest
        if sample is None:
            return
        row = next((r for r in sample.processes if r.pid == message.pid), None)
        if row is None or row.pinned:
            self.notify("Cannot act on pinned process.", severity="warning")
            return

        # Quit goes through the OS app shutdown (osascript) — that already
        # cascades to children, so we always operate on the parent only.
        # Calm / Pause / Resume need to fan out to each helper because
        # SIGSTOP/taskpolicy don't propagate to children.
        targets = (
            [(message.pid, message.start_unix)]
            if message.action == "quit"
            else list(row.child_pids) or [(message.pid, message.start_unix)]
        )

        # Calm/uncalm/thaw are reversible & low-risk — skip the confirm dialog.
        if message.action in ("calm", "uncalm", "thaw"):
            await self._dispatch_many(message.action, targets, message.name)
            return

        # Returns: None (cancelled), int seconds for freeze (0 = indefinite),
        # or a sentinel non-zero int for other confirmed actions.
        result = await self.push_screen_wait(ConfirmAction(message.action, row))
        if result is None:
            return
        kwargs: dict = {}
        if message.action == "freeze":
            kwargs["auto_resume_seconds"] = None if result == 0 else result
        await self._dispatch_many(message.action, targets, message.name, **kwargs)

    async def on_insight_action_requested(self, message: InsightActionRequested) -> None:
        payload = message.payload
        action = payload.get("action")
        if not action:
            return

        # Bulk action: insights can suggest "calm Slack + Discord + Notion" as
        # a single bundle. Iterate the target list and fire each individually.
        if action == "calm_many":
            targets = payload.get("targets") or []
            if not targets:
                return
            ok_count = 0
            failures: list[str] = []
            for t in targets:
                pid = int(t.get("pid", 0))
                start_unix = int(t.get("start_unix", 0))
                name = t.get("name", f"pid {pid}")
                if not pid:
                    continue
                try:
                    from .actions.calm import calm
                    async with self._action_lock:
                        result = await calm(pid, start_unix)
                    if result.success:
                        ok_count += 1
                    else:
                        failures.append(f"{name}: {result.message}")
                except Exception as e:
                    failures.append(f"{name}: {e}")
            total = len(targets)
            if failures:
                self.notify(
                    f"{theme.GLYPHS.icon_warn} Calmed {ok_count} of {total}. "
                    f"Failures: {'; '.join(failures[:3])}",
                    severity="warning",
                    timeout=6,
                )
            else:
                self.notify(
                    f"{theme.GLYPHS.icon_ok} Calmed {ok_count} processes.",
                    severity="information",
                    timeout=4,
                )
            return

        # Single-target action: same shape as table-row actions.
        pid = int(payload.get("pid", 0))
        start_unix = int(payload.get("start_unix", 0))
        name = payload.get("name", f"pid {pid}")
        if not pid:
            return
        await self._dispatch(action, pid, start_unix, name)

    async def _dispatch_many(
        self,
        action: str,
        targets: list[tuple[int, int]],
        group_name: str,
        **action_kwargs,
    ) -> None:
        """Run one action across N pids and post a single summary toast.
        For a group of one, behaves identically to the old per-pid dispatch."""
        if len(targets) == 1:
            pid, start_unix = targets[0]
            await self._dispatch(action, pid, start_unix, group_name, **action_kwargs)
            return

        # Capture the baseline BEFORE running the action so we can ask
        # "did it help?" 60s later.
        target_rss = self._target_rss_for_pids(targets)
        baseline = self._capture_outcome_baseline(action, group_name, target_rss)

        ok_count = 0
        failures: list[str] = []
        for pid, start_unix in targets:
            try:
                async with self._action_lock:
                    result = await self._run_action(action, pid, start_unix, **action_kwargs)
                if result is None or result.success:
                    ok_count += 1 if result is not None else 0
                else:
                    failures.append(result.message)
            except Exception as e:
                failures.append(str(e))

        total = len(targets)
        if failures:
            self.notify(
                f"{theme.GLYPHS.icon_warn} {action} {group_name}: "
                f"{ok_count} of {total} succeeded. "
                f"First failure: {failures[0][:100]}",
                severity="warning",
                timeout=6,
            )
        else:
            self.notify(
                f"{theme.GLYPHS.icon_ok} {action} {group_name} ({total} processes)",
                severity="information",
                timeout=4,
            )

        if baseline is not None and ok_count > 0:
            self._schedule_outcome_check(baseline)

    # ----- Outcome feedback -----
    # After every successful action we schedule a check ~60s later that
    # compares the system state to a baseline captured at action time and
    # surfaces a "helped / neutral / worsened" verdict. Background tasks
    # are kept in a set so they aren't garbage-collected mid-flight.

    _OUTCOME_CHECK_SECONDS = 60

    def _target_rss_for_pids(self, targets: list[tuple[int, int]]) -> float:
        """Look up the combined footprint (in GB) of the given (pid, start)
        tuples in the most recent sample. Used to grade `quit` actions —
        if App Memory drops by close to this number, the quit "helped"."""
        sample = self.sampler.latest
        if sample is None:
            return 0.0
        target_set = {pid for pid, _ in targets}
        total = 0.0
        for row in sample.processes:
            for pid, _ in row.child_pids:
                if pid in target_set:
                    total += row.rss_gb
                    break
        return round(total, 2)

    def _capture_outcome_baseline(self, action: str, name: str, target_rss: float):
        """Snapshot the current state for later comparison. Returns None
        if there's no sample to compare to (very first tick) or if this
        action is a no-op for outcome purposes (uncalm/thaw aren't worth
        grading — the user just undid an earlier action)."""
        if action in ("uncalm", "thaw"):
            return None
        sample = self.sampler.latest
        if sample is None:
            return None
        from .actions.outcomes import OutcomeBaseline
        return OutcomeBaseline(
            action=action,
            target_name=name,
            target_rss_gb=target_rss,
            memory=sample.memory,
            cpu_total_pct=sample.cpu.total_pct,
        )

    def _schedule_outcome_check(self, baseline) -> None:
        task = asyncio.create_task(self._outcome_check_task(baseline))
        self._outcome_tasks.add(task)
        task.add_done_callback(self._outcome_tasks.discard)

    async def _outcome_check_task(self, baseline) -> None:
        try:
            await asyncio.sleep(self._OUTCOME_CHECK_SECONDS)
            sample = self.sampler.latest
            if sample is None:
                return
            from .actions.outcomes import evaluate
            verdict = evaluate(baseline, sample.memory, sample.cpu.total_pct)
            self._notify_outcome(baseline, verdict)
            self._log_outcome(baseline, verdict)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Outcome feedback is non-critical; never raise into the loop.
            pass

    def _notify_outcome(self, baseline, verdict) -> None:
        glyph, severity = {
            "helped":   (theme.GLYPHS.icon_ok,       "information"),
            "neutral":  (theme.GLYPHS.icon_info,     "information"),
            "worsened": (theme.GLYPHS.icon_critical, "warning"),
        }[verdict.bucket]
        verb = {"calm": "Calmed", "freeze": "Paused", "quit": "Quit",
                "calm_many": "Calmed"}.get(baseline.action, baseline.action)
        self.notify(
            f"{glyph} {verb} {baseline.target_name}: "
            f"{verdict.bucket} — {verdict.summary}",
            severity=severity,
            timeout=8,
        )

    @staticmethod
    def _log_outcome(baseline, verdict) -> None:
        """Append a structured outcome line to ~/.minomonitor/actions.log
        next to the action's own log line. Schema is plain, easy to grep
        or feed to `minomon advise` later."""
        try:
            from .actions._common import append_action_log
            append_action_log(
                action=f"{baseline.action}.outcome",
                pid=0,
                start_unix=0,
                success=(verdict.bucket != "worsened"),
                message=f"{verdict.bucket}: {verdict.summary}",
                name=baseline.target_name,
            )
        except Exception:
            pass

    async def _run_action(self, action: str, pid: int, start_unix: int, **kwargs):
        """Single-pid action runner. Returns ActionResult or None for
        unknown actions. Used by _dispatch_many to keep each individual
        call lock-free at the loop level (the caller holds the lock once
        per pid). `kwargs` are forwarded to the action — `freeze` accepts
        `auto_resume_seconds` (int seconds, or None for indefinite)."""
        if action == "calm":
            from .actions.calm import calm
            return await calm(pid, start_unix)
        if action == "uncalm":
            from .actions.calm import uncalm
            return await uncalm(pid, start_unix)
        if action == "freeze":
            from .actions.freeze import freeze
            return await freeze(pid, start_unix, **kwargs)
        if action == "thaw":
            from .actions.freeze import thaw
            return await thaw(pid, start_unix)
        if action == "quit":
            from .actions.quit import quit_app
            return await quit_app(pid, start_unix)
        return None

    async def _dispatch(self, action: str, pid: int, start_unix: int, name: str, **kwargs) -> None:
        # Snapshot baseline BEFORE the action so the outcome verdict has
        # something honest to compare against.
        baseline = self._capture_outcome_baseline(
            action, name, target_rss=self._target_rss_for_pids([(pid, start_unix)])
        )

        async with self._action_lock:
            try:
                result = await self._run_action(action, pid, start_unix, **kwargs)
                if result is None:
                    self.notify(f"Unknown action: {action}", severity="error")
                    return
            except Exception as e:
                self.notify(f"{action} failed: {e}", severity="error", timeout=6)
                return

        sev = "information" if result.success else "error"
        glyph = theme.GLYPHS.icon_ok if result.success else theme.GLYPHS.icon_critical
        self.notify(
            f"{glyph} {action} {name}: {result.message}",
            severity=sev,
            timeout=4,
        )

        if baseline is not None and result.success:
            self._schedule_outcome_check(baseline)

    async def action_resume_all_paused(self) -> None:
        """Shift-R: resume every process this monitor currently has paused."""
        from .actions.freeze import thaw_all
        async with self._action_lock:
            resumed, failed = await thaw_all()
        if resumed == 0 and failed == 0:
            self.notify("Nothing was paused.", timeout=2)
            return
        if failed:
            self.notify(
                f"{theme.GLYPHS.icon_warn} Resumed {resumed} of {resumed + failed}.",
                severity="warning",
                timeout=4,
            )
        else:
            self.notify(
                f"{theme.GLYPHS.icon_ok} Resumed {resumed} paused process"
                f"{'es' if resumed != 1 else ''}.",
                timeout=3,
            )

    def action_apply_first_insight(self) -> None:
        try:
            panel = self.query_one("#insights", InsightsPanel)
        except Exception:
            return
        if not panel.numbered_actions:
            self.notify("No insight has an action available right now.", timeout=2)
            return
        _label, payload = panel.numbered_actions[0]
        self.post_message(InsightActionRequested(payload))

    def action_apply_insight(self, index: int) -> None:
        try:
            panel = self.query_one("#insights", InsightsPanel)
        except Exception:
            return
        idx = int(index) - 1
        if idx < 0 or idx >= len(panel.numbered_actions):
            return
        _label, payload = panel.numbered_actions[idx]
        self.post_message(InsightActionRequested(payload))

    async def action_quit_app(self) -> None:
        self.exit()

    def action_force_refresh(self) -> None:
        sample = self.sampler.latest
        if sample is not None:
            self._on_sample(sample)
        self.notify("Refreshed.", timeout=1)

    def action_help(self) -> None:
        self.notify(
            "c=calm  f=freeze  u=uncalm/thaw  q=quit  r=refresh  ?=help  Ctrl+C=exit",
            timeout=8,
        )
