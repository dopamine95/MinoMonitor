from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import time
from pathlib import Path


STATE_DIR = Path(os.environ.get("MINOMONITOR_HOME", str(Path.home() / ".minomonitor"))).expanduser()
PAUSED_DIR = STATE_DIR / "paused"
ACTIONS_LOG = STATE_DIR / "actions.log"
MONITOR_PID_FILE = STATE_DIR / "monitor.pid"


def main() -> None:
    _ensure_state_dirs()
    while True:
        sentinels = _list_paused_sentinels()
        if sentinels and not _monitor_alive():
            for pid, start_unix, path in sentinels:
                if not _matches_process_identity(pid, start_unix):
                    path.unlink(missing_ok=True)
                    _append_action_log("thaw", pid, start_unix, False, "PID reused - not resuming.")
                    continue
                try:
                    os.kill(pid, signal.SIGCONT)
                except OSError as exc:
                    _append_action_log("thaw", pid, start_unix, False, f"Watchdog resume failed: {exc}")
                    continue
                path.unlink(missing_ok=True)
                _append_action_log("thaw", pid, start_unix, True, "Watchdog resumed paused process.")
            return
        time.sleep(30)


def _ensure_state_dirs() -> None:
    PAUSED_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _list_paused_sentinels() -> list[tuple[int, int, Path]]:
    entries: list[tuple[int, int, Path]] = []
    for path in PAUSED_DIR.iterdir():
        try:
            pid_text, start_text = path.name.split("_", 1)
            entries.append((int(pid_text), int(start_text), path))
        except ValueError:
            continue
    return entries


def _monitor_alive() -> bool:
    try:
        pid = int(MONITOR_PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _matches_process_identity(pid: int, start_unix: int) -> bool:
    try:
        output = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return False
    if not output:
        return False

    try:
        started = time.mktime(time.strptime(output, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return False
    return int(started) == int(start_unix)


def _append_action_log(action: str, pid: int, start_unix: int, success: bool, message: str) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    status = "success" if success else "fail"
    line = (
        f"{timestamp} action={action} pid={pid} start_unix={int(start_unix)} "
        f"name={pid!r} status={status} message={message}\n"
    )
    with ACTIONS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)


if __name__ == "__main__":
    main()
