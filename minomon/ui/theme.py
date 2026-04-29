"""
Theme and visual primitives for Mino Monitor.

A small, considered palette so the UI feels designed, not slapped together:
- Severity colors that look good in both warm-light and dark terminals
- Block-character meter bars with severity-aware gradient fill
- Glyph fallback: nerd-font detected once at startup, otherwise plain ASCII
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# Severity palette. Hex values are tuned so the green→amber→red gradient
# reads as "calm → uneasy → alarm" without being neon.
SEVERITY = {
    "ok":       "#10b981",   # emerald-500
    "info":     "#22d3ee",   # cyan-400
    "warn":     "#f59e0b",   # amber-500
    "critical": "#ef4444",   # red-500
}

PALETTE = {
    "primary":   "#7dd3fc",   # sky-300, the headline accent
    "muted":     "#64748b",   # slate-500
    "dim":       "#475569",   # slate-600
    "fg":        "#f1f5f9",   # slate-100, body text
    "fg_strong": "#ffffff",
    "bg":        "#0b0e14",   # near-black, slight blue cast
    "bg_panel":  "#111827",   # gray-900
    "bg_row":    "#1f2937",   # gray-800, alt rows
    "border":    "#334155",   # slate-700
    "pinned":    "#a78bfa",   # violet-400 — pinned/protected glow
    "active":    "#34d399",   # emerald-400 — frontmost dot
}


@dataclass
class GlyphSet:
    """Either nerd-font flavored or pure ASCII. Detected once."""
    bar_full: str
    bar_part: str          # half-block when bar fill is 50%
    bar_empty: str
    sparkline: tuple[str, ...]   # 8 levels for the sparkline bands
    dot_active: str
    dot_idle: str
    dot_pinned: str
    icon_warn: str
    icon_critical: str
    icon_info: str
    icon_ok: str
    icon_brain: str
    icon_chip: str
    icon_thermo: str


_NERD = GlyphSet(
    bar_full="█",
    bar_part="▌",
    bar_empty=" ",
    sparkline=("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"),
    dot_active="●",
    dot_idle="◐",
    dot_pinned="",
    icon_warn="",
    icon_critical="",
    icon_info="",
    icon_ok="",
    icon_brain="",
    icon_chip="",
    icon_thermo="",
)


_ASCII = GlyphSet(
    bar_full="#",
    bar_part="=",
    bar_empty=" ",
    sparkline=("_", ".", ":", "-", "=", "+", "*", "#"),
    dot_active="*",
    dot_idle="o",
    dot_pinned="P",
    icon_warn="!",
    icon_critical="!!",
    icon_info="i",
    icon_ok="v",
    icon_brain="C",
    icon_chip="G",
    icon_thermo="T",
)


def detect_glyphs() -> GlyphSet:
    """Best-effort nerd-font detection. Override with MINOMON_ASCII=1."""
    if os.environ.get("MINOMON_ASCII"):
        return _ASCII
    # WezTerm, Kitty, Alacritty, iTerm2 all advertise themselves; Apple
    # Terminal does not — fall back to ASCII there to be safe.
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program in ("wezterm", "kitty", "alacritty", "iterm.app", "ghostty"):
        return _NERD
    return _ASCII


GLYPHS = detect_glyphs()


def severity_for_pct(pct: float) -> str:
    if pct < 60:
        return "ok"
    if pct < 80:
        return "warn"
    return "critical"


def severity_color(severity: str) -> str:
    return SEVERITY.get(severity, PALETTE["fg"])


def make_bar(pct: float, width: int = 30, severity: str | None = None) -> str:
    """Block-character horizontal bar with Rich-style markup."""
    pct = max(0.0, min(100.0, pct))
    sev = severity or severity_for_pct(pct)
    color = severity_color(sev)
    filled_cells = (pct / 100.0) * width
    full = int(filled_cells)
    half = (filled_cells - full) >= 0.5
    if full > width:
        full, half = width, False
    fill = GLYPHS.bar_full * full + (GLYPHS.bar_part if half and full < width else "")
    empty_cells = width - full - (1 if half and full < width else 0)
    empty = GLYPHS.bar_empty * max(0, empty_cells)
    track = f"[{PALETTE['dim']}]{empty}[/]"
    return f"[{color}]{fill}[/]{track}"


def make_sparkline(values: list[float], max_value: float | None = None) -> str:
    """8-band sparkline. values can be any positive floats."""
    if not values:
        return ""
    top = max_value if max_value is not None else max(values) or 1.0
    bands = GLYPHS.sparkline
    out = []
    for v in values:
        idx = int((v / top) * (len(bands) - 1))
        idx = max(0, min(len(bands) - 1, idx))
        out.append(bands[idx])
    return "".join(out)


def fmt_gb(g: float) -> str:
    if g < 1.0:
        return f"{g * 1024:.0f} MB"
    return f"{g:.1f} GB"


def fmt_idle(seconds: int) -> str:
    if seconds < 60:
        return "active"
    if seconds < 3600:
        return f"idle {seconds // 60}m"
    if seconds < 86400:
        return f"idle {seconds // 3600}h"
    return f"idle {seconds // 86400}d"
