"""
The header strip: four big meters (RAM detail, CPU, GPU/ANE, Cassie status)
with sparklines drawn from the sampler history.
"""

from __future__ import annotations

from collections import deque
from typing import Deque

from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from ..data.sample import Sample
from . import theme


class MetersPanel(Static):
    """Top header section. Updates by being handed each new Sample."""

    sample: reactive[Sample | None] = reactive(None)

    def __init__(self, history_seconds: int = 60, **kwargs):
        super().__init__(**kwargs)
        self.history_seconds = history_seconds
        self._ram_history: Deque[float] = deque(maxlen=history_seconds)
        self._cpu_history: Deque[float] = deque(maxlen=history_seconds)
        self._gpu_history: Deque[float] = deque(maxlen=history_seconds)

    def push(self, sample: Sample) -> None:
        """Sampler hands us each tick."""
        self._ram_history.append(sample.memory.pressure_pct)
        self._cpu_history.append(sample.cpu.total_pct)
        self._gpu_history.append(sample.gpu.gpu_pct if sample.gpu.powermetrics_available else 0)
        self.sample = sample

    def render(self) -> RenderableType:
        s = self.sample
        if s is None:
            return Panel("Loading…", border_style=theme.PALETTE["border"])

        # Memory breakdown — the centerpiece
        mem = s.memory
        ram_sev = mem.pressure_level.lower() if mem.pressure_level != "NORMAL" else "ok"
        if ram_sev == "warn":
            sev_key = "warn"
        elif ram_sev == "critical":
            sev_key = "critical"
        else:
            sev_key = theme.severity_for_pct(mem.pressure_pct)

        # Top headline row
        pressure_color = theme.severity_color(sev_key)
        pressure_icon = {
            "ok": theme.GLYPHS.icon_ok,
            "warn": theme.GLYPHS.icon_warn,
            "critical": theme.GLYPHS.icon_critical,
            "info": theme.GLYPHS.icon_info,
        }[sev_key]

        spark_ram = theme.make_sparkline(list(self._ram_history), max_value=100)
        spark_cpu = theme.make_sparkline(list(self._cpu_history), max_value=100)
        spark_gpu = theme.make_sparkline(list(self._gpu_history), max_value=100)

        # Build a 2-column table: left = breakdown, right = sparklines + extras
        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(ratio=2)
        body.add_column(ratio=1)

        # Left column: memory breakdown rows
        breakdown = Table.grid(padding=(0, 0))
        breakdown.add_column(width=8)
        breakdown.add_column(width=34)
        breakdown.add_column(width=14)
        breakdown.add_column(no_wrap=True)

        def row(label: str, gb: float, total: float, color: str, note: str = ""):
            pct = (gb / total) * 100 if total else 0
            bar = theme.make_bar(pct, width=24, severity=None)
            # Override color of the fill
            bar = bar.replace(theme.severity_color(theme.severity_for_pct(pct)), color)
            breakdown.add_row(
                f"[{theme.PALETTE['muted']}]{label}[/]",
                bar,
                f"[{theme.PALETTE['fg']}]{theme.fmt_gb(gb)}[/]",
                f"[{theme.PALETTE['dim']}]{note}[/]" if note else "",
            )

        row("App",      mem.app_gb,        mem.total_gb, theme.SEVERITY["info"])
        row("Comp",     mem.compressed_gb, mem.total_gb, theme.SEVERITY["warn"], "← OS compressing")
        row("Wired",    mem.wired_gb,      mem.total_gb, theme.PALETTE["pinned"])
        row("Cache",    mem.cached_gb,     mem.total_gb, theme.PALETTE["muted"])
        row("Free",     mem.free_gb,       mem.total_gb, theme.SEVERITY["ok"],
            "← tight" if mem.free_gb < 1.0 else "")

        # Headline above the breakdown
        headline = Text()
        headline.append("RAM PRESSURE  ", style=f"bold {theme.PALETTE['fg_strong']}")
        headline.append(f"{pressure_icon} {mem.pressure_level}", style=f"bold {pressure_color}")
        headline.append(f"   ({mem.pressure_pct}%)", style=theme.PALETTE["muted"])
        if mem.swap_in_rate_mbps > 0.5 or mem.swap_out_rate_mbps > 0.5:
            headline.append(
                f"   swap: in {mem.swap_in_rate_mbps:.1f} MB/s  out {mem.swap_out_rate_mbps:.1f} MB/s",
                style=theme.SEVERITY["warn"],
            )

        left = Table.grid()
        left.add_row(headline)
        left.add_row(breakdown)

        # Right column: CPU/GPU/Cassie compact status with sparklines
        right = Table.grid(padding=(0, 0))
        right.add_column(width=10)
        right.add_column()

        cpu_sev = theme.severity_for_pct(s.cpu.total_pct)
        cpu_line = Text.from_markup(
            f"[bold {theme.PALETTE['fg_strong']}]CPU[/] "
            f"[{theme.severity_color(cpu_sev)}]{s.cpu.total_pct:>5.1f}%[/]  "
            f"[{theme.PALETTE['muted']}]P[/]{s.cpu.perf_pct:>4.1f}  "
            f"[{theme.PALETTE['muted']}]E[/]{s.cpu.eff_pct:>4.1f}  "
            f"[{theme.PALETTE['muted']}]load[/] {s.cpu.load_avg_1:.2f}"
        )
        right.add_row(
            f"[{theme.severity_color(cpu_sev)}]{spark_cpu[-10:]:>10}[/]",
            cpu_line,
        )

        if s.gpu.powermetrics_available:
            gpu_sev = theme.severity_for_pct(s.gpu.gpu_pct)
            gpu_line = Text.from_markup(
                f"[bold {theme.PALETTE['fg_strong']}]GPU[/] "
                f"[{theme.severity_color(gpu_sev)}]{s.gpu.gpu_pct:>5.1f}%[/]  "
                f"[{theme.PALETTE['muted']}]ANE[/] {s.gpu.ane_pct:>4.1f}%  "
                f"{theme.GLYPHS.icon_thermo} {s.gpu.soc_temp_c:>4.1f}°C  "
                f"[{theme.PALETTE['muted']}]fan[/] {s.gpu.fan_rpm}"
            )
            right.add_row(
                f"[{theme.severity_color(gpu_sev)}]{spark_gpu[-10:]:>10}[/]",
                gpu_line,
            )
        else:
            right.add_row(
                "",
                Text.from_markup(
                    f"[{theme.PALETTE['muted']}]GPU/ANE/temp · run[/] "
                    f"[bold]minomon enable-powermetrics[/] "
                    f"[{theme.PALETTE['muted']}]for telemetry[/]"
                ),
            )

        cassie = s.cassie
        if cassie.available:
            stat_chunks = []
            if cassie.fast_loaded:
                stat_chunks.append(
                    f"[{theme.SEVERITY['ok']}]fast[/] "
                    f"[{theme.PALETTE['muted']}]{cassie.fast_resident_gb:.1f}G[/]"
                )
            if cassie.deep_loaded:
                stat_chunks.append(
                    f"[{theme.SEVERITY['info']}]deep[/] "
                    f"[{theme.PALETTE['muted']}]{cassie.deep_resident_gb:.1f}G[/]"
                )
            activity = (
                f"[{theme.SEVERITY['warn']}]generating…[/]"
                if cassie.in_flight else
                f"[{theme.PALETTE['muted']}]{theme.fmt_idle(cassie.seconds_idle)}[/]"
            )
            cassie_line = Text.from_markup(
                f"{theme.GLYPHS.icon_brain} "
                f"[bold {theme.PALETTE['primary']}]Cassie[/]  "
                + "  ".join(stat_chunks)
                + f"   {activity}"
            )
            right.add_row("", cassie_line)
        else:
            right.add_row(
                "",
                Text.from_markup(
                    f"{theme.GLYPHS.icon_brain} "
                    f"[{theme.PALETTE['muted']}]Cassie status not reporting[/]"
                ),
            )

        body.add_row(left, right)
        return Panel(
            body,
            border_style=theme.PALETTE["border"],
            padding=(0, 1),
            title=f"[bold {theme.PALETTE['primary']}]live system[/]",
            title_align="left",
        )
