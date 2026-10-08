#!/usr/bin/env python3
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


recipe = Path(__file__).resolve().parent
run_root = Path(os.environ['LAB_RUN_ROOT']) / 'linux-tracer-fixtures'
bash = shutil.which('bash')
python = sys.executable
if platform.system() != 'Linux' or platform.machine() != 'x86_64':
    raise SystemExit('Linux x86_64 fixtures are required; no platform substitution is allowed')
if not bash:
    raise SystemExit('fixture checks require bash')
run_root.mkdir(mode=0o700, parents=True, exist_ok=True)


def write(path, contents, executable=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(contents)
    if executable:
        path.chmod(0o755)


def verify_pid_reuse_refusal():
    sys.path.insert(0, str(recipe))
    from owned_child import OwnedChild

    class FakeProcess:
        pid = 424242

        @staticmethod
        def poll():
            return None

    sent = []
    owner = OwnedChild(
        FakeProcess(), 99, 'original-start',
        identity_reader=lambda _pid: 'reused-unrelated-start',
        signal_sender=lambda pidfd, signum: sent.append((pidfd, signum)),
    )
    assert not owner.send(signal.SIGTERM)
    assert sent == [], sent
    return {'recorded_identity': 'original-start',
            'observed_identity': 'reused-unrelated-start',
            'signals_sent': sent, 'result': 'refused'}


def run_tracer(name, owner_text, timeout=4, send_signal=None):
    case = run_root / name
    source = case / 'source'
    output = case / 'evidence'
    owner_tmp = case / 'owner-tmp'
    retained = output / 'owner-output-retained'
    source.mkdir(mode=0o700, parents=True)
    owner_tmp.mkdir(mode=0o700)
    retained.mkdir(mode=0o700, parents=True)
    owner = source / 'owner.sh'
    step = case / 'analysis-step.sh'
    write(owner, owner_text, executable=True)
    write(step, 'owner.sh\n')
    stdout_path = output / 'owner.stdout'
    stderr_path = output / 'owner.stderr'
    ready_path = output / 'tracer-ready.txt'
    tracer_stdout = (output / 'tracer.stdout').open('wb')
    tracer_stderr = (output / 'tracer.stderr').open('wb')
    environment = os.environ.copy()
    environment.update({'TMPDIR': str(owner_tmp), 'CI': 'true',
                        'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C'})
    command = [python, str(recipe / 'capture-owner-buffers.py'),
               '--owner-tmp', str(owner_tmp), '--destination', str(retained),
               '--cwd', str(source), '--stdout', str(stdout_path),
               '--stderr', str(stderr_path), '--ready', str(ready_path),
               '--cleanup-deadline', str(time.time() + timeout),
               '--owner-exit', str(output / 'owner-step.exit'),
               '--', bash, '-e', str(step)]
    (output / 'command.json').write_text(json.dumps({
        'argv': command, 'cwd': str(source), 'TMPDIR': str(owner_tmp),
        'environment': {'CI': 'true', 'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C',
                        'FM_LINT_JOBS': 'unset', 'FM_LINT_TELEMETRY': 'unset'},
        'timeout_seconds': timeout, 'signal': send_signal,
    }, indent=2) + '\n')
    process = subprocess.Popen(command, cwd=source, env=environment,
                               stdout=tracer_stdout, stderr=tracer_stderr,
                               start_new_session=True)
    cleanup = {'term_sent': False, 'kill_group_sent': False}
    try:
        ready_deadline = time.monotonic() + min(timeout, 2)
        while not ready_path.exists() and process.poll() is None and time.monotonic() < ready_deadline:
            time.sleep(0.01)
        if send_signal is not None:
            if not ready_path.exists():
                raise AssertionError(f'{name}: tracer readiness marker missing')
            process.send_signal(send_signal)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise AssertionError(f'{name}: fixture exceeded {timeout}s') from error
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            cleanup['term_sent'] = True
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # This is only the private session created for this fixture case.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    cleanup['kill_group_sent'] = True
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        cleanup['returncode'] = process.poll()
        (output / 'cleanup.json').write_text(json.dumps(cleanup, indent=2) + '\n')
        tracer_stdout.close()
        tracer_stderr.close()
    summary_path = retained / 'capture-summary.json'
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
    (output / 'tracer-exit.txt').write_text(f'{returncode}\n')
    if summary is None:
        raise AssertionError(f'{name}: capture summary missing')
    return case, output, returncode, summary


def assert_captured(case, relative, expected):
    actual = (case / 'evidence/owner-output-retained' / relative).read_bytes()
    assert actual == expected, (relative, actual, expected)


def run_untraced_control(name, owner_text):
    case = run_root / f'{name}-untraced-control'
    source = case / 'source'
    output = case / 'evidence'
    owner_tmp = case / 'owner-tmp'
    source.mkdir(mode=0o700, parents=True)
    output.mkdir(mode=0o700)
    owner_tmp.mkdir(mode=0o700, parents=True)
    owner = source / 'owner.sh'
    step = case / 'analysis-step.sh'
    write(owner, owner_text, executable=True)
    write(step, 'owner.sh\n')
    environment = os.environ.copy()
    environment.update({'TMPDIR': str(owner_tmp), 'CI': 'true',
                        'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C'})
    command = [bash, '-e', str(step)]
    (output / 'command.json').write_text(json.dumps({
        'argv': command, 'cwd': str(source), 'TMPDIR': str(owner_tmp),
        'environment': {'CI': 'true', 'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C',
                        'FM_LINT_JOBS': 'unset', 'FM_LINT_TELEMETRY': 'unset'},
    }, indent=2) + '\n')
    result = subprocess.run(command, cwd=source, env=environment,
                            capture_output=True, timeout=3, check=False)
    (output / 'stdout.bin').write_bytes(result.stdout)
    (output / 'stderr.bin').write_bytes(result.stderr)
    (output / 'exit.txt').write_text(f'{result.returncode}\n')
    return result.returncode


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
control_status = run_untraced_control('immediate-unlink', immediate_owner)
assert control_status == result, (control_status, result)

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

supervisor_case = run_root / 'sampler-exit-at-owner-completion'
supervisor_source = supervisor_case / 'source'
supervisor_recipe = supervisor_case / 'recipe'
supervisor_output = supervisor_case / 'evidence'
supervisor_source.mkdir(mode=0o700, parents=True)
supervisor_recipe.mkdir(mode=0o700)
supervisor_output.mkdir(mode=0o700)
for filename in ('analysis-supervisor.py', 'capture-owner-buffers.py',
                 'owned_child.py', 'cgroup-snapshot.py'):
    shutil.copy2(recipe / filename, supervisor_recipe / filename)
write(supervisor_recipe / 'sample.sh', '''#!/usr/bin/env bash
set -eu
exec python3 "$3/sample.py" "$1" "$2" "$3"
''', executable=True)
write(supervisor_recipe / 'sample.py', '''#!/usr/bin/env python3
import sys
import time
from pathlib import Path
root = Path(sys.argv[1])
while not (root / 'owner-finished').exists():
    time.sleep(0.01)
time.sleep(0.05)
raise SystemExit(7)
''')
write(supervisor_source / 'bin/fm-lint.sh',
      '#!/usr/bin/env bash\ntouch "$FIXTURE_ROOT/owner-finished"\nexit 0\n', executable=True)
supervisor_step = supervisor_case / 'analysis-step.sh'
write(supervisor_step, 'bin/fm-lint.sh\n')
supervisor_env = os.environ.copy()
supervisor_env.update({'SOURCE_COMMIT': 'fixture-source',
                       'FIXTURE_ROOT': str(supervisor_output)})
supervisor_command = [python, str(supervisor_recipe / 'analysis-supervisor.py'),
                      'baseline', '8', str(int(time.time()) + 20),
                      str(int(time.time()) + 25), str(supervisor_output),
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
            try:
                os.killpg(supervisor_process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            supervisor_process.wait(timeout=2)
    (supervisor_output / 'fixture-cleanup.json').write_text(json.dumps({
        'supervisor_returncode': supervisor_process.poll(),
        'cleanup': 'direct child waited; owned session KILL fallback only if required',
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
        'fatal owner exit 23 preserved separately',
        'TERM forwarded to owner trap with final diagnostic before unlink',
        'INT and HUP forwarded with original owner trap statuses',
        'final symlink rejected before unlink and outside target retained',
        'reused numeric root identity refused without signal',
        'resource observer failure at owner completion invalidates wrapper',
    ],
    'native_capacity_evidence': False,
}
(run_root / 'fixture-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary, indent=2))
