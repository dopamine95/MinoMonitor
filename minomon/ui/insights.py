"""
Bottom panel — deterministic insights from the rules engine.
Each insight may carry inline action buttons.
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


class InsightsPanel(Static):
    sample: reactive[Sample | None] = reactive(None)
    vibe_mode: reactive[bool] = reactive(False)

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

        actions_row = Table.grid(padding=(0, 2))
        for label, _payload in insight.actions:
            actions_row.add_column()
        actions_row.add_row(
            *[
                Text.from_markup(
                    f"  [bold {theme.PALETTE['bg']} on {color}] {label} [/]"
                )
                for label, _ in insight.actions
            ]
        )

        return Group(line, actions_row)
