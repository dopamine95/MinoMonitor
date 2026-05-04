"""
3-second countdown confirm dialog. Shows the action, the target, the risks
(audio/socket flags), and counts down before enabling Confirm.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
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
               "Auto-resumes in 60 seconds. Safe for most apps; "
               "may briefly disconnect audio or live chat sessions on resume."),
    "thaw":   ("Resume this app",
               "Wakes a paused app right back up where it left off."),
    "quit":   ("Quit application",
               "Sends a graceful quit (osascript). Falls back to SIGTERM if it doesn't respond."),
}


class ConfirmAction(ModalScreen[bool]):
    """Returns True if confirmed, False if cancelled."""

    BINDINGS = [
        Binding("escape", "dismiss(False)", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    countdown: reactive[int] = reactive(3)

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
                warnings.append("This app holds an audio session — playback may need reinit on thaw.")
            if self.row.holds_socket:
                warnings.append("This app holds a live WebSocket — server may time out at ~60s.")
        # If the row groups several helpers, the action will fan out — make
        # that explicit so the user knows what they're agreeing to.
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

        body = Vertical(
            Static(f"[bold {theme.PALETTE['primary']}]{title}[/]"),
            Static(""),
            Static(target_line),
            Static(f"[{theme.PALETTE['fg']}]{desc}[/]"),
            Static(""),
            *[Static(f"[{theme.SEVERITY['warn']}]{theme.GLYPHS.icon_warn} {w}[/]") for w in warnings],
            Static(""),
            Static(self._countdown_markup(), id="countdown"),
            Static(""),
            Center(
                Button("Cancel", id="cancel", variant="default"),
                Button("Confirm", id="confirm", variant="error", disabled=True),
            ),
            id="confirm-body",
        )
        body.styles.width = 64
        body.styles.padding = (1, 2)
        body.styles.background = theme.PALETTE["bg_panel"]
        body.styles.border = ("heavy", theme.PALETTE["border"])
        yield Center(body)

    def on_mount(self) -> None:
        self._timer = self.set_interval(1.0, self._tick)

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
            self.dismiss(True)
        elif event.button.id == "cancel":
            self.dismiss(False)

    def action_confirm(self) -> None:
        try:
            btn = self.query_one("#confirm", Button)
            if not btn.disabled:
                self.dismiss(True)
        except Exception:
            pass
