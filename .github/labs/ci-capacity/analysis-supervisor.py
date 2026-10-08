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
from wait_owner import WaitOwner


scenario, cap_text, analysis_deadline_text, cleanup_deadline_text, root_text, source_text, recipe_text, step_text, bash_path = sys.argv[1:]
cap_seconds = int(cap_text)
analysis_deadline_monotonic = float(analysis_deadline_text)
cleanup_deadline_monotonic = float(cleanup_deadline_text)
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
sampler_result_state = {'result': None}
wait_owner = WaitOwner(root_text + '/wait-ledger.jsonl')
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
        if time.monotonic() >= cleanup_deadline_monotonic:
            raise TimeoutError('outcome write started after the common cleanup deadline')
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        with temporary.open('x') as stream:
            stream.write(json.dumps(payload, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if time.monotonic() >= cleanup_deadline_monotonic:
            raise TimeoutError('outcome replace completed at or after the common cleanup deadline')
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        message = f'write analysis outcome: {type(error).__name__}: {error}'
        collection_errors.append(message)
        if time.monotonic() < cleanup_deadline_monotonic:
            try:
                with (root / 'supervisor-errors.log').open('a') as stream:
                    stream.write(message + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                pass
        if required:
            return False
    return True


def persist_status(values):
    if not write_status(values, required=True):
        raise OSError('could not persist the durable analysis outcome')


def write_evidence(path, payload, deadline=None):
    path = Path(path)
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f'evidence write started after its deadline: {path}')
    temporary = path.with_name(path.name + '.partial')
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(payload if isinstance(payload, bytes) else payload.encode())
        while view:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f'evidence write crossed its deadline: {path}')
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError('short evidence write')
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
    os.replace(temporary, path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f'evidence replace or directory sync completed after deadline: {path}')


def append_phase(phase, result):
    try:
        if time.monotonic() >= cleanup_deadline_monotonic:
            raise TimeoutError(f'phase journal {phase} started after common deadline')
        with (root / 'phase-journal.tsv').open('a') as stream:
            stream.write(f'{utc_now()}\t{phase}\t{result}\n')
            stream.flush()
            os.fsync(stream.fileno())
        if time.monotonic() >= cleanup_deadline_monotonic:
            raise TimeoutError(f'phase journal {phase} completed after common deadline')
    except OSError as error:
        collection_errors.append(f'phase journal {phase}: {error}')


def advance_lifecycle(phase, result):
    global lifecycle_index
    if time.monotonic() >= cleanup_deadline_monotonic:
        raise TimeoutError(f'lifecycle transition {phase} reached the common deadline')
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
    if time.monotonic() >= cleanup_deadline_monotonic:
        raise TimeoutError(f'lifecycle transition {phase} completed at or after the common deadline')


def mark_signal(signum, _frame):
    global signal_received
    if signal_received is None:
        signal_received = signum


def process_listing_command():
    if platform.system() == 'Linux':
        return ['ps', '-ww', '-eo', 'pid,ppid,pgid,etimes,time,pcpu,rss,vsz,stat,args']
    return ['ps', '-ww', '-axo', 'pid,ppid,pgid,etime,time,%cpu,rss,vsz,stat,command']


def stop_sampler(deadline):
    if sampler is None:
        return {'started': False}
    known = wait_owner.already_reaped(sampler.pid)
    if known is None:
        try:
            term_sent = (sampler_owner.send(signal.SIGTERM) if sampler_owner is not None
                         else bool(wait_owner.signal(sampler.pid, signal.SIGTERM).get('delivered', True)))
        except OSError as error:
            collection_errors.append(f'resource sampler signal TERM: {error}')
            term_sent = False
        except RuntimeError as error:
            collection_errors.append(f'resource sampler signal TERM refused: {error}')
            term_sent = False
    else:
        term_sent = False
    term_deadline = min(time.monotonic() + 2, deadline)
    while known is None and time.monotonic() < term_deadline:
        try:
            event = wait_owner.wait_any()
        except (OSError, RuntimeError) as error:
            collection_errors.append(f'resource sampler terminal wait: {error}')
            break
        if event and event[0] == sampler.pid:
            sampler_owner.record_wait(event[1])
            known = event[3]
            break
        if event:
            collection_errors.append(f'unexpected child wait during sampler retirement: {event[3]}')
        time.sleep(min(0.01, max(deadline - time.monotonic(), 0)))
    kill_sent = False
    if known is None and time.monotonic() < deadline:
        try:
            kill_sent = (sampler_owner.send(signal.SIGKILL) if sampler_owner is not None
                         else bool(wait_owner.signal(sampler.pid, signal.SIGKILL).get('delivered', True)))
        except OSError as error:
            collection_errors.append(f'resource sampler signal KILL: {error}')
        except RuntimeError as error:
            collection_errors.append(f'resource sampler signal KILL refused: {error}')
        while known is None and time.monotonic() < deadline:
            try:
                event = wait_owner.wait_any()
            except (OSError, RuntimeError) as error:
                collection_errors.append(f'resource sampler final wait: {error}')
                break
            if event and event[0] == sampler.pid:
                sampler_owner.record_wait(event[1])
                known = event[3]
                break
            if event:
                collection_errors.append(f'unexpected child wait during sampler finalization: {event[3]}')
            time.sleep(min(0.01, max(deadline - time.monotonic(), 0)))
    if known is None:
        collection_errors.append('resource sampler wait unavailable by the common cleanup deadline')
    sampler_code = None
    if known is not None:
        sampler_code = (known['value'] if known['kind'] == 'exit'
                        else 128 + known['value'] if known['kind'] == 'signal' else None)
    if time.monotonic() > deadline:
        collection_errors.append('resource sampler finalization completed after common deadline')
    return {'started': True, 'term_sent': term_sent, 'kill_sent': kill_sent,
            'returncode': sampler_code, 'raw_wait_status': (known or {}).get('raw_wait_status'),
            'role': 'resource-sampler', 'requested_stop': True,
            'unexpected_exit': known is not None and not term_sent}


def ensure_retired_and_finalizing(reason):
    if lifecycle_index < LIFECYCLE.index('RUN'):
        advance_lifecycle('RUN', f'skipped: {reason}')
    if sampler_result_state['result'] is None and sampler is not None:
        sampler_result_state['result'] = stop_sampler(cleanup_deadline_monotonic)
    if lifecycle_index < LIFECYCLE.index('RETIRE'):
        advance_lifecycle('RETIRE', f'owned waits retired: {reason}')
    if lifecycle_index < LIFECYCLE.index('FINALIZE'):
        advance_lifecycle('FINALIZE', f'evidence finalization: {reason}')


def collect_file(command, destination, deadline):
    try:
        if time.monotonic() >= deadline:
            collection_errors.append(f'{destination}: common cleanup deadline expired before collection')
            return
        if command[0] == 'read':
            source = Path(command[1])
            content = source.read_bytes() if source.is_file() else b'unavailable on this platform\n'
            write_evidence(root / destination, content, deadline)
            return
        remaining = max(deadline - time.monotonic(), 0)
        if remaining <= 0:
            collection_errors.append(f'{destination}: common cleanup deadline expired before collection')
            return
        result = subprocess.run(command, check=False, capture_output=True,
                                timeout=min(2, remaining))
        write_evidence(root / destination, result.stdout, deadline)
        if result.stderr:
            write_evidence(root / (destination + '.stderr'), result.stderr, deadline)
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


def start_sampler(bash_path, readiness_deadline):
    global sampler, sampler_identity, sampler_owner, sampler_stream
    sampler_stream = (root / 'resource-sampler.stdout.txt').open('wb')
    sampler = SimpleNamespace(pid=os.fork())
    if sampler.pid == 0:
        try:
            os.dup2(sampler_stream.fileno(), 1)
            os.dup2(sampler_stream.fileno(), 2)
            os.close(sampler_stream.fileno())
            command = [bash_path, str(recipe_root / 'sample.sh'), str(root), '10', str(recipe_root)]
            os.execve(command[0], command, os.environ.copy())
        except BaseException:
            os._exit(127)
    try:
        sampler_owner = OwnedChild.bind_pid(sampler.pid)
    except (OSError, RuntimeError) as error:
        wait_owner.register(sampler.pid, 'resource-sampler', None)
        collection_errors.append(f'bind resource sampler identity: {error}')
        raise
    sampler_identity = sampler_owner.identity
    wait_owner.register(sampler.pid, 'resource-sampler', sampler_identity,
                        sampler_owner.pidfd, sampler.pid)
    ready = root / 'resource-sampler-ready.txt'
    failed = root / 'resource-sampler-failed.txt'
    sampler_start_deadline = min(time.monotonic() + 5, readiness_deadline)
    while not ready.exists() and not failed.exists() and not sampler_owner.exited():
        if time.monotonic() >= sampler_start_deadline:
            raise TimeoutError('resource sampler did not publish its bounded startup result')
        time.sleep(min(0.01, max(sampler_start_deadline - time.monotonic(), 0)))
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
    'analysis_deadline_monotonic': analysis_deadline_monotonic,
    'required_analysis_start_deadline_epoch': None,
    'cleanup_deadline_monotonic': cleanup_deadline_monotonic,
    'analysis_pid': None,
    'observer_results': [],
    'cleanup': None,
    'analysis_fact': {'state': 'not-started'},
    'instrumentation_fact': {'state': 'not-started'},
    'admission_fact': {'state': 'pending'},
    'delivery_fact': {'state': 'pending'},
    'limitations': [
        'SIGKILL or runner destruction can prevent finalization and artifact upload.',
        'Uninterruptible kernel tasks may remain after bounded KILL escalation.',
        'PTRACE_O_EXITKILL kills tracees if the sole controller is destroyed before evidence finalization.',
    ],
}

