#!/usr/bin/env python3
import contextlib
import io
import os
import runpy
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


output_path = Path(sys.argv[1]) / 'resources-samples.log'
interval = float(sys.argv[2])
recipe_root = Path(sys.argv[3])
ready_path = Path(sys.argv[1]) / 'resource-sampler-ready.txt'
ready_descriptor = ready_path.open('x')
try:
    ready_descriptor.write('ready\n')
    ready_descriptor.flush()
    os.fsync(ready_descriptor.fileno())
finally:
    ready_descriptor.close()


def process_snapshot():
    rows = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            command = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(
                'utf-8', 'replace'
            ).strip()
            rows.append(f'{entry.name} {stat_fields[1]} {stat_fields[0]} {stat_fields[21]} {command}')
        except (OSError, IndexError):
            continue
    return '\n'.join(sorted(rows, key=lambda row: int(row.split()[0])))


def cgroup_snapshot():
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        runpy.run_path(str(recipe_root / 'cgroup-snapshot.py'), run_name='__main__')
    return captured.getvalue()


with output_path.open('a') as stream:
    while True:
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        stream.write(f'\n===== sample_utc={now} =====\n')
        for title, path in (
            ('memory', '/proc/meminfo'),
            ('vmstat', '/proc/vmstat'),
            ('memory pressure', '/proc/pressure/memory'),
            ('cpu pressure', '/proc/pressure/cpu'),
            ('cgroup membership', '/proc/self/cgroup'),
        ):
            stream.write(f'--- {title} ---\n')
            try:
                stream.write(Path(path).read_text())
            except OSError as error:
                stream.write(f'unavailable: {error}\n')
        stream.write('--- process table (pid ppid state rss_pages command) ---\n')
        stream.write(process_snapshot() + '\n')
        stream.write('--- resolved cgroup metrics ---\n')
        try:
            stream.write(cgroup_snapshot())
        except (OSError, RuntimeError) as error:
            stream.write(f'unavailable: {error}\n')
        stream.flush()
        time.sleep(interval)
