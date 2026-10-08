#!/usr/bin/env python3
import ctypes
import json
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


scenario, cap_text, outer_deadline_text, root_text, source_text, recipe_text, step_text, bash_path = sys.argv[1:]
cap_seconds = int(cap_text)
outer_deadline = float(outer_deadline_text)
root = Path(root_text)
source_root = Path(source_text)
recipe_root = Path(recipe_text)
step_file = Path(step_text)
run_started = time.monotonic()
signal_received = None
collection_errors = []
analysis = None
analysis_identity = None
observers = []
observer_identities = set()
subreaper_enabled = False
owned_last_known = {}


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


def append_phase(phase, outcome):
    try:
        with (root / 'phase-journal.tsv').open('a') as stream:
            stream.write(f'{utc_now()}\t{phase}\t{outcome}\n')
    except OSError as error:
        collection_errors.append(f'phase journal {phase}: {error}')


def mark_signal(signum, _frame):
    global signal_received
    if signal_received is None:
        signal_received = signum


def proc_start_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        try:
            result = subprocess.run(
                ['ps', '-p', str(pid), '-o', 'lstart='],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            identity = ' '.join(result.stdout.split())
            return identity if result.returncode == 0 and identity else None
        except (OSError, subprocess.TimeoutExpired):
            return None


def process_table():
    table = {}
    proc_root = Path('/proc')
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                table[int(entry.name)] = {
                    'ppid': int(fields[1]),
                    'state': fields[0],
                    'identity': fields[19],
                }
            except (OSError, ValueError, IndexError):
                continue
        return table
    result = subprocess.run(
        ['ps', '-axo', 'pid=,ppid=,lstart='],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    if result.returncode != 0:
        raise RuntimeError(f'ps process inventory failed: {result.stderr.strip()}')
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 5)
        if len(fields) == 6:
            try:
                table[int(fields[0])] = {
                    'ppid': int(fields[1]),
                    'state': 'unknown',
                    'identity': ' '.join(fields[2:]),
                }
            except ValueError:
                continue
    return table


def owned_descendants():
    global owned_last_known
    if analysis is None or analysis.poll() is not None:
        root_pid = analysis.pid if analysis is not None else None
    else:
        root_pid = analysis.pid
    if root_pid is None:
        return {}, {}
    table = process_table()
    children = {}
    for pid, details in table.items():
        children.setdefault(details['ppid'], []).append(pid)
    seeds = [root_pid]
    if subreaper_enabled:
        seeds.extend(
            pid for pid, details in table.items()
            if details['ppid'] == os.getpid() and pid not in observer_identities
        )
    owned = {}
    depths = {}
    pending = [(pid, 0) for pid in seeds]
    while pending:
        pid, depth = pending.pop()
        if pid in owned or pid not in table:
            continue
        owned[pid] = table[pid]['identity']
        depths[pid] = depth
        pending.extend((child, depth + 1) for child in children.get(pid, []))
    owned_last_known.update(owned)
    return owned, depths


def signal_owned(signum):
    try:
        owned, depths = owned_descendants()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        collection_errors.append(f'process inventory before signal {signum}: {error}')
        owned = dict(owned_last_known)
        depths = {pid: 0 for pid in owned}
    sent = []
    for pid in sorted(owned, key=lambda item: depths[item], reverse=True):
        current_identity = proc_start_identity(pid)
        if current_identity != owned[pid]:
            collection_errors.append(f'owned pid {pid} identity unavailable or changed before signal {signum}')
            continue
        try:
            os.kill(pid, signum)
            sent.append(pid)
        except ProcessLookupError:
            pass
        except OSError as error:
            collection_errors.append(f'signal {signum} to owned pid {pid}: {error}')
    return sent


def signal_analysis_children(signum):
    if analysis is None:
        return []
    try:
        table = process_table()
        owned, _ = owned_descendants()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        collection_errors.append(f'process inventory before owner signal {signum}: {error}')
        return signal_owned(signum)
    children = [pid for pid, details in table.items() if details['ppid'] == analysis.pid and pid in owned]
    if not children:
        return signal_owned(signum)
    sent = []
    for pid in children:
        if proc_start_identity(pid) != owned[pid]:
            collection_errors.append(f'owner child {pid} identity unavailable or changed before signal {signum}')
            continue
        try:
            os.kill(pid, signum)
            sent.append(pid)
        except ProcessLookupError:
            pass
        except OSError as error:
            collection_errors.append(f'signal {signum} to owner child {pid}: {error}')
    return sent


def remaining_owned():
    try:
        owned, _ = owned_descendants()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        collection_errors.append(f'process inventory during reap: {error}')
        owned = dict(owned_last_known)
    remaining = {}
    for pid, identity in owned.items():
        if proc_start_identity(pid) != identity:
            continue
        details = table_details(pid)
        if details and details['state'].startswith('Z') and pid != analysis.pid:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            if proc_start_identity(pid) is None:
                continue
        remaining[pid] = identity
    return remaining


def table_details(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'state': fields[0], 'identity': fields[19]}
    except (OSError, IndexError):
        try:
            result = subprocess.run(
                ['ps', '-p', str(pid), '-o', 'stat='],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            state = result.stdout.strip()
            return {'state': state, 'identity': proc_start_identity(pid)} if state else None
        except (OSError, subprocess.TimeoutExpired):
            return None


def process_listing_command():
    if platform.system() == 'Linux':
        return ['ps', '-ww', '-eo', 'pid,ppid,pgid,etimes,time,pcpu,rss,vsz,stat,args']
    return ['ps', '-ww', '-axo', 'pid,ppid,pgid,etime,time,%cpu,rss,vsz,stat,command']


def enable_child_subreaper():
    if platform.system() != 'Linux':
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(36, 1, 0, 0, 0)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return True


def stop_owned_tree(term_grace=5.0, reap_grace=2.0):
    try:
        owned_at_term, _ = owned_descendants()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        collection_errors.append(f'process inventory before cleanup: {error}')
        owned_at_term = {}
    term_pids = signal_analysis_children(signal.SIGTERM)
    graceful_deadline = time.monotonic() + min(2.0, term_grace)
    while time.monotonic() < graceful_deadline:
        left = remaining_owned()
        if left == {}:
            break
        time.sleep(0.05)
    left = remaining_owned()
    if left:
        term_pids.extend(signal_owned(signal.SIGTERM))
    term_deadline = time.monotonic() + max(term_grace - min(2.0, term_grace), 0)
    while time.monotonic() < term_deadline:
        left = remaining_owned()
        if left == {}:
            break
        signal_owned(signal.SIGTERM)
        time.sleep(0.05)
    left = remaining_owned()
    kill_pids = []
    if left:
        kill_pids = signal_owned(signal.SIGKILL)
        reap_deadline = time.monotonic() + reap_grace
        while time.monotonic() < reap_deadline:
            left = remaining_owned()
            if left == {}:
                break
            signal_owned(signal.SIGKILL)
            time.sleep(0.05)
    if analysis is not None:
        try:
            analysis.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            collection_errors.append('analysis step process remained after bounded KILL reap')
    return {
        'owned_at_term': owned_at_term,
        'term_pids': term_pids,
        'kill_pids': kill_pids,
        'remaining': remaining_owned(),
    }


def stop_observers():
    results = []
    for name, process, _stdout in observers:
        identity = proc_start_identity(process.pid)
        if process.poll() is None:
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    os.kill(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as error:
                collection_errors.append(f'terminate observer {name}: {error}')
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if proc_start_identity(process.pid) == identity:
                        if os.getpgid(process.pid) == process.pid:
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            os.kill(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    collection_errors.append(f'kill observer {name}: {error}')
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    collection_errors.append(f'observer {name} did not reap within three seconds')
        results.append({'name': name, 'pid': process.pid, 'returncode': process.poll()})
    return results


def collect_file(command, destination, timeout=3):
    try:
        if command[0] == 'read':
            source = Path(command[1])
            content = source.read_bytes() if source.is_file() else b'unavailable on this platform\n'
            (root / destination).write_bytes(content)
            return
        result = subprocess.run(command, check=False, capture_output=True, timeout=timeout)
        (root / destination).write_bytes(result.stdout)
        if result.stderr:
            (root / (destination + '.stderr')).write_bytes(result.stderr)
        if result.returncode != 0:
            collection_errors.append(f'{destination}: exit {result.returncode}')
    except (OSError, subprocess.TimeoutExpired) as error:
        collection_errors.append(f'{destination}: {type(error).__name__}: {error}')


def save_proc_snapshot(source, destination):
    source_path = Path(source)
    try:
        data = source_path.read_bytes() if source_path.is_file() else b'unavailable on this platform\n'
        (root / destination).write_bytes(data)
    except OSError as error:
        (root / destination).write_text(f'unavailable: {type(error).__name__}: {error}\n')


def collect_after():
    for source, target in (
        ('/proc/meminfo', 'meminfo-after.txt'),
        ('/proc/swaps', 'swaps-after.txt'),
        ('/proc/vmstat', 'vmstat-after.txt'),
        ('/proc/self/cgroup', 'cgroup-after.txt'),
    ):
        collect_file(['read', source], target)
    collect_file(process_listing_command(), 'processes-after.txt')
    collect_file([sys.executable, str(recipe_root / 'cgroup-snapshot.py')], 'cgroup-metrics-after.txt')
    try:
        (root / 'collection-errors.json').write_text(json.dumps(collection_errors, indent=2) + '\n')
    except OSError:
        pass


def start_observer(name, args, stdout_path):
    stream = (root / stdout_path).open('wb')
    process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    observers.append((name, process, stream))
    observer_identities.add(process.pid)
    if os.getpgid(process.pid) != process.pid:
        raise RuntimeError(f'observer {name} did not receive an owned process group')
    return process


def write_wrapper_exit(status):
    try:
        (root / 'analysis-step.exit').write_text(f'{status}\n')
    except OSError as error:
        collection_errors.append(f'write wrapper exit: {error}')


def write_owner_exit(status):
    try:
        value = 'unavailable' if status is None else str(status)
        (root / 'owner-step.exit').write_text(value + '\n')
    except OSError as error:
        collection_errors.append(f'write owner exit: {error}')


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
    'outer_deadline_epoch': outer_deadline,
    'analysis_pid': None,
    'observer_results': [],
    'cleanup': None,
    'limitations': [
        'SIGKILL or runner destruction can prevent finalization and artifact upload.',
        'Uninterruptible kernel tasks may remain after bounded KILL escalation.',
    ],
}
persist_status(outcome)
append_phase('supervisor-start', 'in-progress')

try:
    subreaper_enabled = enable_child_subreaper()
    outcome['child_subreaper_enabled'] = subreaper_enabled
    remaining_seconds = int(outer_deadline - time.time())
    analysis_budget = min(cap_seconds, remaining_seconds)
    outcome['analysis_budget_seconds'] = max(analysis_budget, 0)
    (root / 'analysis-budget.txt').write_text(
        f'scenario={scenario}\nanalysis_limit_seconds={cap_seconds}\n'
        f'analysis_budget_seconds={max(analysis_budget, 0)}\n'
        f'outer_deadline_epoch={outer_deadline}\n'
    )
    if analysis_budget <= 0:
        outcome.update({'phase': 'analysis-not-started', 'result': 'outer-deadline-reserve'})
        wrapper_status = 125
        persist_status(outcome)
        append_phase('analysis-not-started', 'outer-deadline-reserve')
    else:
        owner_tmp = root / 'owner-tmp'
        owner_tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        retained = root / 'owner-output-retained'
        retained.mkdir(mode=0o700, parents=True, exist_ok=True)
        cgroup_before = subprocess.run(
            [sys.executable, str(recipe_root / 'cgroup-snapshot.py')],
            check=False,
            capture_output=True,
            timeout=3,
        )
        (root / 'cgroup-metrics-before.txt').write_bytes(cgroup_before.stdout)
        if cgroup_before.stderr or cgroup_before.returncode != 0:
            collection_errors.append(f'cgroup before: exit {cgroup_before.returncode}')
        save_proc_snapshot('/proc/meminfo', 'meminfo-before.txt')
        save_proc_snapshot('/proc/swaps', 'swaps-before.txt')
        save_proc_snapshot('/proc/vmstat', 'vmstat-before.txt')
        save_proc_snapshot('/proc/self/cgroup', 'cgroup-before.txt')
        processes_before = subprocess.run(
            process_listing_command(),
            check=False,
            capture_output=True,
            timeout=3,
        )
        (root / 'processes-before.txt').write_bytes(processes_before.stdout)
        if processes_before.returncode != 0:
            collection_errors.append(f'processes before: exit {processes_before.returncode}')

        capture = start_observer(
            'owner-buffer-capture',
            [sys.executable, str(recipe_root / 'capture-owner-buffers.py'), str(owner_tmp), str(retained)],
            'capture-reader.stdout.txt',
        )
        capture_ready = retained / 'capture-ready.txt'
        ready_deadline = time.monotonic() + 3
        while not capture_ready.exists() and capture.poll() is None and time.monotonic() < ready_deadline:
            time.sleep(0.025)
        sampler = start_observer(
            'resource-sampler',
            [bash_path, str(recipe_root / 'sample.sh'), str(root), '10', str(recipe_root)],
            'resource-sampler.stdout.txt',
        )
        outcome.update({
            'phase': 'observers-started',
            'owner_tmp': str(owner_tmp),
            'retained_output': str(retained),
            'capture_pid': capture.pid,
            'capture_pgid': os.getpgid(capture.pid),
            'sampler_pid': sampler.pid,
            'sampler_pgid': os.getpgid(sampler.pid),
        })
        persist_status(outcome)
        append_phase('observers-started', 'in-progress')
        if not capture_ready.exists():
            collection_errors.append('owner-buffer capture readiness marker missing')
        time.sleep(0.1)
        if not capture_ready.exists() or capture.poll() is not None or sampler.poll() is not None:
            outcome.update({'phase': 'observer-startup-failure', 'result': 'observer-failed-before-analysis'})
            wrapper_status = 125
            persist_status(outcome)
            append_phase('observer-startup-failure', 'failed')
        elif signal_received is not None:
            outcome.update({'phase': 'supervisor-signal-before-analysis', 'signal': signal_received})
            wrapper_status = 128 + signal_received
            persist_status(outcome)
            append_phase('supervisor-signal-before-analysis', 'interrupted')
        else:
            environment = os.environ.copy()
            environment.pop('FM_LINT_TELEMETRY', None)
            if scenario == 'serial':
                environment['FM_LINT_JOBS'] = '1'
            else:
                environment.pop('FM_LINT_JOBS', None)
            environment.update({
                'CI': 'true',
                'GITHUB_ACTIONS': 'true',
                'LC_ALL': 'C',
                'TMPDIR': str(owner_tmp),
            })
            stdout_stream = (root / 'lint-stdout.txt').open('wb')
            stderr_stream = (root / 'lint-stderr.txt').open('wb')
            command = [bash_path, '-e', str(step_file)]
            (root / 'lint-command.txt').write_text(
                json.dumps({'argv': command, 'cwd': str(source_root), 'environment': {
                    'CI': 'true',
                    'GITHUB_ACTIONS': 'true',
                    'LC_ALL': 'C',
                    'FM_LINT_JOBS': environment.get('FM_LINT_JOBS', 'unset'),
                    'FM_LINT_TELEMETRY': 'unset',
                    'TMPDIR': str(owner_tmp),
                    'PATH': environment.get('PATH'),
                }}, indent=2) + '\n'
            )
            outcome.update({
                'phase': 'owner-step-running',
                'owner_command': command,
                'owner_cwd': str(source_root),
                'analysis_started_utc': utc_now(),
            })
            persist_status(outcome)
            append_phase('owner-step-running', 'in-progress')
            analysis_started = time.monotonic()
            deadline = min(analysis_started + analysis_budget, run_started + max(outer_deadline - time.time(), 0))
            analysis = subprocess.Popen(
                command,
                cwd=source_root,
                env=environment,
                stdout=stdout_stream,
                stderr=stderr_stream,
                start_new_session=True,
            )
            analysis_identity = proc_start_identity(analysis.pid)
            outcome['analysis_pid'] = analysis.pid
            outcome['analysis_pgid'] = os.getpgid(analysis.pid)
            outcome['analysis_start_identity'] = analysis_identity
            persist_status(outcome)
            append_phase('owner-step-running', f'pid={analysis.pid}')
            (root / 'analysis-started-utc.txt').write_text(utc_now() + '\n')
            (root / 'analysis-started-epoch.txt').write_text(f'{time.time():.3f}\n')
            stop_reason = None
            while analysis.poll() is None:
                if signal_received is not None:
                    stop_reason = f'signal-{signal_received}'
                    break
                if time.monotonic() >= deadline:
                    stop_reason = 'analysis-deadline'
                    break
                if capture.poll() is not None or sampler.poll() is not None:
                    stop_reason = 'observer-failure'
                    break
                time.sleep(0.05)
            if stop_reason is None:
                owner_status = analysis.wait()
                orphans = remaining_owned()
                if orphans:
                    outcome['cleanup'] = stop_owned_tree()
                    collection_errors.append('owned descendants remained after owner step exited')
                    result = 'owner-exited-with-owned-descendants'
                    wrapper_status = 125
                    phase = 'owner-step-finished-with-cleanup'
                else:
                    result = 'complete-success' if owner_status == 0 else 'complete-nonzero-or-fatal'
                    wrapper_status = owner_status
                    phase = 'owner-step-finished'
            else:
                result = stop_reason
                phase = 'owner-step-stopping'
                wrapper_status = 124 if stop_reason == 'analysis-deadline' else (
                    128 + signal_received if signal_received is not None else 125
                )
                outcome.update({
                    'phase': phase,
                    'result': result,
                    'stop_started_utc': utc_now(),
                    'wrapper_exit': wrapper_status,
                    'signal': signal_received,
                })
                persist_status(outcome)
                append_phase(phase, result)
                outcome['cleanup'] = stop_owned_tree()
                if analysis.poll() is None:
                    collection_errors.append('analysis wait status unavailable after bounded cleanup')
                    owner_status = None
                else:
                    owner_status = analysis.wait()
            outcome.update({
                'phase': phase,
                'result': result,
                'owner_step_wait_status': owner_status,
                'wrapper_exit': wrapper_status,
                'analysis_finished_utc': utc_now(),
                'analysis_elapsed_seconds': round(time.monotonic() - analysis_started, 3),
            })
            write_owner_exit(owner_status)
            persist_status(outcome)
            append_phase(phase, f'owner_status={owner_status}')
            stdout_stream.close()
            stderr_stream.close()

except BaseException as error:
    wrapper_status = 125
    outcome.update({
        'phase': 'supervisor-exception',
        'result': 'supervisor-failure',
        'wrapper_exit': wrapper_status,
        'exception': f'{type(error).__name__}: {error}',
    })
    try:
        persist_status(outcome)
    except OSError:
        pass
    append_phase('supervisor-exception', outcome['exception'])
    if analysis is not None and analysis.poll() is None:
        outcome['cleanup'] = stop_owned_tree()
    write_status(outcome)
else:
    if 'wrapper_status' not in locals():
        wrapper_status = 125

outcome['wrapper_exit'] = wrapper_status
outcome['collection_errors_before_final'] = list(collection_errors)
write_status(outcome)
append_phase('observers-stopping', 'in-progress')
observer_results = stop_observers()
outcome['observer_results'] = observer_results
for name, process, stream in observers:
    try:
        stream.close()
    except OSError as error:
        collection_errors.append(f'close observer log {name}: {error}')
    expected_returncodes = {'resource-sampler': {-signal.SIGTERM, 128 + signal.SIGTERM}}
    if process.returncode not in (0, None) and process.returncode not in expected_returncodes.get(name, set()):
        collection_errors.append(f'observer {name} exit {process.returncode}')
    if process.returncode is None:
        wrapper_status = 125
        outcome['wrapper_exit'] = wrapper_status
if any('owner-buffer' in error or 'capture ' in error for error in collection_errors):
    wrapper_status = 125
    outcome['wrapper_exit'] = wrapper_status
write_wrapper_exit(wrapper_status)
write_owner_exit(outcome.get('owner_step_wait_status'))
outcome['phase'] = 'final-collection'
outcome['wrapper_exit'] = wrapper_status
outcome['collection_errors_before_final'] = list(collection_errors)
write_status(outcome)
collect_after()
outcome['collection_errors'] = list(collection_errors)
outcome['phase'] = 'complete'
outcome['wrapper_exit'] = wrapper_status
write_status(outcome)
append_phase('complete', f'wrapper_exit={wrapper_status}')
sys.exit(wrapper_status)
