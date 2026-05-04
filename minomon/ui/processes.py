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


_PROC_COUNT_CACHE = {"value": 0, "stamp": 0.0}


def _process_count_estimate() -> int:
    """Cheap-ish process count for the footer. Updates at most every 5s
    so we don't iterate all PIDs on every UI tick."""
    import time as _t
    import psutil as _ps
    if _t.monotonic() - _PROC_COUNT_CACHE["stamp"] > 5.0:
        try:
            _PROC_COUNT_CACHE["value"] = sum(1 for _ in _ps.pids())
        except Exception:
            pass
        _PROC_COUNT_CACHE["stamp"] = _t.monotonic()
    return _PROC_COUNT_CACHE["value"]


def _fmt_mmss(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _pause_countdown(row: ProcessRow) -> str:
    """' 0:23/1:00' for a timed pause, ' (∞)' for indefinite, '' if no meta."""
    if row.pause_resume_in is None and row.pause_total_seconds is None:
        return ""
    if row.pause_total_seconds is None or row.pause_resume_in is None:
        return " (∞)"
    return f" {_fmt_mmss(row.pause_resume_in)}/{_fmt_mmss(row.pause_total_seconds)}"


def _state_label_vibe(row: ProcessRow) -> str:
    """Plain-English version of the technical state."""
    s = row.state
    if row.pinned:
        return "system · protected"
    if s == "paused":
        countdown = _pause_countdown(row)
        if countdown == " (∞)":
            return "paused by you · indefinite"
        if countdown:
            return f"paused · resumes in{countdown.split('/')[0]}"
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


# Sort modes the user can cycle through with `s`. Each tuple is
# (key, label, attr_name on ProcessRow). When attr is None we fall back
# to RSS as the secondary key. Δ values may be None for new processes —
# we treat None as 0 for sorting so they sink below growers.
_SORT_MODES = (
    ("memory", "by Memory",      "rss_gb"),
    ("d1m",    "by Δ memory 1m", "delta_1m_gb"),
    ("d5m",    "by Δ memory 5m", "delta_5m_gb"),
    ("d15m",   "by Δ memory 15m","delta_15m_gb"),
)


class ProcessesPanel(Vertical):
    """A title row + DataTable. Toggleable vibe / techie display modes."""

    BINDINGS = [
        Binding("c", "calm_selected",  "Calm", show=True),
        Binding("f", "freeze_selected", "Pause", show=True),
        Binding("u", "uncalm_selected", "Resume", show=True),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("Q", "quit_selected", "Quit app", show=False),
    ]

    sample: reactive[Sample | None] = reactive(None)
    vibe_mode: reactive[bool] = reactive(False)
    sort_index: reactive[int] = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._table = DataTable(zebra_stripes=True, cursor_type="row", header_height=1)
        self._title = Static("", id="processes-title")
        self._footer = Static("", id="processes-footer")
        self._rows_by_key: dict[int, ProcessRow] = {}
        self._columns_for_mode: str | None = None

    def compose(self):
        yield self._title
        yield self._table
        yield self._footer

    def on_mount(self) -> None:
        self._configure_columns()
        self._table.styles.height = "1fr"
        self._table.styles.background = theme.PALETTE["bg_panel"]
        self._title.update(self._title_markup(0))

    def _columns_signature(self) -> str:
        """Cache key for the configured column layout. Re-running the
        column setup is expensive (clears the table); only do it when
        either the mode or the sort window flips."""
        return f"{'vibe' if self.vibe_mode else 'techie'}|sort={_SORT_MODES[self.sort_index][0]}"

    def _configure_columns(self) -> None:
        """Add the right columns for the current mode. Called whenever the
        mode flips or on first mount."""
        if self._columns_for_mode == self._columns_signature():
            return
        self._table.clear(columns=True)
        attr = _SORT_MODES[self.sort_index][2]
        delta_label = {
            "delta_1m_gb":  "Δ 1m",
            "delta_5m_gb":  "Δ 5m",
            "delta_15m_gb": "Δ 15m",
        }.get(attr)
        if self.vibe_mode:
            self._table.add_column("",        width=2,  key="dot")
            self._table.add_column("App",                key="name")
            self._table.add_column("Memory",  width=12, key="mem")
            if delta_label:
                self._table.add_column(delta_label, width=11, key="delta")
            self._table.add_column("CPU",     width=8,  key="cpu")
            self._table.add_column("Doing",              key="state")
            self._table.add_column("Action",             key="action")
        else:
            self._table.add_column("",        width=2,  key="dot")
            self._table.add_column("Name",               key="name")
            self._table.add_column("Memory",  width=10, key="mem")
            if delta_label:
                self._table.add_column(delta_label, width=11, key="delta")
            self._table.add_column("CPU",     width=7,  key="cpu")
            self._table.add_column("State",   width=18, key="state")
            self._table.add_column("Bundle",  width=30, key="bundle")
            self._table.add_column("",                   key="action")
        self._columns_for_mode = self._columns_signature()

    def _title_markup(self, n: int) -> Text:
        sort_label = _SORT_MODES[self.sort_index][1]
        if self.vibe_mode:
            growth_hint = (
                ""
                if self.sort_index == 0
                else f" · [bold {theme.SEVERITY['warn']}]{sort_label}[/]"
            )
            return Text.from_markup(
                f"[bold {theme.PALETTE['primary']}]Apps using your Mac[/]   "
                f"[{theme.PALETTE['muted']}]{n} shown{growth_hint} · "
                f"[bold]↑/↓[/] pick · [bold]f[/] pause · [bold]u[/] resume · "
                f"[bold]s[/] sort · [bold]v[/] switch view[/]"
            )
        sort_chunk = (
            ""
            if self.sort_index == 0
            else f"  ·  sorted [bold {theme.SEVERITY['warn']}]{sort_label}[/]"
        )
        return Text.from_markup(
            f"[bold {theme.PALETTE['primary']}]TOP PROCESSES[/]   "
            f"[{theme.PALETTE['muted']}]{n} shown{sort_chunk} · "
            f"[bold]c[/]alm  [bold]f[/]reeze  [bold]u[/]ncalm  [bold]s[/]ort  Shift-[bold]Q[/] quit-app · "
            f"[bold]v[/] vibe view[/]"
        )

    def push(self, sample: Sample) -> None:
        self.sample = sample
        self._refresh_table(sample)

    def watch_vibe_mode(self, _old: bool, _new: bool) -> None:
        self._configure_columns()
        if self.sample is not None:
            self._refresh_table(self.sample)

    def watch_sort_index(self, _old: int, _new: int) -> None:
        self._configure_columns()
        if self.sample is not None:
            self._refresh_table(self.sample)
        try:
            label = _SORT_MODES[_new][1]
            self.app.notify(f"Sorted {label}", timeout=2)
        except Exception:
            pass

    def action_cycle_sort(self) -> None:
        self.sort_index = (self.sort_index + 1) % len(_SORT_MODES)

    def _refresh_table(self, sample: Sample) -> None:
        # Save cursor position so the user doesn't lose their place each tick
        try:
            saved_row = self._table.cursor_row
        except Exception:
            saved_row = 0

        self._configure_columns()
        self._table.clear()
        self._rows_by_key = {}

        # If the user picked a growth-based sort, re-rank by that delta
        # (None deltas sort last). RSS sort is the default and matches what
        # the sampler already produced.
        rows_in = sample.processes
        attr = _SORT_MODES[self.sort_index][2]
        if attr != "rss_gb":
            rows_in = sorted(
                sample.processes,
                key=lambda r: (getattr(r, attr) is not None, getattr(r, attr) or 0),
                reverse=True,
            )
        rows_to_show = rows_in[:30]
        self._title.update(self._title_markup(len(rows_to_show)))

        for row in rows_to_show:
            self._rows_by_key[row.pid] = row
            self._table.add_row(*self._cells_for(row), key=str(row.pid), height=1)

        self._update_footer(sample, rows_to_show)

        # Restore cursor
        if rows_to_show and saved_row is not None:
            try:
                self._table.move_cursor(row=min(saved_row, len(rows_to_show) - 1))
            except Exception:
                pass

    def _update_footer(self, sample: Sample, rows: list[ProcessRow]) -> None:
        """Honest accounting line. Per-process numbers are phys_footprint —
        the same ledger Activity Monitor's Memory column uses. They do NOT
        sum to App Memory because the system-wide total includes shared
        anonymous pages, owned-but-unmapped memory, and kernel allocations
        that aren't attributable to any single process. The same gap exists
        in Activity Monitor — Apple's `footprint(1)` man page documents this."""
        shown_sum = sum(r.rss_gb for r in rows)
        app_total = sample.memory.app_gb
        gap = max(0, app_total - shown_sum)

        if self.vibe_mode:
            footer = (
                f"  [{theme.PALETTE['muted']}]Showing {len(rows)} apps · "
                f"sum [bold {theme.PALETTE['fg']}]{shown_sum:.1f} GB[/]   "
                f"App Memory total [bold {theme.PALETTE['fg']}]{app_total:.1f} GB[/]   "
                f"the [bold]{gap:.1f} GB[/] difference is shared system memory + many "
                f"small background apps · macOS does the same in Activity Monitor[/]"
            )
        else:
            footer = (
                f"  [{theme.PALETTE['muted']}]"
                f"shown sum [bold {theme.PALETTE['fg']}]{shown_sum:.1f} GB[/]  ·  "
                f"App Memory [bold {theme.PALETTE['fg']}]{app_total:.1f} GB[/]  ·  "
                f"unaccounted [bold]{gap:.1f} GB[/] (shared dyld cache, kernel-owned, "
                f"~{max(0, _process_count_estimate() - len(rows))} smaller processes) — "
                f"same as Activity Monitor; per-process is phys_footprint, not a sum-to-total ledger[/]"
            )
        self._footer.update(Text.from_markup(footer))

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
            state_text = row.state + _pause_countdown(row)
        state = Text(state_text, style=_state_color(row.state, row.pinned))

        action = _action_cell(row, vibe=self.vibe_mode)

        # Delta cell only present in growth-sort mode
        delta_cell = None
        attr = _SORT_MODES[self.sort_index][2]
        if attr in ("delta_1m_gb", "delta_5m_gb", "delta_15m_gb"):
            delta_val = getattr(row, attr)
            delta_cell = self._format_delta(delta_val)

        if self.vibe_mode:
            cells = [dot, name, mem]
            if delta_cell is not None:
                cells.append(delta_cell)
            cells.extend([cpu, state, action])
            return cells

        bundle = Text((row.bundle_id or "")[:30], style=theme.PALETTE["dim"])
        cells = [dot, name, mem]
        if delta_cell is not None:
            cells.append(delta_cell)
        cells.extend([cpu, state, bundle, action])
        return cells

    @staticmethod
    def _format_delta(value: float | None) -> Text:
        if value is None:
            return Text("—", style=theme.PALETTE["dim"])
        if abs(value) < 0.01:
            return Text("±0", style=theme.PALETTE["muted"])
        if value > 0:
            color = theme.SEVERITY["critical"] if value >= 1.0 else theme.SEVERITY["warn"]
            return Text(f"↑ {value:+.2f} GB", style=color)
        # shrinking — green is welcome
        return Text(f"↓ {value:+.2f} GB", style=theme.SEVERITY["ok"])

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