try:
    persist_status(outcome)
    phase_path = root / 'lifecycle-journal.tsv'
    if not phase_path.exists():
        raise RuntimeError('durable PREPARE lifecycle record is missing')
    existing_phases = [line.split('\t')[1] for line in phase_path.read_text().splitlines()
                       if len(line.split('\t')) >= 3]
    if existing_phases != list(LIFECYCLE[:3]):
        raise RuntimeError(f'pre-analysis lifecycle qualification incomplete: {existing_phases}')
    lifecycle_index = LIFECYCLE.index('ARM')
    append_phase('supervisor-start', 'in-progress')
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('the owning analysis path requires Linux x86_64')
    pidfd_probe = os.pidfd_open(os.getpid(), 0)
    signal.pidfd_send_signal(pidfd_probe, 0)
    os.close(pidfd_probe)
    write_evidence(root / 'pidfd-preflight.txt',
                   f'pidfd_open=pass\npidfd_send_signal_zero=pass\npid={os.getpid()}\n',
                   cleanup_deadline_monotonic)
    job_start_monotonic_ns = int((root / 'job-start-monotonic-ns.txt').read_text())
    job_start_monotonic = job_start_monotonic_ns / 1_000_000_000
    remaining = max(0.0, analysis_deadline_monotonic - time.monotonic())
    analysis_budget = min(float(cap_seconds), remaining)
    outcome['analysis_budget_seconds'] = cap_seconds
    outcome['job_start_monotonic_ns'] = job_start_monotonic_ns
    outcome['analysis_deadline_monotonic'] = analysis_deadline_monotonic
    outcome['cleanup_deadline_monotonic'] = cleanup_deadline_monotonic
    write_evidence(root / 'analysis-budget.txt',
        f'scenario={scenario}\nanalysis_limit_seconds={cap_seconds}\n'
        f'analysis_budget_seconds={analysis_budget}\n'
        f'job_start_monotonic_ns={job_start_monotonic_ns}\n'
        f'analysis_deadline_monotonic={analysis_deadline_monotonic}\n'
        f'cleanup_deadline_monotonic={cleanup_deadline_monotonic}\n',
        cleanup_deadline_monotonic)
    owner_tmp = root / 'owner-tmp'
    retained = root / 'owner-output-retained'
    owner_tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    retained.mkdir(mode=0o700, parents=True, exist_ok=True)
    cgroup_before = subprocess.run(
        [sys.executable, str(recipe_root / 'cgroup-snapshot.py')],
        check=False, capture_output=True, timeout=2,
    )
    write_evidence(root / 'cgroup-metrics-before.txt', cgroup_before.stdout,
                   cleanup_deadline_monotonic)
    if cgroup_before.stderr or cgroup_before.returncode != 0:
        collection_errors.append(f'cgroup before: exit {cgroup_before.returncode}')
    for source, target in (
        ('/proc/meminfo', 'meminfo-before.txt'),
        ('/proc/swaps', 'swaps-before.txt'),
        ('/proc/vmstat', 'vmstat-before.txt'),
        ('/proc/self/cgroup', 'cgroup-before.txt'),
    ):
        write_evidence(root / target, Path(source).read_bytes(), cleanup_deadline_monotonic)
    before = subprocess.run(process_listing_command(), check=False,
                            capture_output=True, timeout=2)
    write_evidence(root / 'processes-before.txt', before.stdout, cleanup_deadline_monotonic)
    if before.returncode != 0:
        collection_errors.append(f'processes before: exit {before.returncode}')
    if lifecycle_index != LIFECYCLE.index('ARM'):
        raise RuntimeError('complete fixture qualification did not arm this analysis job')

    job_start_epoch = float((root / 'job-start-epoch.txt').read_text())
    sampler_stream = start_sampler(bash_path, job_start_monotonic + 60)
    readiness = record_readiness(root, 'analysis-supervisor-ready', job_start_epoch + 60)
    outcome['required_analysis_start_deadline_epoch'] = readiness['required_analysis_start_deadline_epoch']
    outcome['analysis_readiness'] = readiness
    outcome.update({'phase': 'resource-observer-started', 'owner_tmp': str(owner_tmp),
                    'retained_output': str(retained), 'sampler_pid': sampler.pid,
                    'sampler_identity': sampler_identity})
    persist_status(outcome)
    append_phase('resource-observer-started', 'in-progress')
    if sampler_owner.exited() or (root / 'resource-sampler-failed.txt').exists():
        collection_errors.append('resource sampler exited before analysis; exact wait remains in shared ledger')
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
        effective_environment = {key: environment[key] for key in (
            'CI', 'GITHUB_ACTIONS', 'LC_ALL', 'PATH', 'FM_LINT_JOBS',
            'FM_LINT_TELEMETRY', 'TMPDIR'
        ) if key in environment}
        command = [bash_path, '-e', str(step_file)]
        write_evidence(root / 'lint-command.txt', json.dumps({
            'argv': command, 'cwd': str(source_root),
            'environment': effective_environment,
        }, indent=2) + '\n', cleanup_deadline_monotonic)
        launch_readiness = record_readiness(
            root, 'analysis-owner-launch', job_start_epoch + 60
        )
        outcome['analysis_owner_launch_readiness'] = launch_readiness
        persist_status(outcome)
        if not launch_readiness['admitted']:
            raise ReadinessDeadlineExpired(
                'setup exceeded the one-minute deadline before owner launch; experiment pair is invalid'
            )
        if lifecycle_index != LIFECYCLE.index('ARM'):
            raise RuntimeError('analysis launch refused before complete fixture ARM phase')
        outcome.update({'phase': 'owner-step-running', 'owner_command': command,
                        'owner_cwd': str(source_root), 'analysis_started_utc': utc_now(),
                        'controller_pid': os.getpid(),
                        'trace_controller': 'synchronous in-process ptrace reducer'})
        persist_status(outcome)
        append_phase('owner-step-running', 'in-progress')
        analysis_deadline_state = {'monotonic': None}
        stop_state = {'reason': None}

        def requested_stop():
            if stop_state['reason'] is not None:
                return stop_state['reason']
            if signal_received is not None:
                stop_state['reason'] = f'signal-{signal_received}'
            elif (analysis_deadline_state['monotonic'] is not None and
                  time.monotonic() >= analysis_deadline_state['monotonic']):
                stop_state['reason'] = 'analysis-deadline'
            elif ((root / 'resource-sampler-failed.txt').exists() or
                  wait_owner.already_reaped(sampler.pid) is not None or sampler_owner.exited()):
                stop_state['reason'] = 'observer-failure'
                collection_errors.append('resource sampler exited during analysis; raw wait retained by shared ledger')
            if stop_state['reason'] is not None:
                outcome.update({'phase': 'owner-step-stopping', 'result': stop_state['reason'],
                                'stop_started_utc': utc_now(), 'signal': signal_received})
                persist_status(outcome)
            return stop_state['reason']

        trace_args = SimpleNamespace(
            owner_tmp=str(owner_tmp), destination=str(retained), cwd=str(source_root),
            stdout=str(root / 'lint-stdout.txt'), stderr=str(root / 'lint-stderr.txt'),
            ready=str(root / 'capture-ready.txt'),
            owner_exit=str(root / 'owner-step.exit'), command=command,
            owner_wait=str(root / 'owner-step.wait.json'),
            owner_script_path=str(source_root / 'bin/fm-lint.sh'),
            exec_deadline_monotonic=job_start_monotonic + 60,
            environment=environment,
            wait_owner=wait_owner,
            sampler_pid=sampler.pid,
            cleanup_deadline_monotonic=cleanup_deadline_monotonic,
            step_exit=root / 'step-shell.exit',
            step_wait=root / 'step-shell.wait.json',
            lifecycle_callback=None,
            expected_signal_for_fixtures=(os.environ.get('LAB_EXPECTED_SIGNAL_FIXTURE') == '1'),
        )
        def trace_lifecycle(phase_name, description):
            if phase_name == 'RETIRE' and sampler_result_state['result'] is None:
                sampler_result_state['result'] = stop_sampler(cleanup_deadline_monotonic)
                if (sampler_result_state['result'].get('returncode') not in
                        (0, -signal.SIGTERM, 128 + signal.SIGTERM)):
                    collection_errors.append(
                        f'resource sampler terminal result invalid: {sampler_result_state["result"]}'
                    )
            advance_lifecycle(phase_name, description)

        trace_args.lifecycle_callback = trace_lifecycle
        def record_owner_exec(record):
            admission_deadline = min(analysis_deadline_monotonic,
                                     record['monotonic'] + cap_seconds)
            if admission_deadline - record['monotonic'] < cap_seconds:
                raise ReadinessDeadlineExpired(
                    'full owner cap cannot fit inside the absolute analysis cutoff'
                )
            advance_lifecycle('RUN', 'actual executable-owner exec admitted')
            analysis_deadline_state['monotonic'] = admission_deadline
            outcome['owner_exec'] = record
            outcome['actual_owner_exec_epoch'] = record['epoch']
            outcome['actual_owner_exec_utc'] = record['utc']
            outcome['actual_owner_exec_monotonic'] = record['monotonic']
            outcome['analysis_budget_seconds'] = cap_seconds
            outcome['analysis_deadline_monotonic'] = admission_deadline
            outcome['analysis_fact'] = {'state': 'running', 'owner': record,
                                        'deadline_monotonic': admission_deadline,
                                        'cap_seconds': cap_seconds}
            outcome['admission_fact'] = {'state': 'admitted', 'owner_exec': record}
            persist_status(outcome)
            write_evidence(root / 'analysis-started-utc.txt', record['utc'] + '\n',
                           cleanup_deadline_monotonic)
            write_evidence(root / 'analysis-started-epoch.txt',
                           f"{record['epoch']:.6f}\n", cleanup_deadline_monotonic)
            write_evidence(root / 'analysis-started-monotonic.txt',
                           f"{record['monotonic']:.9f}\n", cleanup_deadline_monotonic)

        trace_args.owner_exec_callback = record_owner_exec
        trace_status, summary = run_capture(trace_args, requested_stop)
        owner_status = ((summary.get('step_wait') or {}).get('status')
                        if summary else None)
        owner_status = int(owner_status) if owner_status is not None else None
        # Persist the original owner status immediately after it becomes available.
        write_evidence(root / 'owner-step.exit',
                       'unavailable\n' if owner_status is None else f'{owner_status}\n',
                       cleanup_deadline_monotonic)
        outcome['owner_step_wait_status'] = owner_status
        outcome['owner_step_raw_wait_status'] = (summary or {}).get('step_wait', {}).get('raw_wait_status')
        outcome['executable_owner_wait'] = (summary or {}).get('owner_wait')
        outcome['trace_controller_status'] = trace_status
        outcome['capture_summary'] = summary
        outcome['instrumentation_fact'] = {
            'state': 'complete' if summary and summary.get('finalized') else 'failed',
            'errors': (summary or {}).get('errors', []),
        }
        if summary and summary.get('owner_exec'):
            actual_suffixes = {Path(item['path']).suffix for item in summary.get('unlink_events', [])
                               if item.get('opened_before_resume')}
            if not {'.out', '.rc'}.issubset(actual_suffixes):
                message = ('immutable owner did not expose both .out and .rc unlink events; '
                           f'observed={sorted(actual_suffixes)}')
                collection_errors.append(message)
                outcome['instrumentation_fact'] = {
                    'state': 'failed', 'errors': [message],
                    'actual_owner_unlink_suffixes': sorted(actual_suffixes),
                }
        if summary and summary.get('owner_exec'):
            outcome['analysis_fact'] = {
                'state': ('censored' if stop_state['reason'] == 'analysis-deadline'
                          else 'interrupted' if stop_state['reason'] else 'complete'),
                'owner_exec': summary['owner_exec'],
                'terminal_wait': summary.get('owner_wait'),
                'exit_status': (summary.get('owner_wait') or {}).get('status'),
            }
        else:
            outcome['analysis_fact'] = {
                'state': 'not-started',
                'reason': stop_state['reason'] or 'owner exec was not observed',
            }
        if summary is None or not summary.get('finalized') or summary.get('errors'):
            collection_errors.append('synchronous trace capture finalization failed or incomplete')
        stop_reason = stop_state['reason']
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
        started_monotonic_path = root / 'analysis-started-monotonic.txt'
        elapsed = (round(time.monotonic() - float(started_monotonic_path.read_text()), 3)
                   if started_monotonic_path.exists() else None)
        outcome.update({'phase': phase, 'result': result, 'wrapper_exit': wrapper_status,
                        'analysis_finished_utc': utc_now(),
                        'analysis_elapsed_seconds': elapsed})
        outcome['admission_fact'] = {
            'state': 'admitted' if summary and summary.get('owner_exec') else 'refused-or-not-started',
            'reason': (None if summary and summary.get('owner_exec')
                       else outcome.get('result')),
        }
        append_phase(phase, f'owner_status={owner_status};trace_controller_status={trace_status}')
    if not (root / 'owner-step.wait.json').exists():
        write_evidence(root / 'owner-step.wait.json', json.dumps({
            'status': None, 'reason': 'owner command was not started',
            'result': outcome.get('result'),
        }, indent=2) + '\n', cleanup_deadline_monotonic)
    if not (root / 'owner-step.exit').exists():
        write_evidence(root / 'owner-step.exit', 'unavailable\n', cleanup_deadline_monotonic)
    if lifecycle_index < LIFECYCLE.index('FINALIZE'):
        ensure_retired_and_finalizing('analysis did not reach owner execution and retirement')
    close_stream(stdout_stream)
    close_stream(stderr_stream)
    outcome['wrapper_exit'] = wrapper_status
    outcome['collection_errors_before_observer_stop'] = list(collection_errors)
    write_status(outcome)
    sampler_result = sampler_result_state['result'] if 'sampler_result_state' in locals() else None
    if sampler_result is None:
        sampler_result = stop_sampler(cleanup_deadline_monotonic)
    outcome['observer_results'] = [sampler_result]
    if sampler_result.get('unexpected_exit') or sampler_result.get('returncode') not in (0, -signal.SIGTERM, 128 + signal.SIGTERM):
        collection_errors.append(f'unexpected resource sampler final state: {sampler_result}')
        wrapper_status = 125
        outcome['wrapper_exit'] = wrapper_status
        outcome['result'] = 'observer-failure'
    if sampler_owner is not None:
        sampler_owner.close()
    close_stream(sampler_stream)
    outcome['cleanup_finished_epoch'] = time.time()
    outcome['phase'] = 'final-collection'
    outcome['wrapper_exit'] = wrapper_status
    write_evidence(root / 'analysis-step.exit', f'{wrapper_status}\n', cleanup_deadline_monotonic)
    outcome['collection_errors_before_final'] = list(collection_errors)
    write_status(outcome)
    collect_after(cleanup_deadline_monotonic)
    if collection_errors and wrapper_status == 0:
        wrapper_status = 125
        outcome['wrapper_exit'] = wrapper_status
        outcome['result'] = 'final-evidence-collection-failed'
        write_evidence(root / 'analysis-step.exit', f'{wrapper_status}\n', cleanup_deadline_monotonic)
    write_evidence(root / 'collection-errors.json',
                   json.dumps(collection_errors, indent=2) + '\n', cleanup_deadline_monotonic)
    outcome['collection_errors'] = list(collection_errors)
    outcome['phase'] = 'sealed' if time.monotonic() < cleanup_deadline_monotonic else 'finalization-incomplete'
    outcome['wrapper_exit'] = wrapper_status
    if time.monotonic() >= cleanup_deadline_monotonic:
        wrapper_status = 125
        outcome.update({'wrapper_exit': wrapper_status, 'result': 'common-cleanup-deadline',
                        'phase': 'finalization-incomplete',
                        'finalization_fact': {'state': 'incomplete', 'deadline_met': False}})
        write_evidence(root / 'analysis-step.exit', '125\n', cleanup_deadline_monotonic)
    else:
        outcome['finalization_fact'] = {'state': 'complete', 'deadline_met': True}
    persist_status(outcome)
    if outcome['finalization_fact']['state'] == 'complete':
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
        persist_status(outcome)
        append_phase('supervisor-exception', outcome['exception'])
    except OSError:
        pass
    try:
        ensure_retired_and_finalizing(f'{exception_phase}: {type(error).__name__}')
    except BaseException as lifecycle_error:
        collection_errors.append(f'lifecycle finalize transition failed: {lifecycle_error}')
    if sampler_result_state['result'] is not None:
        outcome['observer_results'] = [sampler_result_state['result']]
    if sampler_owner is not None:
        sampler_owner.close()
    close_stream(sampler_stream)
    outcome['collection_errors'] = list(collection_errors)
    outcome['cleanup_finished_epoch'] = time.time()
    if not (root / 'owner-step.exit').exists():
        write_evidence(root / 'owner-step.exit', 'unavailable\n', cleanup_deadline_monotonic)
    if not (root / 'owner-step.wait.json').exists():
        write_evidence(root / 'owner-step.wait.json', json.dumps({
            'status': None, 'reason': 'owner command was not started',
            'exception': f'{type(error).__name__}: {error}',
        }, indent=2) + '\n', cleanup_deadline_monotonic)
    write_evidence(root / 'analysis-step.exit', f'{wrapper_status}\n', cleanup_deadline_monotonic)
    write_status(outcome)
    collect_after(cleanup_deadline_monotonic)
    if collection_errors and wrapper_status == 0:
        wrapper_status = 125
        outcome['wrapper_exit'] = wrapper_status
        outcome['result'] = 'final-evidence-collection-failed'
        write_evidence(root / 'analysis-step.exit', f'{wrapper_status}\n', cleanup_deadline_monotonic)
    write_evidence(root / 'collection-errors.json',
                   json.dumps(collection_errors, indent=2) + '\n', cleanup_deadline_monotonic)
    outcome['collection_errors'] = list(collection_errors)
    outcome['phase'] = 'failed-unsealed'
    outcome['finalization_fact'] = {
        'state': 'complete' if time.monotonic() < cleanup_deadline_monotonic else 'incomplete',
        'deadline_met': time.monotonic() < cleanup_deadline_monotonic,
    }
    try:
        persist_status(outcome)
        if (lifecycle_index == LIFECYCLE.index('FINALIZE') and
                outcome['finalization_fact']['state'] == 'complete'):
            advance_lifecycle('SEALED', f'wrapper_exit={wrapper_status};result={exception_result}')
    except BaseException as seal_error:
        collection_errors.append(f'failure evidence seal failed: {seal_error}')
    raise SystemExit(wrapper_status)
