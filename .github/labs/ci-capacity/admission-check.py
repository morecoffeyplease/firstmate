#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


event_path = Path(os.environ['GITHUB_EVENT_PATH'])
event = json.loads(event_path.read_text())
pull_request = event.get('pull_request') or {}
head = pull_request.get('head') or {}
head_repo = head.get('repo') or {}
base = pull_request.get('base') or {}
checks = {
    'repository': os.environ.get('GITHUB_REPOSITORY') == 'morecoffeyplease/firstmate',
    'event_name': os.environ.get('GITHUB_EVENT_NAME') == 'pull_request',
    'event_action': event.get('action') == 'opened',
    'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT') == '1',
    'pull_request_number': str(pull_request.get('number', ''))
    == os.environ.get('LAB_PULL_REQUEST_NUMBER'),
    'draft': pull_request.get('draft') is True,
    'base_ref': base.get('ref') == 'main',
    'head_ref': head.get('ref') == 'fm/ci-capacity-design',
    'head_repository': head_repo.get('full_name') == 'morecoffeyplease/firstmate',
    'head_sha': head.get('sha') == os.environ.get('LAB_HEAD_SHA'),
}
result = {
    'repository': os.environ.get('GITHUB_REPOSITORY'),
    'event_name': os.environ.get('GITHUB_EVENT_NAME'),
    'event_action': event.get('action'),
    'run_id': os.environ.get('GITHUB_RUN_ID'),
    'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
    'pull_request_number': pull_request.get('number'),
    'draft': pull_request.get('draft'),
    'base_ref': base.get('ref'),
    'head_ref': head.get('ref'),
    'head_repository': head_repo.get('full_name'),
    'head_sha': head.get('sha'),
    'checks': checks,
    'admitted': all(checks.values()),
}
output = Path(sys.argv[1]) if len(sys.argv) > 1 else None
encoded = json.dumps(result, indent=2) + '\n'
if output:
    output.write_text(encoded)
print(encoded, end='')
if not result['admitted']:
    raise SystemExit(1)
