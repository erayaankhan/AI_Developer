"""The one place that writes DEV_TICKETS/N/state.json.

write_state(): temp file in the same folder + os.replace, so a crash or a second process never sees a half-written file.
update_state(): lock + re-read + change + write, for writers that can overlap another `dt` call (a background deploy
poll, the gates run, risk_check): they change their own keys on the file as it is NOW, not as it was when they started.
"""
import json
import os
import time
from pathlib import Path

LOCK_WAIT_SECONDS = 10
LOCK_STALE_SECONDS = 30


def write_state(path: Path, state: dict) -> None:
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    for attempt in range(10):  # Windows: the target may be open for a moment in an editor or a virus scanner
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.1)


def _acquire(lock: Path) -> None:
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:  # left by a crashed process
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"{lock} is held by another dt command")
            time.sleep(0.05)


def update_state(path: Path, change) -> dict:
    """Re-read state.json under a lock, call change(state) to edit it in place, write it back. Returns the new state.
    Does nothing (returns {}) when the file does not exist."""
    path = Path(path)
    if not path.is_file():
        return {}
    lock = path.with_name(path.name + ".lock")
    _acquire(lock)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        change(state)
        write_state(path, state)
        return state
    finally:
        lock.unlink(missing_ok=True)
