"""
Confirm dialog with countdown.

Returns:
    None  — user cancelled
    int   — user confirmed; for freeze actions this is the chosen
            auto-resume duration in seconds, where 0 means indefinite
            (no auto-resume scheduled). For other actions the value is
            ignored.

The freeze flow shows a duration picker row (30s · 1m · 2m · 5m · ∞)
with the current choice highlighted and selectable via digit keys 1-5.
"""

from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..data.sample import ProcessRow
from . import theme


_ACTION_DESCRIPTION = {
    "calm":   ("Calm process",
               "Lowers QoS to background. Safe and reversible. No socket damage."),
    "uncalm": ("Restore QoS",
               "Returns the process to default scheduling priority."),
    "freeze": ("Pause this app",
               "Pauses the app completely without quitting it. "
               "Memory stays where it is, but CPU work stops, which calms the system. "
               "Safe for most apps; may briefly disconnect audio or live "
               "chat sessions when resumed."),
    "thaw":   ("Resume this app",
               "Wakes a paused app right back up where it left off."),
    "quit":   ("Quit application",
               "Sends a graceful quit (osascript). Falls back to SIGTERM if it doesn't respond."),
}


# Duration choices for the freeze picker. (label, seconds). 0 = indefinite.
_DURATION_OPTIONS = (
    ("30s", 30),
    ("1m",  60),
    ("2m",  120),
    ("5m",  300),
    ("∞",   0),
)
_DEFAULT_DURATION_INDEX = 1  # 1m — same as the previous hardcoded behavior


class ConfirmAction(ModalScreen[Optional[int]]):
    """Modal confirm with optional duration picker for freeze actions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
        # Digit keys 1-5 pick a freeze duration. Harmless on other actions.
        Binding("1", "pick_duration(0)", show=False),
        Binding("2", "pick_duration(1)", show=False),
        Binding("3", "pick_duration(2)", show=False),
        Binding("4", "pick_duration(3)", show=False),
        Binding("5", "pick_duration(4)", show=False),
    ]

    countdown: reactive[int] = reactive(3)
    duration_index: reactive[int] = reactive(_DEFAULT_DURATION_INDEX)

    def __init__(self, action: str, row: ProcessRow):
        super().__init__()
        self.action = action
        self.row = row
        self._timer = None

    def compose(self) -> ComposeResult:
        title, desc = _ACTION_DESCRIPTION.get(self.action, (self.action, ""))
        warnings = []
        if self.action == "freeze":
            if self.row.holds_audio:
                warnings.append("This app holds an audio session — playback may need reinit on resume.")
            if self.row.holds_socket:
                warnings.append("This app holds a live WebSocket — server may time out at ~60s.")
        n = len(self.row.child_pids)
        if n > 1 and self.action in ("freeze", "calm"):
            warnings.append(
                f"This app has {n} processes (parent + {n - 1} helpers). "
                f"The action will be applied to all of them."
            )

        target_line = (
            f"[{theme.PALETTE['muted']}]Target:[/] [bold]{self.row.name}[/]  "
            + (
                f"[{theme.PALETTE['dim']}]{n} processes[/]"
                if n > 1 else
                f"[{theme.PALETTE['dim']}]pid {self.row.pid}[/]"
            )
        )

        children: list = [
            Static(f"[bold {theme.PALETTE['primary']}]{title}[/]"),
            Static(""),
            Static(target_line),
            Static(f"[{theme.PALETTE['fg']}]{desc}[/]"),
            Static(""),
        ]

        if self.action == "freeze":
            children.append(
                Static(
                    f"[{theme.PALETTE['muted']}]Auto-resume after  "
                    f"[/]{self._duration_picker_markup()}",
                    id="duration-picker",
                )
            )
            children.append(Static(""))

        for w in warnings:
            children.append(
                Static(f"[{theme.SEVERITY['warn']}]{theme.GLYPHS.icon_warn} {w}[/]")
            )
        if warnings:
            children.append(Static(""))

        children.append(Static(self._countdown_markup(), id="countdown"))
        children.append(Static(""))
        children.append(
            Center(
                Button("Cancel", id="cancel", variant="default"),
                Button("Confirm", id="confirm", variant="error", disabled=True),
            )
        )

        body = Vertical(*children, id="confirm-body")
        body.styles.width = 70
        body.styles.padding = (1, 2)
        body.styles.background = theme.PALETTE["bg_panel"]
        body.styles.border = ("heavy", theme.PALETTE["border"])
        yield Center(body)

    def on_mount(self) -> None:
        self._timer = self.set_interval(1.0, self._tick)

    def _duration_picker_markup(self) -> str:
        chips = []
        for i, (label, _seconds) in enumerate(_DURATION_OPTIONS):
            digit = i + 1
            if i == self.duration_index:
                chips.append(
                    f"[bold {theme.PALETTE['bg']} on {theme.PALETTE['primary']}]"
                    f" {digit} {label} [/]"
                )
            else:
                chips.append(
                    f"[{theme.PALETTE['muted']}] {digit} {label} [/]"
                )
        hint = (
            f"  [{theme.PALETTE['dim']}](press 1-5 to pick)[/]"
        )
        return "  ".join(chips) + hint

    def _countdown_markup(self) -> str:
        if self.countdown > 0:
            return (
                f"[{theme.PALETTE['muted']}]Confirm enables in[/] "
                f"[bold {theme.SEVERITY['warn']}]{self.countdown}…[/]"
            )
        return f"[bold {theme.SEVERITY['ok']}]Ready — press Enter or click Confirm.[/]"

    def watch_countdown(self, _old: int, _new: int) -> None:
        try:
            self.query_one("#countdown", Static).update(self._countdown_markup())
        except Exception:
            pass

    def watch_duration_index(self, _old: int, _new: int) -> None:
        try:
            self.query_one("#duration-picker", Static).update(
                f"[{theme.PALETTE['muted']}]Auto-resume after  [/]"
                + self._duration_picker_markup()
            )
        except Exception:
            pass

    def _tick(self) -> None:
        if self.countdown > 0:
            self.countdown -= 1
            if self.countdown == 0 and self._timer:
                self._timer.stop()
                try:
                    btn = self.query_one("#confirm", Button)
                    btn.disabled = False
                    btn.focus()
                except Exception:
                    pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm" and not event.button.disabled:
            self.dismiss(self._chosen_seconds())
        elif event.button.id == "cancel":
            self.dismiss(None)

    def action_confirm(self) -> None:
        try:
            btn = self.query_one("#confirm", Button)
            if not btn.disabled:
                self.dismiss(self._chosen_seconds())
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_pick_duration(self, idx: int) -> None:
        if self.action != "freeze":
            return
        idx = int(idx)
        if 0 <= idx < len(_DURATION_OPTIONS):
            self.duration_index = idx

    def _chosen_seconds(self) -> int:
        """Returns the chosen duration in seconds for freeze, or a sentinel
        non-zero int for other actions (the value is ignored by callers
        on non-freeze actions)."""
        if self.action != "freeze":
            return 1
        return _DURATION_OPTIONS[self.duration_index][1]
