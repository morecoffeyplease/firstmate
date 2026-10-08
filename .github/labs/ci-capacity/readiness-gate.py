#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path


def record_readiness(root, stage, deadline):
    root = Path(root)
    started = float((root / 'job-start-epoch.txt').read_text())
    observed = time.time()
    admitted = observed <= deadline
    result = {
        'run_id': os.environ.get('GITHUB_RUN_ID'),
        'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
        'scenario': os.environ.get('LAB_SCENARIO'),
        'stage': stage,
        'job_start_epoch': started,
        'required_analysis_start_deadline_epoch': deadline,
        'observed_epoch': observed,
        'elapsed_seconds': round(observed - started, 3),
        'admitted': admitted,
        'pair_result': 'pending both matrix artifacts' if admitted else 'invalid readiness deadline exceeded',
    }
    target = root / f'analysis-readiness-{stage}.json'
    temporary = target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, indent=2) + '\n')
    os.replace(temporary, target)
    return result


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit('usage: readiness-gate.py pre-fixture | prepare-end <run-root>')
    if sys.argv[1] == 'prepare-end' and len(sys.argv) == 3:
        root = Path(sys.argv[2])
        job_start = float((root / 'job-start-epoch.txt').read_text())
        print(json.dumps(record_readiness(root, 'prepare-ended', job_start + 60), indent=2))
        return
    if sys.argv[1] != 'pre-fixture' or len(sys.argv) != 2:
        raise SystemExit('usage: readiness-gate.py pre-fixture | prepare-end <run-root>')
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
