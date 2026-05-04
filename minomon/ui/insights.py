"""
Bottom panel — deterministic insights from the rules engine.

Each insight may carry inline action buttons. The panel is focusable so
the user can press a digit (1-9) or `a` to actually run the suggested
action — pure text "buttons" can't capture mouse clicks reliably across
terminals, but a keyboard shortcut works everywhere.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from ..data.sample import Insight, Sample
from . import theme


class InsightActionRequested(Message):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        super().__init__()


# Maximum number of inline insight actions the digit-key bindings cover.
# Keep aligned with len(DIGIT_BINDINGS) below.
_MAX_INSIGHT_ACTIONS = 9


class InsightsPanel(Static):
    sample: reactive[Sample | None] = reactive(None)
    vibe_mode: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Most-recent rendered list of (label, payload) for each numbered
        # action shown on screen, in the order the digit keys map to.
        # The App reads this when the user presses `a` or a digit.
        self.numbered_actions: list[tuple[str, dict]] = []

    def push(self, sample: Sample) -> None:
        self.sample = sample

    def watch_vibe_mode(self, _old: bool, _new: bool) -> None:
        self.refresh()

    def render(self) -> RenderableType:
        s = self.sample
        title = (
            f"[bold {theme.PALETTE['primary']}]"
            f"{'what is going on' if self.vibe_mode else 'insights · rules-based'}"
            f"[/]"
        )

        # Reset the numbered-action map each render so digit hotkeys always
        # match what's on screen.
        self.numbered_actions = []

        if s is None or not s.insights:
            return Panel(
                Text(
                    "Looks calm. Nothing needs your attention." if self.vibe_mode
                    else "No insights yet.",
                    style=theme.PALETTE["muted"],
                ),
                border_style=theme.PALETTE["border"],
                title=title,
                title_align="left",
                padding=(0, 1),
            )

        renderables: list = []
        for insight in s.insights:
            renderables.append(self._render_one(insight))

        # Show the keyboard hint at the bottom only if there are runnable actions
        if self.numbered_actions:
            hint = Text.from_markup(
                f"  [{theme.PALETTE['muted']}]Press [bold]a[/] to apply the first "
                f"action, or a number key (1-{len(self.numbered_actions)}) "
                f"to apply a specific one.[/]"
            )
            renderables.append(hint)

        return Panel(
            Group(*renderables),
            border_style=theme.PALETTE["border"],
            title=title,
            title_align="left",
            padding=(0, 1),
        )

    def _render_one(self, insight: Insight) -> RenderableType:
        icon = {
            "ok":       theme.GLYPHS.icon_ok,
            "info":     theme.GLYPHS.icon_info,
            "warn":     theme.GLYPHS.icon_warn,
            "critical": theme.GLYPHS.icon_critical,
        }.get(insight.severity, theme.GLYPHS.icon_info)

        color = theme.severity_color(insight.severity)

        line = Text()
        line.append(f"{icon}  ", style=f"bold {color}")
        line.append(insight.message, style=theme.PALETTE["fg"])

        if not insight.actions:
            return line

        # Stamp each action with a digit key (1..N), wrap-stop at _MAX.
        labelled = []
        for label, payload in insight.actions:
            if len(self.numbered_actions) >= _MAX_INSIGHT_ACTIONS:
                break
            digit = len(self.numbered_actions) + 1
            self.numbered_actions.append((label, payload))
            labelled.append((digit, label))

        actions_row = Table.grid(padding=(0, 2))
        for _ in labelled:
            actions_row.add_column()
        actions_row.add_row(
            *[
                Text.from_markup(
                    f"  [bold {theme.PALETTE['bg']} on {color}] {digit}  {label} [/]"
                )
                for digit, label in labelled
            ]
        )

        return Group(line, actions_row)

    # The App owns the keybindings (`a` and digits) so they fire regardless
    # of which widget has focus. The App reads numbered_actions from this
    # panel and posts InsightActionRequested itself.
