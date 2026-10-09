#!/usr/bin/env python3
"""Observe terminal signals delivered to the lane command process group."""

from __future__ import annotations

import os
import select
import signal
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 125
    try:
        signal_fd, stop_fd, ready_fd = (int(value) for value in argv)
    except ValueError:
        return 125

    codes = {signal.SIGINT: b"I", signal.SIGTERM: b"T", signal.SIGHUP: b"H"}

    def record(signum: int, _frame: object) -> None:
        try:
            os.write(signal_fd, codes[signum])
        except OSError:
            pass

    for signum in codes:
        signal.signal(signum, record)
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
    os.write(ready_fd, b"R")
    os.close(ready_fd)
    while True:
        try:
            readable, _, _ = select.select([stop_fd], [], [], 1)
        except InterruptedError:
            continue
        if readable:
            try:
                if os.read(stop_fd, 1) == b"S":
                    return 0
            except OSError:
                return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
