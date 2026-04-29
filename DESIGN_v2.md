# Mino Monitor — Design v2 (synthesized)

After critique from Codex (skeptical macOS engineer) and a Plan reviewer, the v1 was redesigned around two core corrections:

1. `kill -STOP` does **not** free RAM — pages stay mapped. Apple Silicon already compresses inactive memory aggressively. The honest value is **reducing CPU/background churn**, not "freeing 6.5 GB". The product is reframed accordingly.
2. The action surface needed a **gentler tier** (`taskpolicy -b`) between "do nothing" and "freeze the process." This is the right default for almost everything.

## What the tool actually does (revised)

A single-screen TUI dashboard for Apple Silicon with:
- **Honest memory accounting** — wired / compressed / file cache / app / free, plus swap in/out *rates*, plus kernel pressure level
- **Apple-Silicon specials** — GPU and ANE power via `powermetrics` (sudo, opt-in), SoC temp + fan via `powermetrics`/SMC
- **Cassie awareness** — reads `~/.cassie/status.json` so insights say "deep model loaded, idle 12 min" instead of "python3 using 15 GB"
- **A three-rung action ladder, never auto-applied in v1**:
  1. **Calm** — `taskpolicy -b <pid>` (background QoS; reversible; no socket damage)
  2. **Freeze** — `kill -STOP <pid>` with a forced auto-`CONT` timer (default 60s, max 5min)
  3. **Quit** — `osascript 'quit app'` for graceful shutdown
- **Crash-safe pause tracking** — sentinel file written *before* STOP, deleted after CONT; launchd watchdog agent resumes any orphan T-state PIDs if the monitor dies
- **Audit log** at `~/.minomonitor/actions.log`

`sudo purge` is deleted from the design entirely.

## Stack (locked)

- **Python 3.13** + **Textual** for UI. Textual stays over Rich-Live because we need clickable buttons, modal confirms, and a focusable process table. Rich-Live would punt those to keystrokes only — fine for read-only, painful for an action surface.
- **psutil** for process/memory sampling. `vm_stat`, `memory_pressure`, `lsappinfo` parsed in a 1-Hz async sampler, cached, top-N only.
- **`powermetrics`** behind a one-time sudoers entry, opt-in; if not available, the GPU/ANE/thermal panel shows "not enabled — see README".
- **Glyph fallback**: nerd-font glyphs detected once at startup; ASCII fallback if absent.

## Cassie awareness (new)

Cassie's server writes `~/.cassie/status.json` once per second:
```json
{
  "fast_loaded": true,
  "deep_loaded": true,
  "fast_resident_gb": 14.2,
  "deep_resident_gb": 12.8,
  "in_flight": false,
  "last_request_unix": 1730412345,
  "tts_in_flight": false
}
```
Monitor reads this each tick. Insights become Cassie-aware:
- "Deep model loaded but idle 12m — `CASSIE_DISABLE_DEEP=1` next restart would free ~13 GB"
- "Cassie is mid-generation — pausing other apps now will not affect this turn"

This is the single highest-leverage addition the Plan reviewer identified.

## Sampler design (perf)

Critique was right that walking every process every tick is sloppy. Fixed:
- Top-N (default 30) by RSS, refreshed every tick
- Static metadata (name, parent, bundle id) cached forever per PID
- `vm_stat`/`memory_pressure`/`lsappinfo` invoked once per tick total, results cached
- `powermetrics` (if enabled) is its own subprocess streaming `--samplers gpu_power,ane_power,thermal,smc -i 1000` — read its stdout, don't relaunch
- Frontmost-app detection from `lsappinfo` cached for 5s

## Deny list (expanded)

Hard-pinned, button greyed out:
```
kernel_task, WindowServer, Finder, Dock, SystemUIServer, launchd, loginwindow,
mds, mds_stores, mdworker, mdworker_shared, fseventsd, distnoted, cfprefsd,
runningboardd, backupd, bird, cloudd, nsurlsessiond, photolibraryd,
fileproviderd, sharingd, rapportd, coreduetd, locationd, tccd, coreaudiod,
bluetoothd, controlcenter, Cassie, cassie_server.py
```

Plus user's own terminal (auto-detected from `$TERM_PROGRAM`/parent PID chain) and Xcode (dev work).

