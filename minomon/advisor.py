"""
Opt-in rule advisor backed by Claude Code.

Reads recent actions, outcomes, and config; asks Claude Code (via
`claude -p ...` headless) for one or two specific rule-change
suggestions; prints the response and writes a copy to
`~/.minomonitor/advice/<timestamp>.md`. Never auto-applies anything.

Why Claude Code instead of a local LLM:
- 0 RAM cost on the user's Mac (the thing this monitor is managing)
- Opus-class reasoning over a small batch of operational data is
  exactly the right shape; per-tick decisions are not
- Already on the user's PATH if they're shipping software with this
  tool; a reasonable opt-in dependency

The data flow:
    actions.log + config.toml + 1h timeline   →   claude -p ...
                                              ↓
                       prose advice + optional config diff
                                              ↓
                                   stdout + advice/<ts>.md

Failure modes (the ones Codex flagged):
- claude not on PATH         → clear actionable error
- claude hangs               → hard timeout, kill the child
- network failure inside     → propagated as nonzero exit + stderr
- silent degradation         → never returns "" — always says something
- unintended local context   → we pass the prompt via -p (string),
                               not via a working-directory hand-off,
                               so claude doesn't see the rest of the
                               filesystem unless it explicitly chooses
                               to. Filesystem access stays under the
                               user's normal Claude Code permissions.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .data.config import CONFIG_PATH, load_advisor_config


_ADVICE_DIR = Path.home() / ".minomonitor" / "advice"
_ACTIONS_LOG = Path.home() / ".minomonitor" / "actions.log"
_MAX_ACTIONS_LOG_LINES = 200    # plenty of context, won't blow up the prompt
_PROMPT_HEADER = """\
You are advising on rule changes for Mino Monitor, a small macOS
process monitor. The user has built up an actions log of every
calm/pause/quit they've performed plus an outcome verdict (helped /
neutral / worsened) recorded 60 seconds after each action.

Your job: read the data below and propose at most TWO concrete rule
changes that would likely improve the user's experience. Examples of
useful proposals:

- "I notice you manually pause Slack every time you open Xcode. Add
  a user pin or a future auto-rule for that."
- "The 60-min idle threshold for calm hasn't fired usefully — most
  helpful actions were on apps idle <30 min. Consider tightening."
- "Your last 12 calms on Brave Browser were neutral. The footprint
  there isn't worth the action; consider unpinning it from auto-mode
  candidates."

