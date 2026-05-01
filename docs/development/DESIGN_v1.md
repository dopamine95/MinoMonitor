# Mino Monitor — Design v1 (Claude proposal)

A real-time RAM/CPU monitor for Amine's M1 Max with the ability to actively reclaim memory by suspending background apps. Built because Cassie's two-model setup leaves a tight headroom and Amine wants visibility plus active management.

## Stack — chosen, defended

**Language**: Python 3.13 (already on the box)
**UI**: Textual (modern TUI; mouse + keyboard; gorgeous out of the box; fast iteration)
**Data**: `psutil` (cross-platform process metrics) + macOS-specific shellouts:
  - `vm_stat` → page-level memory pressure
  - `memory_pressure` → kernel-reported pressure level
  - `lsappinfo front` / `osascript` → frontmost / foreground app
  - `kill -STOP <pid>` / `kill -CONT <pid>` → suspend/resume
  - `sudo purge` → drop inactive memory (optional, behind confirmation)

**Why TUI over native macOS app**:
1. Ships in 200 lines, not 2,000
2. Runs over SSH if Amine ever wants to monitor remotely
3. Textual makes it look genuinely cool (true-color gradients, sparklines, mouse, modal dialogs)
4. Native is a week of Xcode work for the same outcome

## Visual concept

