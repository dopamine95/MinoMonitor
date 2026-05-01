# Mino Monitor

A terminal dashboard for **Apple Silicon Macs** that shows what's actually
happening with your memory, lets you intervene without nuking apps, and is
honest about what it can and can't do.

Per-process numbers come from `proc_pid_rusage` — the same `phys_footprint`
ledger Activity Monitor's "Memory" column uses, verified against
[`footprint(1)`](https://manp.gs/mac/1/footprint). System totals match
Activity Monitor's Memory tab footer (App / Wired / Compressed / Cached /
Free, plus swap rates and the kernel-derived pressure level).

```
╭─ live system ─────────────────────────────────────────────────────────────╮
│  PRESSURE  ✓ Normal    USED  53.7 / 64 GB (84%)                           │
│                                                                           │
│  App   ███████████████░░░░░░  47.5 GB   in use by apps                    │
│  Comp  █░░░░░░░░░░░░░░░░░░░    1.7 GB   kernel-compressed                 │
│  Wired ██░░░░░░░░░░░░░░░░░░    4.4 GB   kernel-locked                     │
│  Cache █████░░░░░░░░░░░░░░░   10.2 GB   reclaimable                       │
│  Free  ░░░░░░░░░░░░░░░░░░░░    0.1 GB   untouched                         │
│                                                                           │
│   ▆▇█▇  CPU  18.4%  P:12.5  E:25.0  load 1.43                             │
│   ▂▃▄▃  GPU  10.5%  ANE  0.0%   62.0°C   fan 2400                         │
╰───────────────────────────────────────────────────────────────────────────╯
TOP PROCESSES   12 shown · c calm · f pause · u resume · v vibe view
  ●  Python · myapp.py        14.2 GB   6.8%   active        ...
  ◐  Slack                     1.8 GB   0.0%   idle 47m      [c]alm  [f]reeze
  ◐  Discord                   1.6 GB   0.0%   idle 2h       [c]alm  [f]reeze
  *  Xcode                     0.4 GB   1.0%   foreground    protected
```

## What it does well

- **Honest memory accounting**: matches Activity Monitor exactly because
  it reads the same kernel ledgers. App Memory is real Apple "anonymous
  pages × pagesize," not a residual computation. Compressed is "pages
  occupied by compressor" (the actual RAM the compressor uses), not the
  uncompressed equivalent that some tools report.
- **Pressure indicator that means something**: derived from real
  reclaim activity (compressor + swap rates), not from a synthetic "% of
  RAM used." Sitting at 84% committed with no swap and no compressor
  activity reads `Normal`, not `Critical` — same as Apple's pressure
  graph.
- **Three-tier action ladder, never auto-applied**:
  1. **Calm** — `taskpolicy -b <pid>` (background QoS). Reversible, no
     socket damage. Default for almost everything.
  2. **Pause** — `kill -STOP` with a forced auto-`CONT` after 60 s.
     Confirms before acting; warns if the target holds audio or live
     sockets.
  3. **Quit** — `osascript 'tell app … to quit'` with a `SIGTERM`
     fallback if the app doesn't respond.
- **Crash-safe**: each pause writes a sentinel file at
  `~/.minomonitor/paused/<pid>_<start_unix>` *before* sending `STOP`. A
  small launchd watchdog can be installed to resume orphans if the
  monitor itself dies.
- **Two views**: techie by default (full detail, technical labels) and a
  vibe view (`v` to toggle) with plain English ("Squeezed" instead of
  "Compressed", "hasn't done anything for 47m" instead of "idle 47m",
  big colored "Pause" buttons).

## What it does not do

- **It does not "free RAM" by pausing apps.** `kill -STOP` halts CPU; the
  resident pages stay mapped. macOS's compressor and file-cache eviction
  do the actual reclaim. The honest value of pausing is *reducing CPU /
  background churn* under pressure — not making the bar shorter.
- **It does not auto-pause anything.** Manual only in v1. Insights
  *suggest*; you click `Apply`.
- **It does not run `purge`.** It's a placebo on modern Apple Silicon
  where the compressor and inactive-file eviction handle it.
- **Per-process numbers do not sum to App Memory.** Same as Activity
  Monitor — `phys_footprint` is a per-task ledger, not a partition of
  total RAM. The footer line shows the gap explicitly.

## Install

Requires macOS on Apple Silicon (M1+) and **Python 3.13**.

```bash
git clone https://github.com/<your-username>/MinoMonitor.git
cd MinoMonitor
pip3 install textual psutil rich
chmod +x bin/minomon
./bin/minomon
```

Add `alias monitor="$PWD/bin/minomon"` to your shell rc if you want it on
your path.

### UI dev mode (no system access, fake oscillating data)

```bash
./bin/minomon --stub
```

## Optional: GPU / ANE / temperature panel

`powermetrics` requires `sudo`. To enable the GPU/ANE/temp/fan strip
without typing your password each launch, add a tightly-scoped sudoers
entry:

```bash
echo "$(whoami) ALL=(root) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/minomon-powermetrics
sudo chmod 440 /etc/sudoers.d/minomon-powermetrics
```

Without it, the GPU strip shows a "not enabled" hint and the rest of the
dashboard works fine.

## Optional: launchd watchdog

A small auxiliary script (`minomon/actions/watchdog.py`) can be run by
`launchd` every 60 seconds. If it ever finds your monitor dead while
sentinels exist, it resumes the paused processes for you.

```bash
cp contrib/com.minomon-watchdog.plist.template ~/Library/LaunchAgents/com.minomon-watchdog.plist
# Edit the file to point at your install path, then:
launchctl load ~/Library/LaunchAgents/com.minomon-watchdog.plist
```

## Optional: link a local helper app

The header has room for a one-line "linked app" status — for example, an
LLM server advertising which models are currently loaded. See
[`docs/integrations.md`](./docs/integrations.md) for the JSON contract.
The row is hidden when no integration is configured.

## Hotkeys

- `c` calm selected · `f` pause (with confirm) · `u` uncalm or resume
- `v` toggle vibe view · `r` refresh · `?` help
- Shift-`Q` graceful quit-app (with confirm)
- Ctrl+`C` exit the monitor

## Files written

- `~/.minomonitor/actions.log` — audit trail of every calm / pause / quit
- `~/.minomonitor/paused/` — sentinel files for currently `STOP`'d
  processes
- `~/.minomonitor/monitor.pid` — current monitor PID, used by the
  watchdog

## How it was built

Two AI assistants debated the design (an architect agent acted as an
independent reviewer), then split the build: one built the data and
actions layer, the other the UI. See
[`docs/development/`](./docs/development/) for the conversation and the
two design iterations — the v1 was wrong about something fundamental, and
the writeup is honest about what the critique caught.

## License

MIT — see [LICENSE](./LICENSE).
