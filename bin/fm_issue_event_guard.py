"""Serialize issue-event writes with summary basis capture and publication."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path


def issue_event_lock(home: Path):
    state = home / "state"
    if state.is_symlink() or not state.is_dir():
        raise OSError("issue event state directory is unavailable")
    path = state / ".issue-events.lock"
    if path.is_symlink():
        raise OSError("issue event lock is a symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except BaseException:
        os.close(fd)
        raise


def main(argv: list[str]) -> int:
    if len(argv) < 6 or argv[0] != "append":
        return 2
    task_dir = Path(argv[1])
    task = argv[2]
    if task_dir.name != task or task_dir.parent.name != "data" or task_dir.is_symlink():
        return 2
    home = task_dir.parent.parent
    try:
        fd = issue_event_lock(home)
    except OSError:
        return 1
    try:
        result = subprocess.run(argv[3:], check=False)
        if result.returncode == 0:
            cache = home / "state" / "issue-status"
            if cache.is_dir() and not cache.is_symlink():
                for path in cache.glob("projection-*.json"):
                    if path.is_file() and not path.is_symlink():
                        path.unlink(missing_ok=True)
        return result.returncode
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
