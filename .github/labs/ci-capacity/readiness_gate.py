#!/usr/bin/env python3
import json
import sys
import time
from pathlib import Path
import os


def record_readiness(root, stage, deadline):
    root = Path(root)
    started_epoch = float((root / 'job-start-epoch.txt').read_text())
    started_ns = int((root / 'job-start-monotonic-ns.txt').read_text())
    started = started_ns / 1_000_000_000
    deadline_monotonic = started + 60
    observed_monotonic = time.monotonic()
    observed = time.time()
    admitted = observed_monotonic <= deadline_monotonic and observed <= deadline
    result = {
        'run_id': os.environ.get('GITHUB_RUN_ID'),
        'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
        'scenario': os.environ.get('LAB_SCENARIO'),
        'stage': stage,
        'job_start_epoch': started_epoch,
        'job_start_monotonic_ns': started_ns,
        'required_analysis_start_deadline_epoch': deadline,
        'required_analysis_start_deadline_monotonic': deadline_monotonic,
        'observed_epoch': observed,
        'observed_monotonic': observed_monotonic,
        'elapsed_seconds': round(observed_monotonic - started, 6),
        'admitted': admitted,
        'pair_result': 'pending both matrix artifacts' if admitted else 'invalid readiness deadline exceeded',
    }
    target = root / f'analysis-readiness-{stage}.json'
    temporary = target.with_suffix('.json.tmp')
    if time.monotonic() > deadline_monotonic:
        result['admitted'] = False
        result['pair_result'] = 'invalid readiness deadline exceeded during record creation'
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        payload = (json.dumps(result, indent=2) + '\n').encode()
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError('short readiness record write')
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if time.monotonic() > deadline_monotonic:
        result['admitted'] = False
        result['pair_result'] = 'invalid readiness deadline exceeded after durable record'
        late = target.with_suffix('.json.late.tmp')
        late_descriptor = os.open(late, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            payload = (json.dumps(result, indent=2) + '\n').encode()
            view = memoryview(payload)
            while view:
                written = os.write(late_descriptor, view)
                if written <= 0:
                    raise OSError('short late readiness record write')
                view = view[written:]
            os.fsync(late_descriptor)
        finally:
            os.close(late_descriptor)
        os.replace(late, target)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return result


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit('usage: readiness_gate.py pre-fixture | prepare-end <run-root>')
    if sys.argv[1] == 'prepare-end' and len(sys.argv) == 3:
        root = Path(sys.argv[2])
        job_start = float((root / 'job-start-epoch.txt').read_text())
        print(json.dumps(record_readiness(root, 'prepare-ended', job_start + 60), indent=2))
        return
    if sys.argv[1] != 'pre-fixture' or len(sys.argv) != 2:
        raise SystemExit('usage: readiness_gate.py pre-fixture | prepare-end <run-root>')
    runner_temp = Path(os.environ['RUNNER_TEMP'])
    run_id = os.environ['GITHUB_RUN_ID']
    attempt = os.environ['GITHUB_RUN_ATTEMPT']
    scenario = os.environ['LAB_SCENARIO']
    root = runner_temp / 'fm-capacity' / run_id / attempt / scenario
    job_start = float((root / 'job-start-epoch.txt').read_text())
    result = record_readiness(root, 'pre-fixture', job_start + 60)
    print(json.dumps(result, indent=2))
    if not result['admitted']:
        raise SystemExit('setup exceeded the one-minute analysis readiness window; experiment pair is invalid')


if __name__ == '__main__':
    main()