Format your response as:

    ## Summary
    1-3 sentences on what stood out in the data.

    ## Proposals
    1. <proposal title>
       Reason: <one sentence>
       Suggested config patch (optional, ```toml block):
       <only include the keys that change>
    2. <second proposal — if any; skip if data isn't strong>

If the data doesn't support a confident proposal, say so plainly and
end with "no changes recommended" — don't invent suggestions.

Never propose changes that would touch the system pinned list (kernel
processes, WindowServer, Finder, Dock, audio daemons, etc.) — those
are protected for safety reasons and aren't user-editable.
"""


def _read_recent_actions(max_lines: int = _MAX_ACTIONS_LOG_LINES) -> str:
    if not _ACTIONS_LOG.exists():
        return "(no actions log yet — user hasn't taken any actions)"
    try:
        text = _ACTIONS_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(could not read actions.log: {e})"
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]) if lines else "(empty)"


def _read_current_config() -> str:
    if not CONFIG_PATH.exists():
        return "(no user config yet — pin/unpin lists are empty, advisor uses defaults)"
    try:
        return CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as e:
        return f"(could not read config.toml: {e})"


def _system_summary() -> str:
    """A tiny snapshot taken right now, so Claude has the current state
    without us pulling in the whole sampler."""
    try:
        import asyncio
        from .data.sampler import Sampler

        async def grab():
            s = Sampler(top_n=12)
            await s.start()
            try:
                await asyncio.sleep(2.0)
                return s.latest
            finally:
                await s.stop()

        sample = asyncio.run(grab())
        if sample is None:
            return "(no sample available)"
        m = sample.memory
        used = m.app_gb + m.wired_gb + m.compressed_gb
        lines = [
            f"pressure={m.pressure_level}  used={used:.1f}/{m.total_gb:.0f} GB  "
            f"swap_out={m.swap_out_rate_mbps:.1f} MB/s",
            "",
            "Top processes (matches Activity Monitor's Memory column):",
        ]
        for r in sample.processes[:10]:
            d1 = "—" if r.delta_1m_gb is None else f"{r.delta_1m_gb:+.1f}"
            d5 = "—" if r.delta_5m_gb is None else f"{r.delta_5m_gb:+.1f}"
            lines.append(
                f"  {r.name[:40]:40} {r.rss_gb:>5.1f} GB   "
                f"d1m={d1}  d5m={d5}  state={r.state}  pinned={r.pinned}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"(could not gather system snapshot: {e})"


def _build_prompt() -> str:
    actions_log = _read_recent_actions()
    config = _read_current_config()
    summary = _system_summary()

    return f"""{_PROMPT_HEADER}

----- Current system state -----
{summary}

----- Current ~/.minomonitor/config.toml -----
{config}

----- Last {_MAX_ACTIONS_LOG_LINES} lines of ~/.minomonitor/actions.log -----
{actions_log}

----- End of data -----

Now write the Summary and Proposals sections per the format above.
"""


def _save_advice(advice: str) -> Path:
    _ADVICE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _ADVICE_DIR / f"{stamp}.md"
    path.write_text(advice, encoding="utf-8")
    return path


def run_advise() -> int:
    """Entry point used by `minomon --advise`. Returns a process exit
    code: 0 success, 1 advisor disabled or claude missing, 2 timeout,
    3 claude returned nonzero, 4 unexpected error."""
    cfg = load_advisor_config()
    if cfg["engine"] == "none":
        print(
            "Advisor is not configured.\n\n"
            "To enable Claude Code as the rule advisor, edit "
            f"{CONFIG_PATH} and add:\n\n"
            '    [advisor]\n'
            '    engine = "claude-code"\n\n'
            "What would be sent to Claude Code on each invocation:\n"
            f"  • Last {_MAX_ACTIONS_LOG_LINES} lines of "
            "~/.minomonitor/actions.log\n"
            "  • Your ~/.minomonitor/config.toml\n"
            "  • A short system snapshot (top 10 processes by phys_footprint, "
            "current memory pressure, swap rate)\n\n"
            "No data leaves your machine until you opt in. Claude's response is "
            "written to ~/.minomonitor/advice/<timestamp>.md and never applies "
            "config changes automatically.",
            file=sys.stderr,
        )
        return 1

    claude = shutil.which("claude")
    if not claude:
        print(
            "advisor.engine is 'claude-code' but `claude` is not on your PATH.\n"
            "Install Claude Code: https://docs.claude.com/en/docs/claude-code/quickstart\n",
            file=sys.stderr,
        )
        return 1

    prompt = _build_prompt()
    timeout = cfg["timeout_seconds"]

    print(f"Asking Claude Code for advice (timeout {timeout}s)…\n", file=sys.stderr)
    try:
        completed = subprocess.run(
            [claude, "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            f"Claude Code did not respond within {timeout}s. The child process was killed.\n"
            "Try again, or raise timeout_seconds in [advisor] in your config.toml.",
            file=sys.stderr,
        )
        return 2
    except OSError as e:
        print(f"Failed to run claude: {e}", file=sys.stderr)
        return 4

    advice = completed.stdout.strip()
    if completed.returncode != 0:
        print(
            f"claude exited {completed.returncode}.\n"
            f"stderr: {completed.stderr.strip()[:1000]}",
            file=sys.stderr,
        )
        return 3

    if not advice:
        # Defensive: never return silently. Codex flagged this as a real risk.
        print(
            "claude returned an empty response. This is unusual — try again, "
            "or check your Claude Code authentication.",
            file=sys.stderr,
        )
        return 3

    saved = _save_advice(advice)
    sys.stdout.write(advice)
    sys.stdout.write(f"\n\n---\nSaved to {saved}\n")

    # Always log that the advisor was invoked, even when it succeeded —
    # gives the user (and future advisor calls) a record of how often
    # they're consulting the rule curator.
    try:
        from .actions._common import append_action_log
        append_action_log(
            action="advise",
            pid=0,
            start_unix=0,
            success=True,
            message=f"saved to {saved.name}",
            name="claude-code",
        )
    except Exception:
        pass

    return 0
