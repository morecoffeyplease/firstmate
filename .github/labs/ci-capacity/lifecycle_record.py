#!/usr/bin/env python3
"""Append one validated durable transition to the diagnostic lifecycle journal."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


PHASES = ('PREPARE', 'QUALIFY', 'ARM', 'RUN', 'RETIRE', 'FINALIZE', 'SEALED')
root = Path(sys.argv[1])
phase = sys.argv[2]
result = sys.argv[3]
journal = root / 'lifecycle-journal.tsv'
existing = []
if journal.exists():
    existing = [line.split('\t')[1] for line in journal.read_text().splitlines()
                if len(line.split('\t')) >= 3]
expected = len(existing)
if expected >= len(PHASES) or PHASES[expected] != phase:
    raise SystemExit(f'lifecycle transition refused: existing={existing}; requested={phase}')
if existing != list(PHASES[:expected]):
    raise SystemExit(f'lifecycle journal sequence invalid: {existing}')
stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
descriptor = os.open(journal, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
try:
    payload = f'{stamp}\t{phase}\t{result}\n'.encode()
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError('short lifecycle journal write')
        view = view[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
try:
    os.fsync(directory)
finally:
    os.close(directory)