```
╭─ 󰍛 Mino Monitor · darwin · 2026-04-29 12:43 ──── q quit · p pause · ? help ─╮
│                                                                              │
│  RAM   ████████████████████░░░░░░░░░░  62.3 %   39.8 / 64.0 GB   ⚠ HIGH      │
│        ▁▂▂▃▄▅▆▇█▇▇▆▅▄▄▄▅▆▇▇█▇▆▅▄  60s history                                │
│                                                                              │
│  CPU   ███░░░░░░░░░░░░░░░░░░░░░░░░░░  18.4 %   P:12% E: 6%   load 3.4       │
│        ▁▁▂▂▁▂▃▃▂▁▁▁▂▃▄▃▂▂▃▄▅▄▃▂▁                                             │
│                                                                              │
│  GPU   ████████████░░░░░░░░░░░░░░░░░  52.0 %   ← Cassie generating          │
│  SWAP  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░  0.0 GB    pressure: NORMAL              │
│                                                                              │
├─ TOP PROCESSES ──────────────────────────────────────────────────────── 12 ─┤
│  ● python3 (cassie_server)       15.4 GB   28% CPU   active     [pinned]    │
│    Google Chrome Helper           4.2 GB    8% CPU   foreground             │
│  ◐ Xcode                          3.1 GB    1% CPU   idle 23m   [pause]     │
│  ◐ Slack                          1.8 GB    0% CPU   idle 47m   [pause]     │
│  ◐ Discord                        1.6 GB    0% CPU   idle 2h    [pause]     │
│  ○ Spotify                        0.9 GB    3% CPU   playing                │
│    ...                                                                       │
├─ INSIGHTS ──────────────────────────────────────────────────────────────────┤
│  💡 3 idle apps holding 6.5 GB combined. Pausing them frees 10% RAM.         │
│  💡 [Reclaim 6.5 GB]  [Pause Slack only]  [Pause Discord only]              │
│  ✓  Swap pressure is normal.                                                │
│  ⚠  Cassie deep model + chrome + xcode = will hit yellow if you open one    │
│     more 27B-class workload.                                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

Color system:
- Green ≤ 60%, Amber 60–80%, Red 80%+. Sparklines use the same palette.
- Frontmost app → bright cyan dot; idle → dimmed dot; pinned (Cassie) → pin glyph.
- Fixed-width monospace nerd-font glyphs for icons (Amine's terminal already has it).

## Process states

- **active** — frontmost or has had user input < 5s
- **foreground** — visible window, but not frontmost
- **idle Xm** — no input/CPU activity for X minutes
- **playing** — Spotify-class media (special-cased, never auto-pause)
- **pinned** — user-protected from any auto action (Cassie, Finder, WindowServer, kernel_task)

## Pinned-by-default deny list (never pause, never recommend pausing)

`kernel_task`, `WindowServer`, `Finder`, `Dock`, `SystemUIServer`, `mds`, `mdworker`,
`launchd`, `loginwindow`, `coreaudiod`, `bluetoothd`, `controlcenter`, `Cassie`,
`cassie_server.py`, `Xcode` (dev work), `Terminal`/`iTerm2` (the one Mino is using).

## Action surface (what user can actually trigger)

1. **Pause selected** (`p` or click `[pause]`) → `kill -STOP <pid>` after a 3-second confirm dialog
2. **Resume selected** (`r`) → `kill -CONT <pid>`
3. **Resume all paused** (`R`) → blanket
4. **Apply insight** → bundles multiple pauses into one action
5. **Purge inactive memory** (`P`, behind sudo prompt) → `sudo purge`
6. **Show full process tree** (`t`) → modal with all procs, sortable
7. **Quit selected** (`Q`, behind a "are you sure" modal) → `osascript quit` for nice shutdown, fallback `kill -TERM`

Auto-pause is **off by default**. Auto-pause mode would need a separate "Auto-manage" toggle that we can ship later.

## Insight engine (rules, not LLM)

Keep it deterministic — no Cassie API calls in the hot loop. Rules:

1. **Idle-RAM holders**: any non-pinned app with idle ≥ 30 min and ≥ 500 MB → suggest pause.
2. **Memory pressure escalation**: when `memory_pressure` reports `WARN` or `CRITICAL`, surface the top 3 idle holders as a one-click bundle.
3. **Cassie load forecast**: if fast model + deep model + frontmost are all live and free RAM < 8 GB, warn that the next chat could swap.
4. **Swap-already-spilling**: if swap > 1 GB, raise red banner with "purge + pause biggest 3" recommendation.
5. **Chrome tab discount**: Chrome's per-tab helpers are already accounted for, but show parent only by default — drill-down on demand.

## Safety model

- **Never auto-suspend without explicit Apply click.** Even rule-based insights only suggest.
- **Never SIGKILL.** Only STOP/CONT, or for "quit" a graceful AppleScript quit.
- **Pinned list is a hard block** — UI greys out the pause button on pinned procs.
- **Confirmation for destructive actions**: 3-second countdown dialog with "Cancel" focus.
- **Audit log**: every pause/resume/quit appended to `~/.minomonitor/actions.log` so Amine can see what happened.
- **Crash-resume safety**: on startup, scan for stopped processes that we paused and resume them if monitor exited uncleanly. (We track our own pauses in `~/.minomonitor/state.json`.)

## File layout

```
~/Developer/MinoMonitor/
├── README.md
├── pyproject.toml             # textual, psutil, rich pinned
├── minomon/
│   ├── __init__.py
│   ├── __main__.py             # entrypoint: python -m minomon
│   ├── app.py                  # Textual App + screen wiring
│   ├── ui/
│   │   ├── header.py           # title bar
│   │   ├── meters.py           # the four big bars (RAM/CPU/GPU/SWAP)
│   │   ├── processes.py        # process table + action buttons
│   │   ├── insights.py         # bottom panel
│   │   └── theme.py            # colors, glyphs, severity palette
│   ├── data/
│   │   ├── sampler.py          # background asyncio task pulling psutil
│   │   ├── macos.py            # vm_stat / memory_pressure / lsappinfo wrappers
│   │   ├── pinned.py           # deny list + user pin/unpin
│   │   └── insights.py         # rules engine
│   └── actions/
│       ├── suspend.py          # STOP/CONT with state tracking
│       ├── purge.py            # sudo purge
│       └── quit.py             # graceful quit
└── bin/cassie-mon              # shell shim that exec's python -m minomon
```

## Open questions for Codex

1. **Stack**: do you push back on Textual? Anything you'd swap?
2. **Auto-pause**: am I right to make it manual-only by default, or should we ship a conservative auto-mode?
3. **Suspending apps via SIGSTOP**: any apps you'd add to the deny list that I'm missing? Risks I'm not seeing? (e.g. apps that hold network sockets that timeout, audio sessions, etc.)
4. **Insight engine**: rules-based vs let Cassie generate them on demand?
5. **Permission model**: `purge` needs sudo — handle with osascript admin prompt, hardcoded sudoers entry, or skip entirely?
6. **macOS-specific data we're missing**: anything beyond `vm_stat`/`memory_pressure`/`lsappinfo`?
7. **Visual concept**: anything that's going to look bad in a real terminal vs my mockup?
