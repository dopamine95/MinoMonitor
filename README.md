# Mino Monitor

A pressure-and-culprit monitor for Apple Silicon, with manual intervention.
Built for Amine's M1 Max running Cassie.

## Run

```bash
chmod +x ~/Developer/MinoMonitor/bin/minomon
~/Developer/MinoMonitor/bin/minomon
```

Optionally add an alias: `alias monitor="~/Developer/MinoMonitor/bin/minomon"`.

UI dev mode (no system access, fake oscillating data):

```bash
~/Developer/MinoMonitor/bin/minomon --stub
```

## What it shows

- Apple-Silicon-honest memory accounting: app, wired, compressed, cached, free,
  plus swap in/out *rates* and the kernel pressure level.
- CPU split into P-cores / E-cores, load average, sparkline history.
- Optional GPU/ANE/SoC-temp/fan via `powermetrics` (see below).
- Cassie awareness: reads `~/.cassie/status.json` and shows which models are
  loaded, whether a generation is in flight, and how long since the last turn.
- Top processes, sortable, with flags for audio/socket holders.
- Deterministic insights (rules-based, no LLM).

## What it does not do

- It does **not** "free RAM" by pausing apps. `kill -STOP` halts CPU; the
  pages stay resident. macOS's compressor and file-cache eviction do the
  actual reclaim. The honest value of pausing is reducing CPU/background
  churn under pressure.

## Action ladder (manual only in v1)

1. **Calm** (`c`) — `taskpolicy -b <pid>` (background QoS). Reversible, no
   socket damage. Default for almost everything.
2. **Freeze** (`f`) — `kill -STOP` with auto-`CONT` after 60 seconds. Shows a
   3-second confirm dialog. Warns if the target holds audio/sockets.
3. **Quit** (`q`-app) — graceful `osascript quit`, falls back to SIGTERM.

Pinned processes (system, terminals, Cassie, Xcode) cannot be acted on.

## Crash safety

Pause sentinels live at `~/.minomonitor/paused/<pid>_<start_unix>`. The
identity stamp prevents resuming a recycled PID.

To install the launchd watchdog (recommended, resumes orphan paused
processes if the monitor dies):

```bash
cp ~/Developer/MinoMonitor/contrib/com.threetrees.minomon-watchdog.plist \
   ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.threetrees.minomon-watchdog.plist
```

## Powermetrics (optional)

`powermetrics` needs `sudo`. To use the GPU/ANE/temp/fan panel, add a sudoers
rule for your user:

```
echo "$(whoami) ALL=(root) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/minomon-powermetrics
sudo chmod 440 /etc/sudoers.d/minomon-powermetrics
```

Then re-run the monitor. Without it, the GPU panel shows a "not enabled" hint.

## Files

- `~/.minomonitor/actions.log` — audit trail of every calm/freeze/quit.
- `~/.minomonitor/paused/` — sentinel files for currently STOP'd processes.
- `~/.minomonitor/monitor.pid` — current monitor PID (used by the watchdog).
- `~/.cassie/status.json` — Cassie writes this; the monitor reads it.

## Hotkeys

- `c` calm selected · `f` freeze (with confirm) · `u` uncalm or thaw
- `q` quit-app (with confirm) · `r` refresh · `?` help · Ctrl+C exit

## How it was built

A debate between Claude and Codex (with a parallel Plan reviewer) produced
v1, then v2 after critique. Codex built `data/`, `actions/`, and the
watchdog; Claude built `ui/`, `app.py`, the theme system, and the Cassie
status writer. See `DESIGN_v1.md` and `DESIGN_v2.md` for the design history.
