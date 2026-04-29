"""
Process table — top processes by RSS, with click-to-act buttons in the
last column. Selecting a row highlights it and the footer hotkeys (c/f/q)
target the selected row.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from ..data.sample import ProcessRow, Sample
from . import theme


class ActionRequested(Message):
    """Posted when user picks an action on a row. App handler runs it."""

    def __init__(self, action: str, pid: int, start_unix: int, name: str) -> None:
        self.action = action
        self.pid = pid
        self.start_unix = start_unix
        self.name = name
        super().__init__()


class ProcessesPanel(Vertical):
    """A title row + DataTable with sortable columns."""

    BINDINGS = [
        Binding("c", "calm_selected",  "Calm", show=True),
        Binding("f", "freeze_selected", "Freeze", show=True),
        Binding("q", "quit_selected", "Quit app", show=False),
        Binding("u", "uncalm_selected", "Uncalm", show=False),
    ]

    sample: reactive[Sample | None] = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._table = DataTable(zebra_stripes=True, cursor_type="row", header_height=1)
        self._title = Static("", id="processes-title")
        self._rows_by_key: dict[int, ProcessRow] = {}

    def compose(self):
        yield self._title
        yield self._table

    def on_mount(self) -> None:
        self._table.add_columns(
            "▸",                       # frontmost / pinned dot
            "Name",
            "RAM",
            "CPU",
            "State",
            "Bundle",
            "",                        # action hint
        )
        self._table.styles.height = "1fr"
        self._table.styles.background = theme.PALETTE["bg_panel"]
        self._title.update(self._title_markup(0))

    def _title_markup(self, n: int) -> Text:
        return Text.from_markup(
            f"[bold {theme.PALETTE['primary']}]TOP PROCESSES[/]  "
            f"[{theme.PALETTE['muted']}]{n} shown · "
            f"keys: [bold]c[/]alm  [bold]f[/]reeze  [bold]u[/]ncalm  [bold]q[/]uit-app[/]"
        )

    def push(self, sample: Sample) -> None:
        self.sample = sample
        self._refresh_table(sample)

    def _refresh_table(self, sample: Sample) -> None:
        # Clear and re-add. DataTable mutation is cheap enough at 30 rows.
        self._table.clear()
        self._rows_by_key = {}
        rows_to_show = sample.processes[:30]
        self._title.update(self._title_markup(len(rows_to_show)))

        for row in rows_to_show:
            self._rows_by_key[row.pid] = row

            if row.pinned:
                dot = Text(theme.GLYPHS.dot_pinned, style=theme.PALETTE["pinned"])
            elif row.state.startswith("idle") or row.state == "calmed":
                dot = Text(theme.GLYPHS.dot_idle, style=theme.PALETTE["muted"])
            elif row.state == "paused":
                dot = Text(theme.GLYPHS.dot_idle, style=theme.SEVERITY["warn"])
            elif row.state in ("active", "playing"):
                dot = Text(theme.GLYPHS.dot_active, style=theme.PALETTE["active"])
            else:
                dot = Text(" ")

            name = Text(row.name, style=theme.PALETTE["fg_strong"] if not row.pinned
                        else theme.PALETTE["pinned"])

            ram_sev = theme.severity_for_pct((row.rss_gb / 64) * 100)
            ram = Text(theme.fmt_gb(row.rss_gb), style=theme.severity_color(ram_sev))

            cpu_sev = theme.severity_for_pct(row.cpu_pct)
            cpu = Text(f"{row.cpu_pct:>4.1f}%", style=theme.severity_color(cpu_sev))

            state_color = {
                "active":     theme.PALETTE["active"],
                "foreground": theme.PALETTE["fg"],
                "playing":    theme.SEVERITY["info"],
                "paused":     theme.SEVERITY["warn"],
                "calmed":     theme.PALETTE["muted"],
            }.get(row.state.split()[0], theme.PALETTE["muted"])
            state = Text(row.state, style=state_color)

            bundle = Text(
                (row.bundle_id or "")[:32],
                style=theme.PALETTE["dim"],
            )

            if row.pinned:
                hint = Text("pinned", style=theme.PALETTE["pinned"])
            elif row.state == "paused":
                hint = Text("[u] uncalm/thaw", style=theme.PALETTE["muted"])
            elif row.state == "calmed":
                hint = Text("[u] restore", style=theme.PALETTE["muted"])
            else:
                marks = []
                if row.holds_audio:
                    marks.append(Text("audio", style=theme.SEVERITY["warn"]))
                if row.holds_socket:
                    marks.append(Text("socket", style=theme.SEVERITY["warn"]))
                hint = Text("  ").join(marks) if marks else Text("[c]alm  [f]reeze",
                                                                  style=theme.PALETTE["muted"])

            self._table.add_row(dot, name, ram, cpu, state, bundle, hint, key=str(row.pid))

    # ----- Action bindings -----

    def _selected_row(self) -> ProcessRow | None:
        try:
            cursor_key = self._table.coordinate_to_cell_key(self._table.cursor_coordinate).row_key
        except Exception:
            return None
        if cursor_key.value is None:
            return None
        try:
            pid = int(cursor_key.value)
        except (TypeError, ValueError):
            return None
        return self._rows_by_key.get(pid)

    def action_calm_selected(self) -> None:
        row = self._selected_row()
        if row is None or row.pinned:
            return
        self.post_message(ActionRequested("calm", row.pid, row.start_unix, row.name))

    def action_uncalm_selected(self) -> None:
        row = self._selected_row()
        if row is None or row.pinned:
            return
        # Choose uncalm vs thaw based on current state
        action = "thaw" if row.state == "paused" else "uncalm"
        self.post_message(ActionRequested(action, row.pid, row.start_unix, row.name))

    def action_freeze_selected(self) -> None:
        row = self._selected_row()
        if row is None or row.pinned:
            return
        self.post_message(ActionRequested("freeze", row.pid, row.start_unix, row.name))

    def action_quit_selected(self) -> None:
        row = self._selected_row()
        if row is None or row.pinned:
            return
        self.post_message(ActionRequested("quit", row.pid, row.start_unix, row.name))
