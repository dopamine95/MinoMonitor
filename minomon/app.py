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
    """

    BINDINGS = [
        Binding("?", "help", "Help"),
        Binding("q", "quit_app", "Quit"),
        Binding("r", "force_refresh", "Refresh"),
    ]

    TITLE = "Mino Monitor"
    SUB_TITLE = "live system telemetry"

    def __init__(self, sampler):
        super().__init__()
        self.sampler = sampler
        self._action_lock = asyncio.Lock()

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
        # Wire sampler updates → UI panels.
        self.sampler.subscribe(self._on_sample)
        await self.sampler.start()

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
            self.query_one("#meters", MetersPanel).push(sample)
            self.query_one("#processes", ProcessesPanel).push(sample)
            self.query_one("#insights", InsightsPanel).push(sample)
        except Exception:
            # During teardown the queries may fail — silently ignore.
            pass

    async def on_action_requested(self, message: ActionRequested) -> None:
        """Routed from the process table."""
        sample = self.sampler.latest
        if sample is None:
            return
        row = next((r for r in sample.processes if r.pid == message.pid), None)
        if row is None or row.pinned:
            self.notify("Cannot act on pinned process.", severity="warning")
            return

        # Calm/uncalm are reversible & low-risk — skip the confirm dialog.
        if message.action in ("calm", "uncalm", "thaw"):
            await self._dispatch(message.action, message.pid, message.start_unix, message.name)
            return

        confirmed = await self.push_screen_wait(ConfirmAction(message.action, row))
        if confirmed:
            await self._dispatch(message.action, message.pid, message.start_unix, message.name)

    async def on_insight_action_requested(self, message: InsightActionRequested) -> None:
        payload = message.payload
        action = payload.get("action")
        pid = int(payload.get("pid", 0))
        start_unix = int(payload.get("start_unix", 0))
        name = payload.get("name", f"pid {pid}")
        if not action or not pid:
            return
        await self._dispatch(action, pid, start_unix, name)

    async def _dispatch(self, action: str, pid: int, start_unix: int, name: str) -> None:
        async with self._action_lock:
            try:
                if action == "calm":
                    from .actions.calm import calm
                    result = await calm(pid, start_unix)
                elif action == "uncalm":
                    from .actions.calm import uncalm
                    result = await uncalm(pid, start_unix)
                elif action == "freeze":
                    from .actions.freeze import freeze
                    result = await freeze(pid, start_unix)
                elif action == "thaw":
                    from .actions.freeze import thaw
                    result = await thaw(pid, start_unix)
                elif action == "quit":
                    from .actions.quit import quit_app
                    result = await quit_app(pid, start_unix)
                else:
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
