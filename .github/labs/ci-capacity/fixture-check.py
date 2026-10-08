#!/usr/bin/env python3
import json
import os
import platform
import resource
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


recipe = Path(__file__).resolve().parent
run_root = Path(os.environ['LAB_RUN_ROOT']) / 'linux-tracer-fixtures'
bash = shutil.which('bash')
python = sys.executable
if platform.system() != 'Linux' or platform.machine() != 'x86_64':
    raise SystemExit('Linux x86_64 fixtures are required; no platform substitution is allowed')
if not bash:
    raise SystemExit('fixture checks require bash')
run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
sys.path.insert(0, str(recipe))
from owned_child import OwnedChild


def write(path, contents, executable=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(contents)
    if executable:
        path.chmod(0o755)


def kill_owned_session(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def verify_pid_reuse_refusal():
    sys.path.insert(0, str(recipe))
    from owned_child import OwnedChild, process_identity
    process = subprocess.Popen([shutil.which('sleep'), '30'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    sent = []
    owner = OwnedChild.bind(process)
    recorded_identity = owner.identity
    owner.identity_reader = lambda _pid: 'reused-unrelated-start'
    owner.signal_sender = lambda pidfd, signum: sent.append((pidfd, signum))
    assert not owner.send(signal.SIGTERM)
    assert sent == [], sent
    owner.identity_reader = process_identity
    owner.signal_sender = signal.pidfd_send_signal
    assert owner.send(signal.SIGTERM)
    try:
        status = process.wait(timeout=2)
    finally:
        owner.close()
    return {'recorded_identity': recorded_identity,
            'observed_identity': 'reused-unrelated-start',
            'signals_sent_on_mismatch': sent, 'real_child_exit': status,
            'result': 'mismatch-refused-then-real-pidfd-cleanup'}


def run_tracer(name, owner_text, timeout=4, send_signal=None, failure_injection=None,
               owner_filename='owner.sh', owner_executable=None):
    started = time.monotonic()
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    case = run_root / name
    source = case / 'source'
    output = case / 'evidence'
    owner_tmp = case / 'owner-tmp'
    retained = output / 'owner-output-retained'
    source.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner_tmp.mkdir(mode=0o700, exist_ok=True)
    retained.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = source / owner_filename
    step = case / 'analysis-step.sh'
    write(owner, owner_text, executable=True)
    write(step, f'{owner}\n')
    stdout_path = output / 'owner.stdout'
    stderr_path = output / 'owner.stderr'
    ready_path = output / 'controller-ready.txt'
    environment = os.environ.copy()
    environment.pop('FM_LINT_JOBS', None)
    environment.pop('FM_LINT_TELEMETRY', None)
    environment.update({'TMPDIR': str(owner_tmp), 'CI': 'true',
                        'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C'})
    if send_signal is not None:
        environment['FIXTURE_TRAP_READY'] = str(trap_ready)
    command = [bash, '-e', str(step)]
    (output / 'command.json').write_text(json.dumps({
        'argv': command, 'cwd': str(source), 'TMPDIR': str(owner_tmp),
        'controller': 'in-process trace_capture.run_capture',
        'environment': {'CI': 'true', 'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C',
                        'FM_LINT_JOBS': 'unset', 'FM_LINT_TELEMETRY': 'unset'},
        'timeout_seconds': timeout, 'signal': send_signal,
    }, indent=2) + '\n')
    sys.path.insert(0, str(recipe))
    from trace_capture import run_capture

    owner_exec_seen = False
    trap_ready = case / 'trap-ready'

    def record_exec(record):
        nonlocal owner_exec_seen
        owner_exec_seen = True
        (output / 'owner-exec.json').write_text(json.dumps(record, indent=2) + '\n')

    def requested_stop():
        if send_signal is not None and owner_exec_seen and trap_ready.exists():
            return f'signal-{send_signal}'
        if time.monotonic() >= analysis_deadline:
            return 'analysis-deadline'
        return None

    analysis_deadline = time.monotonic() + timeout
    cleanup_deadline = analysis_deadline + 2
    from wait_owner import WaitOwner
    wait_owner = WaitOwner(output / 'wait-ledger.jsonl')
    args = SimpleNamespace(
        owner_tmp=str(owner_tmp), destination=str(retained), cwd=str(source),
        stdout=str(stdout_path), stderr=str(stderr_path), ready=str(ready_path),
        cleanup_deadline_monotonic=cleanup_deadline,
        cleanup_deadline=cleanup_deadline, owner_exit=str(output / 'owner-step.exit'),
        owner_wait=str(output / 'owner-step.wait.json'),
        step_exit=output / 'step-shell.exit', step_wait=output / 'step-shell.wait.json',
        command=command, owner_script_path=str(owner),
        owner_executable_path=owner_executable or bash,
        exec_deadline_monotonic=analysis_deadline + 60,
        environment=environment, wait_owner=wait_owner, sampler_pid=None,
        failure_injection=failure_injection,
        owner_exec_callback=record_exec, expected_signal_for_fixtures=True,
    )
    returncode, summary = run_capture(args, requested_stop)
    if not ready_path.exists():
        raise AssertionError(f'{name}: in-process controller readiness marker missing')
    cleanup = {'controller': 'sole process owns tracee wait and cleanup',
               'returncode': returncode, 'owner_wait': summary.get('owner_wait'),
               'errors': summary.get('errors')}
    (output / 'cleanup.json').write_text(json.dumps(cleanup, indent=2) + '\n')
    summary_path = retained / 'capture-summary.json'
    if not summary_path.exists():
        raise AssertionError(f'{name}: capture summary missing')
    (output / 'controller-exit.txt').write_text(f'{returncode}\n')
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    measurements = {
        'wall_seconds': time.monotonic() - started,
        'controller_cpu_user_seconds': cpu_after.ru_utime - cpu_before.ru_utime,
        'controller_cpu_system_seconds': cpu_after.ru_stime - cpu_before.ru_stime,
        'seccomp_events': summary.get('seccomp_event_count'),
        'maximum_held_descriptors': summary.get('maximum_held_descriptors'),
        'retained_bytes': summary.get('retained_bytes'),
    }
    (output / 'traced-measurements.json').write_text(json.dumps(measurements, indent=2) + '\n')
    return case, output, returncode, summary


def assert_captured(case, relative, expected):
    actual = (case / 'evidence/owner-output-retained' / relative).read_bytes()
    assert actual == expected, (relative, actual, expected)


def run_untraced_control(name, owner_text):
    started = time.monotonic()
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    case = run_root / name
    source = case / 'source'
    output = case / 'evidence'
    owner_tmp = case / 'owner-tmp'
    owner = source / 'owner.sh'
    step = case / 'analysis-step.sh'
    environment = os.environ.copy()
    environment.pop('FM_LINT_JOBS', None)
    environment.pop('FM_LINT_TELEMETRY', None)
    environment.update({'TMPDIR': str(owner_tmp), 'CI': 'true',
                        'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C'})
    command = [bash, '-e', str(step)]
    (output / 'control-command.json').write_text(json.dumps({
        'argv': command, 'cwd': str(source), 'TMPDIR': str(owner_tmp),
        'environment': {'CI': 'true', 'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C',
                        'FM_LINT_JOBS': 'unset', 'FM_LINT_TELEMETRY': 'unset'},
    }, indent=2) + '\n')
    process = subprocess.Popen(command, cwd=source, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate(timeout=2)
    result = SimpleNamespace(returncode=process.returncode, stdout=stdout,
                             stderr=stderr, timed_out=timed_out)
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    (output / 'control-stdout.bin').write_bytes(result.stdout)
    (output / 'control-stderr.bin').write_bytes(result.stderr)
    (output / 'control-exit.txt').write_text(f'{result.returncode}\n')
    return {'returncode': result.returncode, 'stdout': result.stdout,
            'stderr': result.stderr,
            'timed_out': result.timed_out,
            'wall_seconds': time.monotonic() - started,
            'child_cpu_user_seconds': cpu_after.ru_utime - cpu_before.ru_utime,
            'child_cpu_system_seconds': cpu_after.ru_stime - cpu_before.ru_stime,
            'argv': command, 'environment': {
                key: environment.get(key) for key in ('CI', 'GITHUB_ACTIONS', 'LC_ALL',
                                                       'TMPDIR', 'FM_LINT_JOBS',
                                                       'FM_LINT_TELEMETRY')
            }}


def run_supervisor_signal_case(name, signum):
    case = run_root / name
    source = case / 'source'
    recipe_copy = case / 'recipe'
    output = case / 'evidence'
    (source / 'bin').mkdir(mode=0o700, parents=True)
    recipe_copy.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    for filename in ('analysis-supervisor.py', 'trace_capture.py', 'owned_child.py',
                     'wait_owner.py', 'cgroup-snapshot.py', 'readiness_gate.py'):
        shutil.copy2(recipe / filename, recipe_copy / filename)
    shutil.copy2(recipe / 'sample.py', recipe_copy / 'sample.py')
    write(recipe_copy / 'sample.sh', '''#!/usr/bin/env bash
set -eu
exec python3 "$3/sample.py" "$1" "$2" "$3"
''', executable=True)
    signal_name = signal.Signals(signum).name.removeprefix('SIG')
    exit_code = 128 + signum
    owner_text = f'''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.outer-signal"
mkdir -p "$d/output"
printf 'before-signal\\n' > "$d/output/shard.9.out"
on_signal() {{
  printf 'final-after-{signal_name}\\n' >> "$d/output/shard.9.out"
  rm -rf "$d"
  exit {exit_code}
}}
trap on_signal {signal_name}
touch "$FIXTURE_TRAP_READY"
while :; do read -r -t 0.05 _ || true; done
'''
    owner = source / 'bin/fm-lint.sh'
    write(owner, owner_text, executable=True)
    step = case / 'analysis-step.sh'
    write(step, 'bin/fm-lint.sh\n')
    start_mono = time.monotonic()
    (output / 'job-start-epoch.txt').write_text(f'{time.time():.9f}\n')
    (output / 'job-start-monotonic-ns.txt').write_text(
        f'{int(start_mono * 1_000_000_000)}\n')
    for phase, result_text in (('PREPARE', 'fixture'), ('QUALIFY', 'fixture'), ('ARM', 'fixture')):
        subprocess.run([python, str(recipe / 'lifecycle_record.py'), str(output),
                        phase, result_text], check=True, timeout=2)
    command = [python, str(recipe_copy / 'analysis-supervisor.py'), 'baseline', '8',
               str(start_mono + 20), str(start_mono + 25), str(output), str(source),
               str(recipe_copy), str(step), bash]
    env = os.environ.copy()
    env.update({'SOURCE_COMMIT': 'fixture-source',
                'FIXTURE_TRAP_READY': str(output / 'trap-ready'),
                'LAB_EXPECTED_SIGNAL_FIXTURE': '1'})
    (output / 'command.json').write_text(json.dumps({
        'argv': command, 'cwd': str(source), 'signal': signal_name,
        'signal_target': 'identity-bound supervisor pidfd',
    }, indent=2) + '\n')
    log = (output / 'supervisor.log').open('wb')
    sentinel = subprocess.Popen([bash, '-c', 'read -r -t 30 _ || true'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    sentinel_owner = OwnedChild.bind(sentinel)
    process = subprocess.Popen(command, cwd=source, env=env, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    process_owner = OwnedChild.bind(process)
    sent = False
    try:
        readiness_deadline = time.monotonic() + 8
        while time.monotonic() < readiness_deadline:
            if (output / 'trap-ready').exists():
                sent = process_owner.send(signum)
                break
            if process.poll() is not None:
                break
            time.sleep(0.01)
        if not sent:
            raise AssertionError(f'{name}: owner trap readiness not reached')
        status = process.wait(timeout=8)
        assert sentinel_owner.exited() is False, f'{name}: unrelated sentinel exited'
    finally:
        if process.poll() is None:
            process_owner.send(signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process_owner.send(signal.SIGKILL)
                kill_owned_session(process)
                process.wait(timeout=2)
        sentinel_owner.send(signal.SIGTERM)
        try:
            sentinel.wait(timeout=2)
        except subprocess.TimeoutExpired:
            sentinel_owner.send(signal.SIGKILL)
            sentinel.wait(timeout=2)
        process_owner.close()
        sentinel_owner.close()
        log.close()
    summary_path = output / 'owner-output-retained/capture-summary.json'
    summary = json.loads(summary_path.read_text())
    exec_signal_state = [event['signal_attributes'] for event in summary['unlink_events']
                         if event.get('event') == 'exec' and 'signal_attributes' in event]
    assert exec_signal_state, (name, summary)
    owner_wait = json.loads((output / 'owner-step.wait.json').read_text())
    assert status == exit_code, (name, status)
    assert owner_wait['status'] == exit_code, (name, owner_wait)
    assert summary['finalized'], (name, summary)
    relative = 'fm-lint.outer-signal/output/shard.9.out'
    assert (output / 'owner-output-retained' / relative).read_text() == (
        f'before-signal\nfinal-after-{signal_name}\n')
    return {'name': name, 'signal': signal_name, 'supervisor_exit': status,
            'owner_wait': owner_wait, 'capture': summary_path.relative_to(output).as_posix(),
            'signal_attributes_at_exec': exec_signal_state,
            'unrelated_sentinel_survived': True}


def run_sampler_failure_case(name, stage):
    case = run_root / name
    source = case / 'source'
    recipe_copy = case / 'recipe'
    output = case / 'evidence'
    (source / 'bin').mkdir(mode=0o700, parents=True)
    recipe_copy.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    for filename in ('analysis-supervisor.py', 'trace_capture.py', 'owned_child.py',
                     'wait_owner.py', 'cgroup-snapshot.py', 'readiness_gate.py'):
        shutil.copy2(recipe / filename, recipe_copy / filename)
    if stage == 'startup':
        sample_text = '#!/usr/bin/env bash\nset -eu\nprintf startup > "$1/resource-sampler-failed.txt"\nexit 7\n'
    else:
        sample_text = ('#!/usr/bin/env bash\nset -eu\nprintf ready > "$1/resource-sampler-ready.txt"\n'
                       'while [[ ! -f "$1/analysis-started-utc.txt" ]]; do read -r -t 0.01 _ || true; done\n'
                       'printf during > "$1/resource-sampler-failed.txt"\nexit 7\n')
    write(recipe_copy / 'sample.sh', sample_text, executable=True)
    owner_text = ('#!/usr/bin/env bash\nset -eu\ntouch "$FIXTURE_ROOT/owner-started"\n'
                  'while :; do read -r -t 0.05 _ || true; done\n')
    write(source / 'bin/fm-lint.sh', owner_text, executable=True)
    step = case / 'analysis-step.sh'
    write(step, 'bin/fm-lint.sh\n')
    start_mono = time.monotonic()
    (output / 'job-start-epoch.txt').write_text(f'{time.time():.9f}\n')
    (output / 'job-start-monotonic-ns.txt').write_text(f'{int(start_mono * 1_000_000_000)}\n')
    for phase in ('PREPARE', 'QUALIFY', 'ARM'):
        subprocess.run([python, str(recipe / 'lifecycle_record.py'), str(output),
                        phase, 'fixture'], check=True, timeout=2)
    command = [python, str(recipe_copy / 'analysis-supervisor.py'), 'baseline', '8',
               str(start_mono + 20), str(start_mono + 25), str(output), str(source),
               str(recipe_copy), str(step), bash]
    env = os.environ.copy()
    env.update({'SOURCE_COMMIT': 'fixture-source', 'FIXTURE_ROOT': str(output)})
    (output / 'command.json').write_text(json.dumps(command) + '\n')
    log = (output / 'supervisor.log').open('wb')
    process = subprocess.Popen(command, cwd=source, env=env, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    owned = OwnedChild.bind(process)
    try:
        status = process.wait(timeout=8)
    finally:
        if process.poll() is None:
            owned.send(signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                owned.send(signal.SIGKILL)
                kill_owned_session(process)
                process.wait(timeout=2)
        owned.close()
        log.close()
    outcome = json.loads((output / 'analysis-outcome.json').read_text())
    assert status == 125, (stage, status, outcome)
    assert (output / 'resource-sampler-failed.txt').exists()
    if stage == 'startup':
        assert not (output / 'owner-started').exists(), outcome
    else:
        assert (output / 'owner-started').exists(), outcome
        assert (output / 'owner-step.wait.json').exists(), outcome
    waits = [json.loads(line) for line in (output / 'wait-ledger.jsonl').read_text().splitlines()]
    assert any(item.get('role') == 'resource-sampler' and item.get('kind') in ('exit', 'signal')
               for item in waits), waits
    return {'name': name, 'stage': stage, 'exit': status,
            'result': outcome.get('result'), 'wait_ledger': 'wait-ledger.jsonl'}


facts = {
    'observed_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    'kernel_release': platform.release(),
    'machine': platform.machine(),
    'python_version': sys.version,
    'bash_path': bash,
    'pidfd_open_available': hasattr(os, 'pidfd_open'),
    'pidfd_send_signal_available': hasattr(signal, 'pidfd_send_signal'),
    'ptrace_source': 'own child only; PTRACE_TRACEME and PTRACE_O_EXITKILL',
    'seccomp_source': 'unlink/unlinkat RET_TRACE; child sets inherited no_new_privs',
}
(run_root / 'preflight.json').write_text(json.dumps(facts, indent=2) + '\n')
if not facts['pidfd_open_available'] or not facts['pidfd_send_signal_available']:
    raise SystemExit('required pidfd operations unavailable; refusing Linux fixture')

sys.path.insert(0, str(recipe))
from trace_capture import (AT_FDCWD, SYS_UNLINK, X32_SYSCALL_BIT, TraceFailure,
                           copy_fd, exact_candidate, run_capture)
try:
    run_capture(SimpleNamespace(command=[bash], failure_injection='ptrace-denied'))
except TraceFailure as error:
    (run_root / 'ptrace-denied-refusal.json').write_text(json.dumps({
        'result': 'refused-before-child-creation', 'error': str(error),
    }, indent=2) + '\n')
else:
    raise AssertionError('injected ptrace denial was not refused')

owner_root_for_refusal = run_root / 'unsupported-path-owner-root'
owner_root_for_refusal.mkdir(mode=0o700)
owner_root_fd = os.open(owner_root_for_refusal, os.O_RDONLY | os.O_DIRECTORY)
try:
    bad_path = str(owner_root_for_refusal / 'fm-lint.bad' / 'output' / '..' /
                   'shard.0.out')
    try:
        exact_candidate(os.getpid(), AT_FDCWD, bad_path,
                        str(owner_root_for_refusal), owner_root_fd)
    except TraceFailure as error:
        (run_root / 'unsupported-path-refusal.json').write_text(json.dumps({
            'path': bad_path, 'result': 'refused', 'error': str(error),
        }, indent=2) + '\n')
    else:
        raise AssertionError('capture-shaped path with traversal was not refused')
finally:
    os.close(owner_root_fd)
owner_root_fd = os.open(owner_root_for_refusal, os.O_RDONLY | os.O_DIRECTORY)
try:
    capture_target(os.getpid(), SimpleNamespace(
        orig_rax=SYS_UNLINK | X32_SYSCALL_BIT, rdi=0, rsi=0),
        str(owner_root_for_refusal), owner_root_fd, owner_root_for_refusal,
        {}, [])
except Exception as error:
    if not isinstance(error, TraceFailure) or 'x32' not in str(error):
        raise
    (run_root / 'unsupported-abi-refusal.json').write_text(json.dumps({
        'syscall': hex(SYS_UNLINK | X32_SYSCALL_BIT), 'result': 'refused',
        'error': str(error),
    }, indent=2) + '\n')
else:
    raise AssertionError('x32 unlink syscall was not refused')
finally:
    os.close(owner_root_fd)

deadline_source = (run_root / 'deadline-source.bin').open('wb')
deadline_source.write(b'deadline evidence')
deadline_source.close()
deadline_fd = os.open(run_root / 'deadline-source.bin', os.O_RDONLY)
deadline_target = run_root / 'deadline-copy.bin'
try:
    copy_fd(deadline_fd, deadline_target, time.monotonic() - 0.001)
except TimeoutError:
    pass
else:
    raise AssertionError('late evidence copy was not refused')
finally:
    os.close(deadline_fd)
assert not deadline_target.exists()
assert not deadline_target.with_name(deadline_target.name + '.partial').exists()
(run_root / 'common-deadline-refusal.json').write_text(json.dumps({
    'result': 'copy-refused-before-writer-creation',
    'deadline': 'already expired', 'target_created': False,
}, indent=2) + '\n')

reuse = verify_pid_reuse_refusal()
(run_root / 'pid-reuse-refusal.json').write_text(json.dumps(reuse, indent=2) + '\n')

immediate_owner = '''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.immediate"
mkdir -p "$d/output"
printf 'immediate-output\\n' > "$d/output/shard.0.out"
printf '0\\n' > "$d/output/shard.0.rc"
rm -f "$d/output/shard.0.out" "$d/output/shard.0.rc"
rmdir "$d/output" "$d"
'''
case, _output, result, summary = run_tracer('immediate-unlink', immediate_owner)
assert result == 0 and summary['finalized'], (result, summary)
assert_captured(case, 'fm-lint.immediate/output/shard.0.out', b'immediate-output\n')
assert_captured(case, 'fm-lint.immediate/output/shard.0.rc', b'0\n')
assert any(event.get('opened_before_resume') for event in summary['unlink_events']), summary
control = run_untraced_control('immediate-unlink', immediate_owner)
assert control['returncode'] == result, (control, result)
assert control['argv'] == [bash, '-e', str(case / 'analysis-step.sh')]
assert control['environment']['TMPDIR'] == str(case / 'owner-tmp')
assert (case / 'evidence/lint-stdout.txt').read_bytes() == control['stdout']
assert (case / 'evidence/lint-stderr.txt').read_bytes() == control['stderr']

relative_dirfd_owner = '''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.dirfd"
mkdir -p "$d/output"
printf 'relative-out\\n' > "$d/output/shard.4.out"
printf '4\\n' > "$d/output/shard.4.rc"
python3 - "$d/output" <<'PY'
import os
import sys
directory = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.unlink('shard.4.out', dir_fd=directory)
finally:
    os.close(directory)
PY
rm -f "$d/output/shard.4.rc"
rm -rf "$d"
'''
case, _output, result, summary = run_tracer('relative-dirfd-unlinkat', relative_dirfd_owner)
assert result == 0 and summary['finalized'], (result, summary)
assert_captured(case, 'fm-lint.dirfd/output/shard.4.out', b'relative-out\n')
assert_captured(case, 'fm-lint.dirfd/output/shard.4.rc', b'4\n')
assert any(event.get('syscall') == 'unlinkat' and event.get('path', '').endswith('shard.4.out')
           for event in summary['unlink_events']), summary

outside_root = run_root / 'outside-root'
outside_target = outside_root / 'fm-lint.external/output/shard.0.out'
outside_target.parent.mkdir(mode=0o700, parents=True)
outside_target.write_text('outside-same-name\n')
os.environ['FIXTURE_OUTSIDE_ROOT'] = str(outside_root)
outside_owner = '''#!/usr/bin/env bash
set -eu
rm -f "$FIXTURE_OUTSIDE_ROOT/fm-lint.external/output/shard.0.out"
'''
case, _output, result, summary = run_tracer('outside-root-same-name', outside_owner)
assert result == 0 and summary['finalized'], (result, summary)
assert not outside_target.exists()
assert not any(name.endswith('shard.0.out') for name in summary['files']), summary

append_owner = '''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.append"
mkdir -p "$d/output"
exec 3>"$d/output/shard.0.out"
printf 'prefix\\n' >&3
rm -f "$d/output/shard.0.out"
printf 'final-after-unlink\\n' >&3
exec 3>&-
rm -rf "$d"
'''
case, _output, result, summary = run_tracer('append-after-unlink', append_owner)
assert result == 0 and summary['finalized'], (result, summary)
assert_captured(case, 'fm-lint.append/output/shard.0.out',
                b'prefix\nfinal-after-unlink\n')

fatal_owner = '''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.fatal"
mkdir -p "$d/output"
printf 'fatal-diagnostic\\n' > "$d/output/shard.0.out"
rm -rf "$d"
exit 23
'''
case, _output, result, summary = run_tracer('fatal-owner', fatal_owner)
assert result == 23 and summary['owner_wait']['status'] == 23, (result, summary)
assert summary['finalized'], summary
assert_captured(case, 'fm-lint.fatal/output/shard.0.out', b'fatal-diagnostic\n')
fatal_control = run_untraced_control('fatal-owner', fatal_owner)
assert fatal_control['returncode'] == result
assert (case / 'evidence/lint-stdout.txt').read_bytes() == fatal_control['stdout']
assert (case / 'evidence/lint-stderr.txt').read_bytes() == fatal_control['stderr']

overhead_owner = '''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.overhead"
mkdir -p "$d/output"
python3 - "$d/output" <<'PY'
import os
import sys
directory = sys.argv[1]
for _ in range(10000):
    os.stat('/etc/hosts')
for index in range(100):
    with open(os.path.join(directory, f'shard.{index}.out'), 'w') as stream:
        stream.write(f'{index}\\n')
PY
rm -f "$d"/output/shard.*.out
rm -rf "$d"
'''
if os.environ.get('LAB_SCENARIO') == 'serial':
    overhead_control = run_untraced_control('fixed-overhead', overhead_owner)
    overhead_case, overhead_output, overhead_status, overhead_summary = run_tracer(
        'fixed-overhead', overhead_owner)
    overhead_order = ['control', 'traced']
else:
    overhead_case, overhead_output, overhead_status, overhead_summary = run_tracer(
        'fixed-overhead', overhead_owner)
    overhead_control = run_untraced_control('fixed-overhead', overhead_owner)
    overhead_order = ['traced', 'control']
assert overhead_status == overhead_control['returncode'] == 0
assert len([event for event in overhead_summary['unlink_events']
            if event.get('opened_before_resume') and event.get('path', '').endswith('.out')]) >= 100
overhead_comparison = {
    'owner': 'fixed 10000 harmless stat syscalls plus 100 shard deletions',
    'order': overhead_order,
    'traced': json.loads((overhead_output / 'traced-measurements.json').read_text()),
    'control': {key: overhead_control[key] for key in (
        'wall_seconds', 'child_cpu_user_seconds', 'child_cpu_system_seconds')},
    'traced_exit': overhead_status,
    'control_exit': overhead_control['returncode'],
    'traced_raw_waits': overhead_summary['terminal_waits'],
    'event_count': overhead_summary['seccomp_event_count'],
    'maximum_held_descriptors': overhead_summary['maximum_held_descriptors'],
    'retained_bytes': overhead_summary['retained_bytes'],
}
(run_root / 'fixed-overhead-comparison.json').write_text(
    json.dumps(overhead_comparison, indent=2) + '\n')

signal_owner = '''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.signal"
mkdir -p "$d/output"
printf 'before-term\\n' > "$d/output/shard.0.out"
on_term() {
  printf 'final-term-diagnostic\\n' >> "$d/output/shard.0.out"
  rm -rf "$d"
  exit 143
}
trap on_term TERM
touch "$FIXTURE_TRAP_READY"
while :; do sleep 0.05; done
'''
for name, signum, trap_name, owner_status in (
    ('term-final-write', signal.SIGTERM, 'TERM', 143),
    ('int-final-write', signal.SIGINT, 'INT', 130),
    ('hup-final-write', signal.SIGHUP, 'HUP', 129),
):
    owner_text = signal_owner.replace('trap on_term TERM', f'trap on_term {trap_name}')
    owner_text = owner_text.replace('exit 143', f'exit {owner_status}')
    case, _output, result, summary = run_tracer(
        name, owner_text, timeout=4, send_signal=signum
    )
    assert result == owner_status and summary['owner_wait']['status'] == owner_status, (
        name, result, summary
    )
    assert summary['finalized'], summary
    assert_captured(case, 'fm-lint.signal/output/shard.0.out',
                    b'before-term\nfinal-term-diagnostic\n')

case, _output, result, summary = run_tracer(
    'unsupported-group-stop', signal_owner, timeout=4, send_signal=signal.SIGSTOP)
assert result == 125 and not summary['finalized'], (result, summary)
assert any('job-control group stop is unsupported' in error for error in summary['errors']), summary

outer_signal_results = [
    run_supervisor_signal_case(f'outer-{signal.Signals(signum).name.lower()}', signum)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
]
sampler_failure_results = [
    run_sampler_failure_case('sampler-startup-failure', 'startup'),
    run_sampler_failure_case('sampler-during-analysis-failure', 'during'),
]

outside = run_root / 'symlink-outside.txt'
outside.write_text('must-remain\n')
symlink_owner = '''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.symlink"
mkdir -p "$d/output"
ln -s "$FIXTURE_OUTSIDE" "$d/output/shard.0.out"
rm -f "$d/output/shard.0.out"
'''
os.environ['FIXTURE_OUTSIDE'] = str(outside)
case, _output, result, summary = run_tracer('symlink-fail-closed', symlink_owner)
assert result == 125 and not summary['finalized'], (result, summary)
assert outside.read_text() == 'must-remain\n'
assert any('openat2' in error or 'not a regular file' in error
           for error in summary['errors']), summary

fifo_owner = '''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.fifo"
mkdir -p "$d/output"
mkfifo "$d/output/shard.0.out"
rm -f "$d/output/shard.0.out"
'''
case, _output, result, summary = run_tracer('fifo-fail-closed', fifo_owner)
assert result == 125 and not summary['finalized'], (result, summary)
assert (case / 'owner-tmp/fm-lint.fifo/output/shard.0.out').is_fifo()

intermediate_owner = '''#!/usr/bin/env bash
set -eu
d="$TMPDIR/fm-lint.intermediate"
mkdir -p "$d/output"
printf 'preserve-through-alias\\n' > "$d/output/shard.0.out"
ln -s output "$d/output-alias"
rm -f "$d/output-alias/shard.0.out"
'''
case, _output, result, summary = run_tracer('intermediate-symlink-fail-closed', intermediate_owner)
assert result == 125 and not summary['finalized'], (result, summary)
assert (case / 'owner-tmp/fm-lint.intermediate/output/shard.0.out').read_text() == 'preserve-through-alias\n'

for injection in ('before-unlink', 'after-buffer-hold'):
    case, _output, result, summary = run_tracer(
        f'injected-{injection}', immediate_owner, failure_injection=injection)
    assert result == 125 and not summary['finalized'], (injection, result, summary)
    assert (case / 'owner-tmp/fm-lint.immediate/output/shard.0.out').read_text() == 'immediate-output\n'
    assert any(event.get('stage') == injection for event in summary['unlink_events']), summary

case, _output, result, summary = run_tracer(
    'injected-after-owner-exit', fatal_owner, failure_injection='after-owner-exit')
assert result == 125 and not summary['finalized'], (result, summary)
assert summary['owner_wait']['status'] == 23
assert_captured(case, 'fm-lint.fatal/output/shard.0.out', b'fatal-diagnostic\n')

for injection in ('openat2-unavailable', 'descriptor-exhaustion', 'storage-failure'):
    case, _output, result, summary = run_tracer(
        f'injected-{injection}', immediate_owner, failure_injection=injection)
    assert result == 125 and not summary['finalized'], (injection, result, summary)

case, _output, result, summary = run_tracer(
    'injected-seccomp-unavailable', immediate_owner,
    failure_injection='seccomp-unavailable')
assert result == 125 and not summary['finalized'], (result, summary)

case, _output, result, summary = run_tracer(
    'injected-root-bootstrap-failure', immediate_owner,
    failure_injection='bootstrap-failure')
assert result == 125 and not summary['finalized'], (result, summary)
assert any(wait.get('role') == 'step-shell' and wait.get('kind') in ('exit', 'signal')
           for wait in summary['terminal_waits']), summary

fork_owner = '''#!/usr/bin/env bash
set -eu
( exit 0 ) &
wait
'''
case, _output, result, summary = run_tracer(
    'injected-birth-enrichment-failure', fork_owner,
    failure_injection='birth-enrichment-failure')
assert result == 125 and not summary['finalized'], (result, summary)
assert any('injected failure after ptrace child birth registration' in error
           for error in summary['errors']), summary

thread_exec_owner = '''#!/usr/bin/env python3
import os
import threading
child = os.fork()
if child == 0:
    os._exit(0)
spawned = os.posix_spawn('/usr/bin/true', ['true'], os.environ.copy())
def exec_from_nonleader():
    command = r"""d="$TMPDIR/fm-lint.thread-exec"
mkdir -p "$d/output"
printf 'thread-exec\n' > "$d/output/shard.7.out"
rm -f "$d/output/shard.7.out"
rm -rf "$d"
"""
    os.execv('/usr/bin/bash', ['/usr/bin/bash', '-c', command])
thread = threading.Thread(target=exec_from_nonleader)
thread.start()
thread.join()
raise SystemExit(126)
'''
case, _output, result, summary = run_tracer(
    'fork-clone-nonleader-exec', thread_exec_owner, owner_filename='owner.py',
    owner_executable=python)
assert result == 0 and summary['finalized'], (result, summary)
assert_captured(case, 'fm-lint.thread-exec/output/shard.7.out', b'thread-exec\n')
assert any(event.get('event') == 'exec-tid-remap'
           for event in summary['unlink_events']), summary
assert any(event.get('event') == 'birth' and event.get('kind') == 2
           for event in summary['unlink_events']), summary

orphan_owner = '''#!/usr/bin/env bash
set -eu
sleep 30 &
exit 0
'''
case, _output, result, summary = run_tracer('orphan-retirement', orphan_owner, timeout=3)
assert result == 125 and not summary['finalized'], (result, summary)
assert not summary['unretired_tasks'], summary
assert any(wait.get('role') == 'tracee-descendant'
           for wait in summary['terminal_waits']), summary

supervisor_case = run_root / 'sampler-exit-at-owner-completion'
supervisor_source = supervisor_case / 'source'
supervisor_recipe = supervisor_case / 'recipe'
supervisor_output = supervisor_case / 'evidence'
supervisor_source.mkdir(mode=0o700, parents=True)
supervisor_recipe.mkdir(mode=0o700)
supervisor_output.mkdir(mode=0o700)
for filename in ('analysis-supervisor.py', 'trace_capture.py', 'owned_child.py',
                 'wait_owner.py', 'cgroup-snapshot.py', 'readiness_gate.py'):
    shutil.copy2(recipe / filename, supervisor_recipe / filename)
write(supervisor_recipe / 'sample.sh', '''#!/usr/bin/env bash
set -eu
printf 'ready\\n' > "$1/resource-sampler-ready.txt"
trap 'exit 7' TERM
while [[ ! -f "$1/owner-finished" ]]; do
  read -r -t 0.01 _ || true
done
exit 7
''', executable=True)
write(supervisor_source / 'bin/fm-lint.sh',
      '#!/usr/bin/env bash\ntouch "$FIXTURE_ROOT/owner-finished"\nexit 0\n', executable=True)
supervisor_step = supervisor_case / 'analysis-step.sh'
write(supervisor_step, 'bin/fm-lint.sh\n')
supervisor_env = os.environ.copy()
supervisor_env.update({'SOURCE_COMMIT': 'fixture-source',
                       'FIXTURE_ROOT': str(supervisor_output)})
supervisor_start = time.monotonic()
supervisor_job_start = time.time()
(supervisor_output / 'job-start-epoch.txt').write_text(f'{supervisor_job_start}\n')
(supervisor_output / 'job-start-monotonic-ns.txt').write_text(
    f'{int(supervisor_start * 1_000_000_000)}\n')
for phase, result_text in (('PREPARE', 'fixture'), ('QUALIFY', 'fixture'), ('ARM', 'fixture')):
    subprocess.run([python, str(recipe / 'lifecycle_record.py'), str(supervisor_output),
                    phase, result_text], check=True)
supervisor_command = [python, str(supervisor_recipe / 'analysis-supervisor.py'),
                      'baseline', '8', str(supervisor_start + 20),
                      str(supervisor_start + 25), str(supervisor_output),
                      str(supervisor_source), str(supervisor_recipe),
                      str(supervisor_step), bash]
(supervisor_output / 'command.json').write_text(json.dumps(supervisor_command) + '\n')
supervisor_log = (supervisor_output / 'supervisor.stdout-stderr.log').open('wb')
supervisor_process = subprocess.Popen(supervisor_command, cwd=supervisor_source,
                                      env=supervisor_env, stdout=supervisor_log,
                                      stderr=subprocess.STDOUT, start_new_session=True)
try:
    supervisor_status = supervisor_process.wait(timeout=8)
except subprocess.TimeoutExpired as error:
    raise AssertionError('observer completion-boundary fixture exceeded 8s') from error
finally:
    if supervisor_process.poll() is None:
        supervisor_process.send_signal(signal.SIGTERM)
        try:
            supervisor_process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            supervisor_process.kill()
            kill_owned_session(supervisor_process)
            supervisor_process.wait(timeout=2)
    (supervisor_output / 'fixture-cleanup.json').write_text(json.dumps({
        'supervisor_returncode': supervisor_process.poll(),
        'cleanup': 'direct child waited; pid-only KILL fallback only if required',
    }, indent=2) + '\n')
    supervisor_log.close()
supervisor_outcome = json.loads((supervisor_output / 'analysis-outcome.json').read_text())
assert supervisor_status == 125, (supervisor_status, supervisor_outcome)
assert supervisor_outcome['owner_step_wait_status'] in (0, 143), supervisor_outcome
assert supervisor_outcome['result'] == 'observer-failure', supervisor_outcome

summary = {
    'fixture_only': True,
    'host_os': platform.system(),
    'kernel_release': platform.release(),
    'cases': [
        'immediate create-write-unlink without owner handshake',
        'final append through retained descriptor after unlink',
        'owner-shaped shard .out and .rc buffers',
        'absolute unlink plus relative unlinkat dirfd and outside-root same-name target',
        'fatal owner exit 23 preserved separately',
        'TERM forwarded to owner trap with final diagnostic before unlink',
        'INT and HUP forwarded with original owner trap statuses',
        'outer supervisor INT TERM HUP with trap-ready barrier and pidfd delivery',
        'final and intermediate symlink plus FIFO targets rejected before unlink',
        'fork, vfork, thread clone, rapid exit, nonleader exec remap, and orphan retirement',
        'tracer failures before unlink, after exact buffer hold, and after durable owner exit',
        'ptrace, seccomp, openat2, x32, descriptor, storage, and malformed path refusal',
        'tracer group stop refused as unsupported',
        'reused numeric root identity refused without signal',
        'resource observer startup, during-analysis, and owner-completion failures',
        'fixed 10000 harmless syscalls plus 100 deletion traced/control comparison',
        'common finalization deadline refuses late evidence writes before creating a target',
    ],
    'outer_signal_results': outer_signal_results,
    'sampler_failure_results': sampler_failure_results,
    'fixed_overhead_comparison': overhead_comparison,
    'native_capacity_evidence': False,
}
(run_root / 'fixture-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
sys.path.insert(0, str(recipe))
subprocess.run([python, str(recipe / 'lifecycle_record.py'), str(run_root), 'ARM',
                'all declared fixture obligations passed'], check=True)
print(json.dumps(summary, indent=2))
