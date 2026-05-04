"""
One-shot text snapshot of the current system state.

Used by `minomon --snapshot`. No TUI, no animation — boots the sampler,
waits one tick, prints a paste-friendly summary, exits. Useful for:

- Sharing in Slack / GitHub issues when something is misbehaving
- Cron / launchd jobs that log state every N minutes
- Quickly answering "what's eating RAM right now?" without entering an
  interactive view
"""

from __future__ import annotations

import asyncio
import platform
import socket
import sys
from datetime import datetime
from io import StringIO

from rich.console import Console

from .data.sample import Sample
from .data.sampler import Sampler


def _bar(pct: float, width: int = 24) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round((pct / 100.0) * width))
    return "█" * filled + "·" * (width - filled)


def _fmt_gb(g: float) -> str:
    if g < 1.0:
        return f"{g * 1024:>5.0f} MB"
    return f"{g:>5.1f} GB"


def _fmt_delta(g: float | None) -> str:
    if g is None:
        return "    —"
    if abs(g) < 0.01:
        return "   ±0"
    return f"{g:+5.2f}"


def render_snapshot(sample: Sample, top_n: int = 12, use_color: bool = True) -> str:
    """Format a Sample as plain text. Returns the rendered string so the
    caller can choose to print, write to a file, copy to clipboard, etc."""
    sink = StringIO()
    console = Console(file=sink, force_terminal=use_color, width=110, no_color=not use_color)
    m = sample.memory
    used_gb = m.app_gb + m.wired_gb + m.compressed_gb
    avail_gb = m.cached_gb + m.free_gb
    used_pct = (used_gb / m.total_gb) * 100 if m.total_gb else 0

    host = socket.gethostname()
    osver = platform.mac_ver()[0] or platform.platform()
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    pressure_color = {
        "NORMAL":   "green",
        "WARN":     "yellow",
        "CRITICAL": "red",
    }.get(m.pressure_level, "white")

    # Header
    console.print(
        f"[bold cyan]Mino Monitor snapshot[/]  ·  [white]{host}[/]  ·  "
        f"[white]macOS {osver}[/]  ·  [white]{when}[/]"
    )
    console.print()

    # Memory
    console.print(
        f"[bold]Memory[/]    pressure [bold {pressure_color}]{m.pressure_level}[/]   "
        f"used [bold]{used_gb:.1f}[/] / {m.total_gb:.0f} GB  ({used_pct:.0f}%)   "
        f"avail [white]{avail_gb:.1f} GB[/]"
    )
    if m.swap_in_rate_mbps > 0.5 or m.swap_out_rate_mbps > 0.5:
        console.print(
            f"           [yellow]swap[/]  in {m.swap_in_rate_mbps:.1f} MB/s   "
            f"out {m.swap_out_rate_mbps:.1f} MB/s"
        )
    console.print(
        f"           App   {_bar(m.app_gb / m.total_gb * 100)}  {_fmt_gb(m.app_gb)}"
    )
    console.print(
        f"           Wired {_bar(m.wired_gb / m.total_gb * 100)}  {_fmt_gb(m.wired_gb)}"
    )
    console.print(
        f"           Comp  {_bar(m.compressed_gb / m.total_gb * 100)}  {_fmt_gb(m.compressed_gb)}"
    )
    console.print(
        f"           Cache {_bar(m.cached_gb / m.total_gb * 100)}  {_fmt_gb(m.cached_gb)}"
    )
    console.print(
        f"           Free  {_bar(m.free_gb / m.total_gb * 100)}  {_fmt_gb(m.free_gb)}"
    )
    console.print()

    # CPU + GPU + Cassie
    cpu = sample.cpu
    console.print(
        f"[bold]CPU[/]       total [bold]{cpu.total_pct:.1f}%[/]   "
        f"P {cpu.perf_pct:.1f}%   E {cpu.eff_pct:.1f}%   load {cpu.load_avg_1:.2f}"
    )
    gpu = sample.gpu
    if gpu.powermetrics_available:
        console.print(
            f"[bold]GPU[/]       gpu {gpu.gpu_pct:.1f}%   ane {gpu.ane_pct:.1f}%   "
            f"temp {gpu.soc_temp_c:.0f}°C   fan {gpu.fan_rpm} rpm"
        )
    if sample.battery.available:
        b = sample.battery
        if b.plugged_in:
            tail = "plugged in" + (" · charging" if b.percent < 100 else " · full")
        elif b.seconds_remaining is not None:
            hrs, rem = divmod(b.seconds_remaining, 3600)
            mins = rem // 60
            tail = f"on battery · {hrs}h {mins}m left" if hrs else f"on battery · {mins}m left"
        else:
            tail = "on battery"
        console.print(f"[bold]Battery[/]   {b.percent:.0f}%   {tail}")
    if sample.cassie.available:
        c = sample.cassie
        chunks = []
        if c.fast_loaded:
            chunks.append(f"fast {c.fast_resident_gb:.1f}G")
        if c.deep_loaded:
            chunks.append(f"deep {c.deep_resident_gb:.1f}G")
        activity = "generating…" if c.in_flight else f"idle {c.seconds_idle // 60}m"
        console.print(
            f"[bold]Cassie[/]    " + "   ".join(chunks) + f"   {activity}"
        )
    console.print()

    # Top processes
    console.print(f"[bold]Top {top_n} processes[/]   "
                  f"(per-process is phys_footprint — same as Activity Monitor)")
    console.print(
        f"  {'NAME':<42}{'MEM':>10}{'Δ 1m':>9}{'Δ 5m':>9}{'CPU':>7}  STATE"
    )
    for row in sample.processes[:top_n]:
        name = row.name[:42]
        cpu_pct = f"{row.cpu_pct:.1f}%"
        state = row.state
        if row.pinned:
            state = f"{state}*"
        console.print(
            f"  {name:<42}{_fmt_gb(row.rss_gb):>10}"
            f"{_fmt_delta(row.delta_1m_gb):>9}"
            f"{_fmt_delta(row.delta_5m_gb):>9}"
            f"{cpu_pct:>7}  {state}"
        )
    console.print()

    # Insights
    actionable = [i for i in sample.insights if i.severity != "ok"]
    if actionable:
        console.print("[bold]Insights[/]")
        for ins in actionable:
            tag = {
                "info":     "[blue]ⓘ[/]",
                "warn":     "[yellow]![/]",
                "critical": "[red]‼[/]",
            }.get(ins.severity, " ")
            console.print(f"  {tag}  {ins.message}")
        console.print()

    return sink.getvalue()


async def _capture(top_n: int) -> Sample:
    """Boot the sampler, take one stable reading, stop. We wait two ticks
    so CPU percentages and swap rates have a baseline (psutil's first
    cpu_percent call returns 0.0 by definition)."""
    sampler = Sampler(top_n=max(top_n, 30))
    await sampler.start()
    try:
        # First sample is collected immediately on start; wait a moment
        # so CPU/swap rates have a meaningful delta.
        await asyncio.sleep(2.0)
        if sampler.latest is None:
            await asyncio.sleep(1.0)
        if sampler.latest is None:
            raise RuntimeError("Sampler produced no data within 3 seconds.")
        return sampler.latest
    finally:
        await sampler.stop()


def run_snapshot(top_n: int = 12, use_color: bool | None = None) -> int:
    """Entry point used by __main__. Returns a process exit code."""
    if use_color is None:
        use_color = sys.stdout.isatty()
    try:
        sample = asyncio.run(_capture(top_n))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"snapshot failed: {e}", file=sys.stderr)
        return 1

    output = render_snapshot(sample, top_n=top_n, use_color=use_color)
    sys.stdout.write(output)
    return 0
