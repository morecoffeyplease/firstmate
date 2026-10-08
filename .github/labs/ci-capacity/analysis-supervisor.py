#!/usr/bin/env python3
import json
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from owned_child import OwnedChild
from readiness_gate import record_readiness
from trace_capture import run_capture


scenario, cap_text, analysis_deadline_text, cleanup_deadline_text, root_text, source_text, recipe_text, step_text, bash_path = sys.argv[1:]
cap_seconds = int(cap_text)
analysis_deadline_epoch = float(analysis_deadline_text)
cleanup_deadline_epoch = float(cleanup_deadline_text)
root = Path(root_text)
source_root = Path(source_text)
recipe_root = Path(recipe_text)
step_file = Path(step_text)
signal_received = None
collection_errors = []
sampler = None
sampler_identity = None
sampler_owner = None
sampler_stream = None
stdout_stream = None
stderr_stream = None
LIFECYCLE = ('PREPARE', 'QUALIFY', 'ARM', 'RUN', 'RETIRE', 'FINALIZE', 'SEALED')
lifecycle_index = -1


class ReadinessDeadlineExpired(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def write_status(values, required=False):
    payload = dict(values)
    payload['updated_utc'] = utc_now()
    target = root / 'analysis-outcome.json'
    temporary = root / 'analysis-outcome.json.tmp'
    try:
        temporary.write_text(json.dumps(payload, indent=2) + '\n')
        os.replace(temporary, target)
    except OSError as error:
        message = f'write analysis outcome: {type(error).__name__}: {error}'
        collection_errors.append(message)
        try:
            with (root / 'supervisor-errors.log').open('a') as stream:
                stream.write(message + '\n')
        except OSError:
            pass
        if required:
            return False
    return True


def persist_status(values):
    if not write_status(values, required=True):
        raise OSError('could not persist the durable analysis outcome')


def append_phase(phase, result):
    try:
        with (root / 'phase-journal.tsv').open('a') as stream:
            stream.write(f'{utc_now()}\t{phase}\t{result}\n')
    except OSError as error:
        collection_errors.append(f'phase journal {phase}: {error}')


def advance_lifecycle(phase, result):
    global lifecycle_index
    expected = lifecycle_index + 1
    if expected >= len(LIFECYCLE) or LIFECYCLE[expected] != phase:
        raise RuntimeError(
            f'lifecycle transition refused: current={LIFECYCLE[lifecycle_index] if lifecycle_index >= 0 else "none"}; '
            f'next={LIFECYCLE[expected] if expected < len(LIFECYCLE) else "none"}; requested={phase}'
        )
    path = root / 'lifecycle-journal.tsv'
    with path.open('a') as stream:
        stream.write(f'{utc_now()}\t{phase}\t{result}\n')
        stream.flush()
        os.fsync(stream.fileno())
    lifecycle_index = expected


def advance_to_finalize(reason):
    while lifecycle_index < LIFECYCLE.index('FINALIZE'):
        next_phase = LIFECYCLE[lifecycle_index + 1]
        result = 'entered' if next_phase == 'FINALIZE' else f'skipped: {reason}'
        advance_lifecycle(next_phase, result)


def mark_signal(signum, _frame):
    global signal_received
    if signal_received is None:
        signal_received = signum


def process_listing_command():
    if platform.system() == 'Linux':
        return ['ps', '-ww', '-eo', 'pid,ppid,pgid,etimes,time,pcpu,rss,vsz,stat,args']
    return ['ps', '-ww', '-axo', 'pid,ppid,pgid,etime,time,%cpu,rss,vsz,stat,command']


def signal_child(owner, signum, label):
    process = owner.process if owner is not None else None
    if process is None or process.poll() is not None:
        return False
    try:
        sent = owner.send(signum)
    except OSError as error:
        collection_errors.append(f'{label} signal {signum}: {error}')
        return False
    if not sent:
        collection_errors.append(f'{label} identity/pidfd check refused signal {signum}')
    return sent


def stop_sampler(deadline):
    if sampler is None:
        return {'started': False}
    if sampler.poll() is not None:
        return {'started': True, 'returncode': sampler.returncode, 'unexpected_exit': True}
    term_sent = signal_child(sampler_owner, signal.SIGTERM, 'resource sampler')
    if sampler_owner is None and sampler.poll() is None:
        sampler.send_signal(signal.SIGTERM)
        term_sent = True
    term_deadline = min(time.monotonic() + 2, deadline)
    while sampler.poll() is None and time.monotonic() < term_deadline:
        time.sleep(min(0.05, max(deadline - time.monotonic(), 0)))
    kill_sent = False
    if sampler.poll() is None:
        kill_sent = signal_child(sampler_owner, signal.SIGKILL, 'resource sampler')
        if sampler_owner is None and sampler.poll() is None:
            sampler.kill()
            kill_sent = True
        try:
            sampler.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            collection_errors.append('resource sampler did not exit by the common cleanup deadline')
    return {'started': True, 'term_sent': term_sent, 'kill_sent': kill_sent,
            'returncode': sampler.poll(), 'requested_stop': True}


def collect_file(command, destination, deadline):
    try:
        if time.monotonic() >= deadline:
            collection_errors.append(f'{destination}: common cleanup deadline expired before collection')
            return
        if command[0] == 'read':
            source = Path(command[1])
            content = source.read_bytes() if source.is_file() else b'unavailable on this platform\n'
            (root / destination).write_bytes(content)
            return
        remaining = max(deadline - time.monotonic(), 0)
        if remaining <= 0:
            collection_errors.append(f'{destination}: common cleanup deadline expired before collection')
            return
        result = subprocess.run(command, check=False, capture_output=True,
                                timeout=min(2, remaining))
        (root / destination).write_bytes(result.stdout)
        if result.stderr:
            (root / (destination + '.stderr')).write_bytes(result.stderr)
        if result.returncode != 0:
            collection_errors.append(f'{destination}: exit {result.returncode}')
    except (OSError, subprocess.TimeoutExpired) as error:
        collection_errors.append(f'{destination}: {type(error).__name__}: {error}')


def collect_after(deadline):
    for source, target in (
        ('/proc/meminfo', 'meminfo-after.txt'),
        ('/proc/swaps', 'swaps-after.txt'),
        ('/proc/vmstat', 'vmstat-after.txt'),
        ('/proc/self/cgroup', 'cgroup-after.txt'),
    ):
        collect_file(['read', source], target, deadline)
    collect_file(process_listing_command(), 'processes-after.txt', deadline)
    collect_file([sys.executable, str(recipe_root / 'cgroup-snapshot.py')],
                 'cgroup-metrics-after.txt', deadline)


def start_sampler(bash_path):
    global sampler, sampler_identity, sampler_owner, sampler_stream
    sampler_stream = (root / 'resource-sampler.stdout.txt').open('wb')
    sampler = subprocess.Popen(
        [bash_path, str(recipe_root / 'sample.sh'), str(root), '10', str(recipe_root)],
        stdout=sampler_stream, stderr=subprocess.STDOUT,
    )
    sampler_owner = OwnedChild.bind(sampler)
    sampler_identity = sampler_owner.identity
    return sampler_stream


def close_stream(stream):
    if stream is not None:
        try:
            stream.close()
        except OSError as error:
            collection_errors.append(f'close stream: {error}')


for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(signum, mark_signal)

outcome = {
    'scenario': scenario,
    'source_commit': os.environ.get('SOURCE_COMMIT'),
    'phase': 'supervisor-start',
    'wrapper_exit': None,
    'owner_step_wait_status': None,
    'signal': None,
    'analysis_limit_seconds': cap_seconds,
    'analysis_deadline_epoch': analysis_deadline_epoch,
    'required_analysis_start_deadline_epoch': None,
    'cleanup_deadline_epoch': cleanup_deadline_epoch,
    'analysis_pid': None,
    'observer_results': [],
    'cleanup': None,
    'limitations': [
        'SIGKILL or runner destruction can prevent finalization and artifact upload.',
        'Uninterruptible kernel tasks may remain after bounded KILL escalation.',
        'PTRACE_O_EXITKILL kills tracees if the sole controller is destroyed before evidence finalization.',
    ],
}

try:
    persist_status(outcome)
    advance_lifecycle('PREPARE', 'begin')
    append_phase('supervisor-start', 'in-progress')
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('the owning analysis path requires Linux x86_64')
    pidfd_probe = os.pidfd_open(os.getpid(), 0)
    signal.pidfd_send_signal(pidfd_probe, 0)
    os.close(pidfd_probe)
    (root / 'pidfd-preflight.txt').write_text(
        f'pidfd_open=pass\npidfd_send_signal_zero=pass\npid={os.getpid()}\n'
    )
    remaining = max(0, analysis_deadline_epoch - time.time())
    analysis_budget = min(cap_seconds, int(remaining))
    outcome['analysis_budget_seconds'] = analysis_budget
    (root / 'analysis-budget.txt').write_text(
        f'scenario={scenario}\nanalysis_limit_seconds={cap_seconds}\n'
        f'analysis_budget_seconds={analysis_budget}\n'
        f'analysis_deadline_epoch={analysis_deadline_epoch}\n'
        f'cleanup_deadline_epoch={cleanup_deadline_epoch}\n'
    )
    owner_tmp = root / 'owner-tmp'
    retained = root / 'owner-output-retained'
    owner_tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    retained.mkdir(mode=0o700, parents=True, exist_ok=True)
    cgroup_before = subprocess.run(
        [sys.executable, str(recipe_root / 'cgroup-snapshot.py')],
        check=False, capture_output=True, timeout=2,
    )
    (root / 'cgroup-metrics-before.txt').write_bytes(cgroup_before.stdout)
    if cgroup_before.stderr or cgroup_before.returncode != 0:
        collection_errors.append(f'cgroup before: exit {cgroup_before.returncode}')
    for source, target in (
        ('/proc/meminfo', 'meminfo-before.txt'),
        ('/proc/swaps', 'swaps-before.txt'),
        ('/proc/vmstat', 'vmstat-before.txt'),
        ('/proc/self/cgroup', 'cgroup-before.txt'),
    ):
        (root / target).write_bytes(Path(source).read_bytes())
    before = subprocess.run(process_listing_command(), check=False,
                            capture_output=True, timeout=2)
    (root / 'processes-before.txt').write_bytes(before.stdout)
    if before.returncode != 0:
        collection_errors.append(f'processes before: exit {before.returncode}')
    advance_lifecycle('QUALIFY', 'linux-x86_64-pidfd-and-source-environment-recorded')

    sampler_stream = start_sampler(bash_path)
    job_start_epoch = float((root / 'job-start-epoch.txt').read_text())
    readiness = record_readiness(root, 'analysis-supervisor-ready', job_start_epoch + 60)
    outcome['required_analysis_start_deadline_epoch'] = readiness['required_analysis_start_deadline_epoch']
    outcome['analysis_readiness'] = readiness
    outcome.update({'phase': 'resource-observer-started', 'owner_tmp': str(owner_tmp),
                    'retained_output': str(retained), 'sampler_pid': sampler.pid,
                    'sampler_identity': sampler_identity})
    persist_status(outcome)
    append_phase('resource-observer-started', 'in-progress')
    if sampler.poll() is not None:
        collection_errors.append(f'resource sampler failed before analysis: {sampler.returncode}')
        wrapper_status = 125
        result = 'observer-failed-before-analysis'
        owner_status = None
        summary = None
        phase = 'observer-startup-failure'
    elif analysis_budget <= 0:
        wrapper_status = 125
        result = 'analysis-not-started-cleanup-reserve'
        owner_status = None
        summary = None
        phase = 'analysis-not-started'
    elif not readiness['admitted']:
        wrapper_status = 125
        result = 'setup-window-expired-before-analysis'
        owner_status = None
        summary = None
        phase = 'analysis-not-started-readiness-deadline'
    elif signal_received is not None:
        wrapper_status = 128 + signal_received
        result = 'supervisor-signal-before-analysis'
        owner_status = None
        summary = None
        phase = 'supervisor-signal-before-analysis'
    else:
        environment = os.environ.copy()
        environment.pop('FM_LINT_TELEMETRY', None)
        if scenario == 'serial':
            environment['FM_LINT_JOBS'] = '1'
        else:
            environment.pop('FM_LINT_JOBS', None)
        environment.update({'CI': 'true', 'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C',
                            'TMPDIR': str(owner_tmp)})
        command = [bash_path, '-e', str(step_file)]
        (root / 'lint-command.txt').write_text(json.dumps({
            'argv': command, 'cwd': str(source_root),
            'environment': {'CI': 'true', 'GITHUB_ACTIONS': 'true', 'LC_ALL': 'C',
                            'FM_LINT_JOBS': environment.get('FM_LINT_JOBS', 'unset'),
                            'FM_LINT_TELEMETRY': 'unset', 'TMPDIR': str(owner_tmp),
                            'PATH': environment.get('PATH')},
        }, indent=2) + '\n')
        launch_readiness = record_readiness(
            root, 'analysis-owner-launch', job_start_epoch + 60
        )
        outcome['analysis_owner_launch_readiness'] = launch_readiness
        persist_status(outcome)
        if not launch_readiness['admitted']:
            raise ReadinessDeadlineExpired(
                'setup exceeded the one-minute deadline before owner launch; experiment pair is invalid'
            )
        advance_lifecycle('ARM', 'observer-ready-and-owner-command-qualified')
        outcome.update({'phase': 'owner-step-running', 'owner_command': command,
                        'owner_cwd': str(source_root), 'analysis_started_utc': utc_now(),
                        'controller_pid': os.getpid(),
                        'trace_controller': 'synchronous in-process ptrace reducer'})
        persist_status(outcome)
        append_phase('owner-step-running', 'in-progress')
        advance_lifecycle('RUN', 'single-controller-owner-exec-admitted-pending')
        analysis_deadline = min(time.monotonic() + analysis_budget,
                                time.monotonic() + max(analysis_deadline_epoch - time.time(), 0))
        stop_reason = None

        def requested_stop():
            nonlocal stop_reason
            if stop_reason is not None:
                return stop_reason
            if signal_received is not None:
                stop_reason = f'signal-{signal_received}'
            elif time.monotonic() >= analysis_deadline:
                stop_reason = 'analysis-deadline'
            elif sampler.poll() is not None:
                stop_reason = 'observer-failure'
                collection_errors.append(f'resource sampler exited during analysis: {sampler.returncode}')
            if stop_reason is not None:
                outcome.update({'phase': 'owner-step-stopping', 'result': stop_reason,
                                'stop_started_utc': utc_now(), 'signal': signal_received})
                persist_status(outcome)
            return stop_reason

        trace_args = SimpleNamespace(
            owner_tmp=str(owner_tmp), destination=str(retained), cwd=str(source_root),
            stdout=str(root / 'lint-stdout.txt'), stderr=str(root / 'lint-stderr.txt'),
            ready=str(root / 'capture-ready.txt'), cleanup_deadline=cleanup_deadline_epoch,
            owner_exit=str(root / 'owner-step.exit'), command=command,
            owner_wait=str(root / 'owner-step.wait.json'),
            owner_script_path=str(source_root / 'bin/fm-lint.sh'),
            exec_deadline_epoch=job_start_epoch + 60,
        )
        def record_owner_exec(record):
            outcome['owner_exec'] = record
            outcome['actual_owner_exec_epoch'] = record['epoch']
            outcome['actual_owner_exec_utc'] = record['utc']
            persist_status(outcome)
            (root / 'analysis-started-utc.txt').write_text(record['utc'] + '\n')
            (root / 'analysis-started-epoch.txt').write_text(f"{record['epoch']:.6f}\n")

        trace_args.owner_exec_callback = record_owner_exec
        (root / 'analysis-started-utc.txt').write_text(utc_now() + '\n')
        (root / 'analysis-started-epoch.txt').write_text(f'{time.time():.3f}\n')
        trace_status, summary = run_capture(trace_args, requested_stop)
        advance_lifecycle('RETIRE', 'controller-reduced-waits-and-recorded-finalization-state')
        owner_status = (summary.get('owner_wait') or {}).get('status') if summary else None
        owner_status = int(owner_status) if owner_status is not None else None
        # Persist the original owner status immediately after it becomes available.
        (root / 'owner-step.exit').write_text(
            'unavailable\n' if owner_status is None else f'{owner_status}\n'
        )
        outcome['owner_step_wait_status'] = owner_status
        outcome['owner_step_raw_wait_status'] = (summary or {}).get('owner_wait', {}).get('raw_wait_status')
        outcome['trace_controller_status'] = trace_status
        outcome['capture_summary'] = summary
        if summary is None or not summary.get('finalized') or summary.get('errors'):
            collection_errors.append('synchronous trace capture finalization failed or incomplete')
        if stop_reason == 'analysis-deadline':
            wrapper_status, result, phase = 124, 'analysis-deadline', 'owner-step-censored'
        elif signal_received is not None:
            wrapper_status, result, phase = 128 + signal_received, f'signal-{signal_received}', 'owner-step-interrupted'
        elif stop_reason is not None:
            wrapper_status, result, phase = 125, stop_reason, 'owner-step-failed'
        elif owner_status is None:
            wrapper_status, result, phase = 125, 'owner-status-unavailable', 'owner-step-failed'
        elif collection_errors:
            wrapper_status, result, phase = 125, 'instrumentation-or-observer-failure', 'owner-step-failed'
        else:
            wrapper_status = owner_status if owner_status >= 0 else 128 + abs(owner_status)
            result = 'complete-success' if owner_status == 0 else 'complete-nonzero-or-fatal'
            phase = 'owner-step-finished'
        outcome.update({'phase': phase, 'result': result, 'wrapper_exit': wrapper_status,
                        'analysis_finished_utc': utc_now(),
                        'analysis_elapsed_seconds': round(time.time() - float(
                            (root / 'analysis-started-epoch.txt').read_text()), 3)})
        append_phase(phase, f'owner_status={owner_status};trace_controller_status={trace_status}')
    if not (root / 'owner-step.wait.json').exists():
        (root / 'owner-step.wait.json').write_text(json.dumps({
            'status': None, 'reason': 'owner command was not started',
            'result': outcome.get('result'),
        }, indent=2) + '\n')
    if not (root / 'owner-step.exit').exists():
        (root / 'owner-step.exit').write_text('unavailable\n')
    advance_to_finalize('analysis did not reach owner execution and retirement')
    close_stream(stdout_stream)
    close_stream(stderr_stream)
    outcome['wrapper_exit'] = wrapper_status
    outcome['collection_errors_before_observer_stop'] = list(collection_errors)
    write_status(outcome)
    sampler_result = stop_sampler(time.monotonic() + max(cleanup_deadline_epoch - time.time(), 0))
    outcome['observer_results'] = [sampler_result]
    if sampler_result.get('unexpected_exit') or sampler_result.get('returncode') not in (0, -signal.SIGTERM, 128 + signal.SIGTERM):
        collection_errors.append(f'unexpected resource sampler final state: {sampler_result}')
        wrapper_status = 125
        outcome['wrapper_exit'] = wrapper_status
        outcome['result'] = 'observer-failure'
    if sampler_owner is not None:
        sampler_owner.close()
    close_stream(sampler_stream)
    outcome['cleanup_deadline_epoch'] = cleanup_deadline_epoch
    outcome['cleanup_finished_epoch'] = time.time()
    outcome['phase'] = 'final-collection'
    outcome['wrapper_exit'] = wrapper_status
    (root / 'analysis-step.exit').write_text(f'{wrapper_status}\n')
    outcome['collection_errors_before_final'] = list(collection_errors)
    write_status(outcome)
    collect_after(time.monotonic() + max(cleanup_deadline_epoch - time.time(), 0))
    if collection_errors and wrapper_status == 0:
        wrapper_status = 125
        outcome['wrapper_exit'] = wrapper_status
        outcome['result'] = 'final-evidence-collection-failed'
        (root / 'analysis-step.exit').write_text(f'{wrapper_status}\n')
    (root / 'collection-errors.json').write_text(json.dumps(collection_errors, indent=2) + '\n')
    outcome['collection_errors'] = list(collection_errors)
    outcome['phase'] = 'sealed'
    outcome['wrapper_exit'] = wrapper_status
    persist_status(outcome)
    advance_lifecycle('SEALED', f'wrapper_exit={wrapper_status};result={outcome.get("result")}')
    append_phase('complete', f'wrapper_exit={wrapper_status}')
    raise SystemExit(wrapper_status)
except SystemExit:
    raise
except BaseException as error:
    wrapper_status = 125
    if isinstance(error, ReadinessDeadlineExpired):
        exception_phase = 'analysis-not-started-readiness-deadline'
        exception_result = 'setup-window-expired-before-owner-launch'
        collection_errors.append(str(error))
    else:
        exception_phase = 'supervisor-exception'
        exception_result = 'supervisor-failure'
    outcome.update({'phase': exception_phase, 'result': exception_result,
                    'wrapper_exit': wrapper_status,
                    'exception': f'{type(error).__name__}: {error}'})
    try:
        advance_to_finalize(f'{exception_phase}: {type(error).__name__}')
    except BaseException as lifecycle_error:
        collection_errors.append(f'lifecycle finalize transition failed: {lifecycle_error}')
    try:
        persist_status(outcome)
        append_phase('supervisor-exception', outcome['exception'])
    except OSError:
        pass
    if sampler is not None:
        outcome['observer_results'] = [
            stop_sampler(time.monotonic() + max(cleanup_deadline_epoch - time.time(), 0))
        ]
    if sampler_owner is not None:
        sampler_owner.close()
    close_stream(sampler_stream)
    outcome['collection_errors'] = list(collection_errors)
    outcome['cleanup_finished_epoch'] = time.time()
    if not (root / 'owner-step.exit').exists():
        (root / 'owner-step.exit').write_text('unavailable\n')
    if not (root / 'owner-step.wait.json').exists():
        (root / 'owner-step.wait.json').write_text(json.dumps({
            'status': None, 'reason': 'owner command was not started',
            'exception': f'{type(error).__name__}: {error}',
        }, indent=2) + '\n')
    (root / 'analysis-step.exit').write_text(f'{wrapper_status}\n')
    write_status(outcome)
    collect_after(time.monotonic() + max(cleanup_deadline_epoch - time.time(), 0))
    if collection_errors and wrapper_status == 0:
        wrapper_status = 125
        outcome['wrapper_exit'] = wrapper_status
        outcome['result'] = 'final-evidence-collection-failed'
        (root / 'analysis-step.exit').write_text(f'{wrapper_status}\n')
    (root / 'collection-errors.json').write_text(json.dumps(collection_errors, indent=2) + '\n')
    outcome['collection_errors'] = list(collection_errors)
    outcome['phase'] = 'failed-unsealed'
    try:
        persist_status(outcome)
        if lifecycle_index == LIFECYCLE.index('FINALIZE'):
            advance_lifecycle('SEALED', f'wrapper_exit={wrapper_status};result={exception_result}')
    except BaseException as seal_error:
        collection_errors.append(f'failure evidence seal failed: {seal_error}')
    raise SystemExit(wrapper_status)