## Audio/network warning

Even with the deny list, pausing user apps that hold:
- audio sessions (Spotify, Zoom, Discord, DAWs)
- live WebSockets (Slack, Discord)
- USB device claims

…can break them. The Freeze action shows a one-line warning before the 3-second confirm: *"Slack holds a WebSocket — server may time out at ~60s. Auto-resume in 60s."*

## Auto-mode (deferred to v2)

Plan reviewer wanted it; Codex said no. Compromise: **v1 ships manual-only**. After Amine has used it for a week and we have real telemetry, we add a single conservative auto-rule (CRITICAL pressure + ≥60m idle + not in audio/socket-active list, capped 2 actions/hour). Not now.

## Visual concept (revised — same idea, honest about glyphs)

```
╭─ Mino Monitor · M1 Max · 2026-04-29 12:43 ─────── q quit · ? help ─╮
│                                                                      │
│  RAM PRESSURE                          ⚠ WARN  (kernel: 78%)         │
│    App     ████████████░░░░░░  14.1 GB   Cassie fast model           │
│    Comp    █████░░░░░░░░░░░░░   6.8 GB   ← OS compressing under load │
│    Wired   ███░░░░░░░░░░░░░░░   3.9 GB                               │
│    Cache   ████████░░░░░░░░░░  12.0 GB                               │
│    Free                          0.4 GB   ← tight                    │
│    Swap I/O   in 4.2 MB/s   out 1.1 MB/s   ← actually moving         │
│                                                                      │
│  CPU  18%  (P:12 E:6)  load 3.4    GPU  52%  ANE  3%   SoC 62°C      │
│                                                                      │
├─ TOP PROCESSES (manual actions only) ───────────────────────────────┤
│  PID    NAME                  RAM    CPU   STATE      [ACTIONS]      │
│  ▶ 71820 python3 (cassie)    15.4G   28%   active     [pinned]      │
│    8231  Google Chrome        4.2G    8%   foreground [calm][stop]  │
│  ◐ 4421  Xcode                3.1G    1%   idle 23m   [calm][stop]  │
│  ◐ 9134  Slack                1.8G    0%   idle 47m   [calm][stop]  │
│  ◐ 9201  Discord              1.6G    0%   idle 2h    [calm][stop]  │
│    7811  Spotify              0.9G    3%   playing    [calm]        │
│                                                                      │
├─ INSIGHTS · rules-based, deterministic ─────────────────────────────┤
│  ⚠  Memory pressure WARN. Compressor moving 6.8 GB. Swap in 4 MB/s.  │
│      Calming Slack + Discord may reduce churn (not free RAM).         │
│      [Calm Slack]  [Calm Discord]  [Calm both]                       │
│  ●  Cassie deep model loaded · idle 14 min. Setting                  │
│     CASSIE_DISABLE_DEEP=1 on next restart frees ~13 GB.              │
│  ✓  No SIGSTOP'd processes orphaned from prior runs.                 │
╰──────────────────────────────────────────────────────────────────────╯
```

Honest framing throughout: "may reduce churn" not "frees X GB", swap rate shown explicitly, compressed memory called out.

## Final answers to v1's open questions

1. **Stack**: Textual + psutil. Locked.
2. **Auto-pause**: Manual-only v1. Conservative auto-mode in v2.
3. **Deny list**: see above, ~30 entries.
4. **Insights engine**: deterministic rules, no Cassie LLM call.
5. **Permission model**: no `purge`, no `sudo` for core function. `powermetrics` is the only privileged path and it's optional.
6. **Missing data**: now has compressed/wired/cache/swap-rate/GPU/ANE/temp/Cassie-status.
7. **Visual**: nerd-font detection + ASCII fallback; mockup uses simple block glyphs only.

## Split for the build

- **Codex**: data layer (`minomon/data/*`), action layer (`minomon/actions/*`), watchdog launchd agent. Knows the syscalls, low-level macOS, error handling.
- **Claude**: UI layer (`minomon/ui/*`, `app.py`), theme system, keybindings, modal dialogs, layout. Plus the Cassie status writer (10 lines added to `cassie_server.py`).

Both agree on the contract: data layer exposes async `Sampler` with a single `Sample` dataclass; UI subscribes via `reactive`/`watch`. No tight coupling beyond that contract.
