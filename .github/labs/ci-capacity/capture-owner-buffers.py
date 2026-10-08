#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
from pathlib import Path


owner_tmp = Path(sys.argv[1])
destination = Path(sys.argv[2])
destination.mkdir(parents=True, exist_ok=True)
stopping = False
captures = {}
errors = []


def request_stop(signum, _frame):
    global stopping
    stopping = True


def record_error(message):
    errors.append(message)
    try:
        with (destination / 'capture-errors.log').open('a') as stream:
            stream.write(message + '\n')
    except OSError:
        pass


def open_new_files():
    if not owner_tmp.exists():
        return
    for directory in owner_tmp.glob('fm-lint.*'):
        if not directory.is_dir():
            continue
        for current, _, filenames in os.walk(directory):
            current_path = Path(current)
            for filename in filenames:
                path = current_path / filename
                try:
                    relative = path.relative_to(owner_tmp)
                    key = str(relative)
                    if key in captures:
                        continue
                    source_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target_fd = os.open(
                        target,
                        os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC,
                        0o600,
                    )
                    captures[key] = [source_fd, target_fd, 0]
                except FileNotFoundError:
                    continue
                except OSError as error:
                    record_error(f'open {path}: {error}')


def copy_available_bytes():
    for key, capture in list(captures.items()):
        source_fd, target_fd, offset = capture
        try:
            size = os.fstat(source_fd).st_size
            while offset < size:
                chunk = os.pread(source_fd, min(1024 * 1024, size - offset), offset)
                if not chunk:
                    break
                written = 0
                while written < len(chunk):
                    written += os.write(target_fd, chunk[written:])
                offset += len(chunk)
            capture[2] = offset
        except OSError as error:
            record_error(f'copy {key}: {error}')


def write_summary(finalized):
    summary = {
        'owner_tmp': str(owner_tmp),
        'finalized': finalized,
        'open_descriptors': len(captures),
        'files': {key: capture[2] for key, capture in captures.items()},
        'errors': errors,
    }
    target = destination / 'capture-summary.json'
    temporary = destination / 'capture-summary.json.tmp'
    try:
        temporary.write_text(json.dumps(summary, indent=2) + '\n')
        os.replace(temporary, target)
    except OSError as error:
        record_error(f'write capture summary: {error}')


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGHUP, request_stop)
try:
    (destination / 'capture-ready.txt').write_text('ready\n')
    while not stopping:
        open_new_files()
        copy_available_bytes()
        time.sleep(0.025)
    open_new_files()
    copy_available_bytes()
    write_summary(True)
except BaseException as error:
    record_error(f'capture reader failed: {type(error).__name__}: {error}')
    try:
        open_new_files()
        copy_available_bytes()
        write_summary(False)
    except BaseException as final_error:
        record_error(f'final capture failed: {type(final_error).__name__}: {final_error}')
finally:
    for source_fd, target_fd, _ in captures.values():
        for descriptor in (source_fd, target_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass
if errors:
    raise SystemExit(1)
