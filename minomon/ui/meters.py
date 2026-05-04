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
    vibe_mode: reactive[bool] = reactive(False)

    def __init__(self, history_seconds: int = 60, **kwargs):
        super().__init__(**kwargs)
        self.history_seconds = history_seconds
        self._ram_history: Deque[float] = deque(maxlen=history_seconds)
        self._cpu_history: Deque[float] = deque(maxlen=history_seconds)
        self._gpu_history: Deque[float] = deque(maxlen=history_seconds)

    def watch_vibe_mode(self, _old: bool, _new: bool) -> None:
        # Force a re-render when mode flips
        self.refresh()

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

        # Memory breakdown — the centerpiece.
        # Pressure SEVERITY follows the kernel's pressure_level signal (NORMAL /
        # WARN / CRITICAL), NOT the percentage. macOS routinely runs at >80%
        # committed without pressure (lots of cached files, compressor idle),
        # and using the % to drive color produced the contradictory
        # "!! NORMAL (84%)" display.
        mem = s.memory
        sev_key = {
            "NORMAL":   "ok",
            "WARN":     "warn",
            "CRITICAL": "critical",
        }.get(mem.pressure_level, "info")

        # Top headline row
        pressure_color = theme.severity_color(sev_key)
        pressure_icon = {
            "ok":       theme.GLYPHS.icon_ok,
            "warn":     theme.GLYPHS.icon_warn,
            "critical": theme.GLYPHS.icon_critical,
            "info":     theme.GLYPHS.icon_info,
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

        # The breakdown is rendered against TOTAL = Used + Available, where
        # Used = App + Wired + Compressed (Activity Monitor's "Memory Used"
        # footer) and Available = Cached + Free. Cached files are shown
        # under Available because macOS reclaims them on demand without any
        # cost. This keeps the bar segments visually adding to ≤100% and
        # matches Apple's mental model.
        if self.vibe_mode:
            row("Apps",     mem.app_gb,        mem.total_gb, theme.SEVERITY["info"],
                "what your apps actually need")
            row("Squeezed", mem.compressed_gb, mem.total_gb, theme.SEVERITY["warn"],
                "macOS shrunk these to fit")
            row("Locked",   mem.wired_gb,      mem.total_gb, theme.PALETTE["pinned"],
                "OS internals — can't be moved")
            row("Cached",   mem.cached_gb,     mem.total_gb, theme.PALETTE["muted"],
                "free if needed — open files & disk cache")
            row("Free",     mem.free_gb,       mem.total_gb, theme.SEVERITY["ok"],
                "untouched, ready")
        else:
            row("App",   mem.app_gb,        mem.total_gb, theme.SEVERITY["info"],   "in use by apps")
            row("Comp",  mem.compressed_gb, mem.total_gb, theme.SEVERITY["warn"],   "kernel-compressed")
            row("Wired", mem.wired_gb,      mem.total_gb, theme.PALETTE["pinned"],  "kernel-locked")
            row("Cache", mem.cached_gb,     mem.total_gb, theme.PALETTE["muted"],   "reclaimable")
            row("Free",  mem.free_gb,       mem.total_gb, theme.SEVERITY["ok"],     "untouched")

        # Headline above the breakdown.
        # Two separate signals, never contradictory:
        #   - Pressure: kernel-reported state (the green/yellow/red bar in AM)
        #   - Used: how much of total RAM is currently committed to apps
        used_gb = mem.app_gb + mem.wired_gb + mem.compressed_gb
        used_pct = (used_gb / mem.total_gb) * 100 if mem.total_gb else 0
        headline = Text()
        if self.vibe_mode:
            headline.append("Memory pressure  ", style=f"bold {theme.PALETTE['fg_strong']}")
        else:
            headline.append("PRESSURE  ", style=f"bold {theme.PALETTE['fg_strong']}")
        headline.append(f"{pressure_icon} {mem.pressure_level.title()}", style=f"bold {pressure_color}")
        headline.append("    ", style=theme.PALETTE["muted"])
        used_label = "in use" if self.vibe_mode else "USED"
        headline.append(f"{used_label}  ", style=f"bold {theme.PALETTE['fg_strong']}")
        headline.append(
            f"{used_gb:.1f} / {mem.total_gb:.0f} GB ({used_pct:.0f}%)",
            style=theme.PALETTE["fg"],
        )
        if mem.swap_in_rate_mbps > 0.5 or mem.swap_out_rate_mbps > 0.5:
            headline.append(
                f"    swap  in {mem.swap_in_rate_mbps:.1f}  out {mem.swap_out_rate_mbps:.1f} MB/s",
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

        # Battery row — laptops only. Hidden on desktops or when the OS
        # doesn't expose battery sensors.
        bat = s.battery
        if bat.available:
            if bat.percent >= 60:
                bat_color = theme.SEVERITY["ok"]
            elif bat.percent >= 25:
                bat_color = theme.SEVERITY["warn"]
            else:
                bat_color = theme.SEVERITY["critical"]

            if bat.plugged_in:
                tail = (
                    f"[{theme.PALETTE['muted']}]plugged in"
                    f"{' · charging' if bat.percent < 100 else ' · full'}[/]"
                )
            elif bat.seconds_remaining is not None:
                hours = bat.seconds_remaining // 3600
                mins = (bat.seconds_remaining % 3600) // 60
                if hours:
                    tail = f"[{theme.PALETTE['muted']}]on battery · {hours}h {mins}m left[/]"
                else:
                    tail = f"[{theme.PALETTE['muted']}]on battery · {mins}m left[/]"
            else:
                tail = f"[{theme.PALETTE['muted']}]on battery · estimating…[/]"

            right.add_row(
                "",
                Text.from_markup(
                    f"[bold {theme.PALETTE['fg_strong']}]Battery[/] "
                    f"[bold {bat_color}]{bat.percent:>4.0f}%[/]   {tail}"
                ),
            )

        # Optional integration: a local app can publish a small JSON file
        # advertising its loaded models / activity. When the file is present
        # we render a one-line summary. When absent, the row is omitted
        # entirely — keeps the dashboard compact for users who don't use it.
        # See docs/integrations.md for the schema.
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

        body.add_row(left, right)
        return Panel(
            body,
            border_style=theme.PALETTE["border"],
            padding=(0, 1),
            title=f"[bold {theme.PALETTE['primary']}]live system[/]",
            title_align="left",
        )
