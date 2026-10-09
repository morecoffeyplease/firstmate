#!/usr/bin/env python3
"""Release a prepared lane command after its foreground TTY handoff."""

from __future__ import annotations

import os
import signal
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        return 125
    try:
        gate_fd = int(argv[0])
        parent_group = int(argv[1])
        unblock_sigint = argv[2] == "unblock"
        command = argv[3:]
        gate = os.read(gate_fd, 1)
        os.close(gate_fd)
    except (OSError, ValueError):
        return 125
    if gate == b"S":
        try:
            os.setpgid(0, parent_group)
        except OSError:
            return 125
    elif gate != b"I":
        return 125
    if unblock_sigint and hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, signal.SIG_DFL)
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        sys.stderr.write(f"fm-lane-run: {exc}\n")
        return 127
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
