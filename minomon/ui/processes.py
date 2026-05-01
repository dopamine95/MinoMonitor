"""
Process table.

Two modes that change the labels and visible columns:
- techie (default): name · memory · cpu · state · bundle id · action hint
- vibe   (toggle v): name · memory · cpu · what it's doing · [Pause] button

Pause is the most prominent action because it's the safest way to relieve
RAM pressure on a process you don't want to lose. It's a SIGSTOP with an
auto-resume timer; the dialog explains this in plain English.
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


def _state_label_vibe(row: ProcessRow) -> str:
    """Plain-English version of the technical state."""
    s = row.state
    if row.pinned:
        return "system · protected"
    if s == "paused":
        return "paused by you"
    if s == "calmed":
        return "running quietly (you slowed it)"
    if s == "active":
        return "active right now"
    if s == "foreground":
        return "open, not in use"
    if s == "playing":
        return "playing media"
    if s.startswith("idle "):
        return f"hasn't done anything for {s.split(' ', 1)[1]}"
    return s


def _state_color(state: str, pinned: bool) -> str:
    if pinned:
        return theme.PALETTE["pinned"]
    head = state.split()[0]
    return {
        "active":     theme.PALETTE["active"],
        "foreground": theme.PALETTE["fg"],
        "playing":    theme.SEVERITY["info"],
        "paused":     theme.SEVERITY["warn"],
        "calmed":     theme.PALETTE["muted"],
        "idle":       theme.PALETTE["muted"],
    }.get(head, theme.PALETTE["fg"])


def _action_cell(row: ProcessRow, vibe: bool) -> Text:
    """The clickable-looking last column. Pinned shows a 'protected' badge,
    paused/calmed show their reverse action, otherwise: vibe mode shows a
    big 'Pause' pill, techie shows compact key hints."""
    if row.pinned:
        return Text.from_markup(
            f"[bold {theme.PALETTE['pinned']} on {theme.PALETTE['bg_panel']}]  protected  [/]"
        )
    if row.state == "paused":
        return Text.from_markup(
            f"[bold {theme.PALETTE['bg']} on {theme.SEVERITY['ok']}]  Resume (u)  [/]"
        )
    if row.state == "calmed":
        return Text.from_markup(
            f"[bold {theme.PALETTE['bg']} on {theme.SEVERITY['ok']}]  Restore (u)  [/]"
        )

    if vibe:
        # One big obvious button. Plain language.
        if row.holds_audio or row.holds_socket:
            return Text.from_markup(
                f"[bold {theme.PALETTE['bg']} on {theme.SEVERITY['warn']}]  Pause (f) [/]"
                f"  [{theme.PALETTE['muted']}]careful — see warning[/]"
            )
        return Text.from_markup(
            f"[bold {theme.PALETTE['bg']} on {theme.PALETTE['primary']}]  Pause (f)  [/]"
        )

    # techie mode — compact hints + flags
    parts = []
    if row.holds_audio:
        parts.append(f"[{theme.SEVERITY['warn']}]audio[/]")
    if row.holds_socket:
        parts.append(f"[{theme.SEVERITY['warn']}]socket[/]")
    parts.append(f"[{theme.PALETTE['muted']}][c]alm [f]reeze[/]")
    return Text.from_markup("  ".join(parts))


class ProcessesPanel(Vertical):
    """A title row + DataTable. Toggleable vibe / techie display modes."""

    BINDINGS = [
        Binding("c", "calm_selected",  "Calm", show=True),
        Binding("f", "freeze_selected", "Pause", show=True),
        Binding("u", "uncalm_selected", "Resume", show=True),
        Binding("Q", "quit_selected", "Quit app", show=False),
    ]

    sample: reactive[Sample | None] = reactive(None)
    vibe_mode: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._table = DataTable(zebra_stripes=True, cursor_type="row", header_height=1)
        self._title = Static("", id="processes-title")
        self._rows_by_key: dict[int, ProcessRow] = {}
        self._columns_for_mode: str | None = None

    def compose(self):
        yield self._title
        yield self._table

    def on_mount(self) -> None:
        self._configure_columns()
        self._table.styles.height = "1fr"
        self._table.styles.background = theme.PALETTE["bg_panel"]
        self._title.update(self._title_markup(0))

    def _configure_columns(self) -> None:
        """Add the right columns for the current mode. Called whenever the
        mode flips or on first mount."""
        if self._columns_for_mode == ("vibe" if self.vibe_mode else "techie"):
            return
        self._table.clear(columns=True)
        if self.vibe_mode:
            self._table.add_column("",        width=2,  key="dot")
            self._table.add_column("App",                key="name")
            self._table.add_column("Memory",  width=12, key="mem")
            self._table.add_column("CPU",     width=8,  key="cpu")
            self._table.add_column("Doing",              key="state")
            self._table.add_column("Action",             key="action")
        else:
            self._table.add_column("",        width=2,  key="dot")
            self._table.add_column("Name",               key="name")
            self._table.add_column("Memory",  width=10, key="mem")
            self._table.add_column("CPU",     width=7,  key="cpu")
            self._table.add_column("State",   width=18, key="state")
            self._table.add_column("Bundle",  width=30, key="bundle")
            self._table.add_column("",                   key="action")
        self._columns_for_mode = "vibe" if self.vibe_mode else "techie"

    def _title_markup(self, n: int) -> Text:
        if self.vibe_mode:
            return Text.from_markup(
                f"[bold {theme.PALETTE['primary']}]Apps using your Mac[/]   "
                f"[{theme.PALETTE['muted']}]{n} shown · "
                f"[bold]↑/↓[/] pick · [bold]f[/] pause · [bold]u[/] resume · "
                f"[bold]v[/] switch view[/]"
            )
        return Text.from_markup(
            f"[bold {theme.PALETTE['primary']}]TOP PROCESSES[/]   "
            f"[{theme.PALETTE['muted']}]{n} shown · "
            f"[bold]c[/]alm  [bold]f[/]reeze  [bold]u[/]ncalm  Shift-[bold]Q[/] quit-app · "
            f"[bold]v[/] vibe view[/]"
        )

    def push(self, sample: Sample) -> None:
        self.sample = sample
        self._refresh_table(sample)

    def watch_vibe_mode(self, _old: bool, _new: bool) -> None:
        self._configure_columns()
        if self.sample is not None:
            self._refresh_table(self.sample)

    def _refresh_table(self, sample: Sample) -> None:
        # Save cursor position so the user doesn't lose their place each tick
        try:
            saved_row = self._table.cursor_row
        except Exception:
            saved_row = 0

        self._configure_columns()
        self._table.clear()
        self._rows_by_key = {}
        rows_to_show = sample.processes[:30]
        self._title.update(self._title_markup(len(rows_to_show)))

        for row in rows_to_show:
            self._rows_by_key[row.pid] = row
            self._table.add_row(*self._cells_for(row), key=str(row.pid), height=1)

        # Restore cursor
        if rows_to_show and saved_row is not None:
            try:
                self._table.move_cursor(row=min(saved_row, len(rows_to_show) - 1))
            except Exception:
                pass

    def _cells_for(self, row: ProcessRow) -> list:
        # Dot column — frontmost / paused / pinned indicator
        if row.pinned:
            dot = Text(theme.GLYPHS.dot_pinned, style=theme.PALETTE["pinned"])
        elif row.state == "paused":
            dot = Text(theme.GLYPHS.dot_idle, style=theme.SEVERITY["warn"])
        elif row.state in ("active", "playing"):
            dot = Text(theme.GLYPHS.dot_active, style=theme.PALETTE["active"])
        elif row.state.startswith("idle") or row.state == "calmed":
            dot = Text(theme.GLYPHS.dot_idle, style=theme.PALETTE["muted"])
        else:
            dot = Text(" ")

        # Name — bright, with bundle-id second line in vibe mode being too
        # busy, so we just brighten the name in vibe and keep bundle separate
        # in techie.
        name_style = theme.PALETTE["pinned"] if row.pinned else theme.PALETTE["fg_strong"]
        name = Text(row.name, style=f"bold {name_style}")

        # Memory — color by absolute size (≥4 GB = warn, ≥8 GB = critical)
        if row.rss_gb >= 8:
            mem_color = theme.SEVERITY["critical"]
        elif row.rss_gb >= 4:
            mem_color = theme.SEVERITY["warn"]
        elif row.rss_gb >= 1:
            mem_color = theme.PALETTE["fg"]
        else:
            mem_color = theme.PALETTE["muted"]
        mem = Text(theme.fmt_gb(row.rss_gb), style=f"bold {mem_color}")

        cpu_sev = theme.severity_for_pct(row.cpu_pct)
        cpu = Text(f"{row.cpu_pct:>4.1f}%", style=theme.severity_color(cpu_sev))

        if self.vibe_mode:
            state_text = _state_label_vibe(row)
        else:
            state_text = row.state
        state = Text(state_text, style=_state_color(row.state, row.pinned))

        action = _action_cell(row, vibe=self.vibe_mode)

        if self.vibe_mode:
            return [dot, name, mem, cpu, state, action]
        bundle = Text((row.bundle_id or "")[:30], style=theme.PALETTE["dim"])
        return [dot, name, mem, cpu, state, bundle, action]

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
