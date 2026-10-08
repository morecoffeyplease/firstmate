#!/usr/bin/env python3
"""Run and trace one lint owner, retaining its shard buffers before unlink."""

import ctypes
import errno
import hashlib
import json
import os
import platform
import resource
import re
import signal
import stat
import sys
import time
from pathlib import Path


PTRACE_TRACEME = 0
PTRACE_CONT = 7
PTRACE_SETOPTIONS = 0x4200
PTRACE_GETEVENTMSG = 0x4201
PTRACE_GETREGS = 12
PTRACE_O_TRACEFORK = 0x00000002
PTRACE_O_TRACEVFORK = 0x00000004
PTRACE_O_TRACECLONE = 0x00000008
PTRACE_O_TRACEEXEC = 0x00000010
PTRACE_O_TRACEEXIT = 0x00000040
PTRACE_O_TRACESECCOMP = 0x00000080
PTRACE_O_EXITKILL = 0x00100000
PTRACE_EVENT_FORK = 1
PTRACE_EVENT_VFORK = 2
PTRACE_EVENT_CLONE = 3
PTRACE_EVENT_EXEC = 4
PTRACE_EVENT_EXIT = 6
PTRACE_EVENT_SECCOMP = 7
PTRACE_EVENT_STOP = 128
WAIT_ALL = 0x40000000
AT_FDCWD = -100
AUDIT_ARCH_X86_64 = 0xC000003E
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_TRACE = 0x7FF00000
SECCOMP_RET_ALLOW = 0x7FFF0000
PR_SET_SECCOMP = 22
PR_SET_NO_NEW_PRIVS = 38
PR_SET_PDEATHSIG = 1
SECCOMP_MODE_FILTER = 2
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_BENEATH = 0x08
SYS_OPENAT2 = 437
SYS_UNLINK = 87
SYS_UNLINKAT = 263
X32_SYSCALL_BIT = 0x40000000
CAPTURE_PATTERN = re.compile(r"fm-lint\.[^/]+/output/shard\.[0-9]+\.(?:out|rc)\Z")
OPTIONS = (
    PTRACE_O_TRACEFORK | PTRACE_O_TRACEVFORK | PTRACE_O_TRACECLONE |
    PTRACE_O_TRACEEXEC | PTRACE_O_TRACEEXIT | PTRACE_O_TRACESECCOMP |
    PTRACE_O_EXITKILL
)


class SockFilter(ctypes.Structure):
    _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    _fields_ = [('length', ctypes.c_ushort),
                ('filters', ctypes.POINTER(SockFilter))]


class OpenHow(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_uint64), ('mode', ctypes.c_uint64),
                ('resolve', ctypes.c_uint64)]


class UserRegs(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulong) for name in (
        'r15', 'r14', 'r13', 'r12', 'rbp', 'rbx', 'r11', 'r10', 'r9',
        'r8', 'rax', 'rcx', 'rdx', 'rsi', 'rdi', 'orig_rax', 'rip', 'cs',
        'eflags', 'rsp', 'ss', 'fs_base', 'gs_base', 'ds', 'es', 'fs', 'gs')]


libc = ctypes.CDLL(None, use_errno=True)
errors = []


class TraceFailure(RuntimeError):
    pass


def check_call(result, label):
    if result == -1:
        code = ctypes.get_errno()
        raise OSError(code, f'{label}: {os.strerror(code)}')
    return result


def ptrace(request, pid, address=0, data=0):
    ctypes.set_errno(0)
    result = libc.ptrace(request, pid, address, data)
    if result == -1 and ctypes.get_errno():
        code = ctypes.get_errno()
        raise OSError(code, f'ptrace({request}, {pid}): {os.strerror(code)}')
    return result


def install_unlink_filter():
    if platform.machine() != 'x86_64':
        raise OSError(errno.ENOTSUP, 'seccomp filter requires x86_64')
    # Load arch, reject any other ABI, then trap native and x32 unlink calls.
    instructions = (SockFilter * 10)(
        SockFilter(0x20, 0, 0, 4),
        SockFilter(0x15, 1, 0, AUDIT_ARCH_X86_64),
        SockFilter(0x06, 0, 0, SECCOMP_RET_KILL_PROCESS),
        SockFilter(0x20, 0, 0, 0),
        SockFilter(0x15, 4, 0, SYS_UNLINK),
        SockFilter(0x15, 3, 0, SYS_UNLINKAT),
        SockFilter(0x15, 2, 0, SYS_UNLINK | X32_SYSCALL_BIT),
        SockFilter(0x15, 1, 0, SYS_UNLINKAT | X32_SYSCALL_BIT),
        SockFilter(0x06, 0, 0, SECCOMP_RET_ALLOW),
        SockFilter(0x06, 0, 0, SECCOMP_RET_TRACE),
    )
    program = SockFprog(len(instructions), instructions)
    check_call(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), 'PR_SET_NO_NEW_PRIVS')
    check_call(libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER,
                          ctypes.byref(program)), 'PR_SET_SECCOMP')


def tracee_exec(command, cwd, stdout_path, stderr_path, parent_pid):
    try:
        check_call(libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0),
                   'PR_SET_PDEATHSIG')
        if os.getppid() != parent_pid:
            os._exit(125)
        if ptrace(PTRACE_TRACEME, 0) != 0:
            os._exit(125)
        os.kill(os.getpid(), signal.SIGSTOP)
        install_unlink_filter()
        os.chdir(cwd)
        with open(stdout_path, 'wb') as stdout_stream, open(stderr_path, 'wb') as stderr_stream:
            os.dup2(stdout_stream.fileno(), 1)
            os.dup2(stderr_stream.fileno(), 2)
            os.execvpe(command[0], command, os.environ.copy())
    except BaseException as error:
        try:
            Path(stderr_path).write_text(f'tracee setup failed: {type(error).__name__}: {error}\n')
        except OSError:
            pass
        os._exit(125)


def process_identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return fields[19]


def task_record(pid):
    try:
        identity = process_identity(pid)
        status = Path(f'/proc/{pid}/status').read_text().splitlines()
        tgid = int(next(line.split()[1] for line in status if line.startswith('Tgid:')))
        pidfd = None if tgid != pid else os.pidfd_open(pid, 0)
        return {'identity': identity, 'pidfd': pidfd, 'bootstrap': True, 'bound': True}
    except (OSError, AttributeError, IndexError) as error:
        raise TraceFailure(f'cannot bind traced child {pid} identity: {error}') from error


def send_pidfd_signal(task, signum):
    if not hasattr(signal, 'pidfd_send_signal'):
        raise TraceFailure('pidfd_send_signal is unavailable')
    try:
        signal.pidfd_send_signal(task['pidfd'], signum)
    except ProcessLookupError:
        return False
    return True


def task_is_live(pid, task):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] == task.get('identity') and fields[0] != 'Z'
    except (OSError, IndexError):
        return False


def read_tracee_string(pid, address, maximum=4096):
    if address == 0:
        raise TraceFailure('unlink pathname pointer is null')
    memory = os.open(f'/proc/{pid}/mem', os.O_RDONLY | os.O_CLOEXEC)
    try:
        data = os.pread(memory, maximum, address)
    finally:
        os.close(memory)
    end = data.find(b'\0')
    if end < 0:
        raise TraceFailure('unlink pathname is unreadable or exceeds 4095 bytes')
    try:
        return data[:end].decode('utf-8', 'strict')
    except UnicodeDecodeError as error:
        raise TraceFailure(f'unlink pathname is not valid UTF-8: {error}') from error


def tracee_base(pid, dirfd):
    path = Path(f'/proc/{pid}/cwd') if dirfd == AT_FDCWD else Path(f'/proc/{pid}/fd/{dirfd}')
    resolved = os.readlink(path)
    if resolved.endswith(' (deleted)'):
        raise TraceFailure(f'unlink directory handle is deleted: {resolved}')
    return os.path.realpath(resolved)


def normalized_candidate(pid, directory_fd, path_text, owner_root):
    if os.path.isabs(path_text):
        candidate = os.path.normpath(path_text)
    else:
        candidate = os.path.normpath(os.path.join(tracee_base(pid, directory_fd), path_text))
    try:
        if os.path.commonpath((owner_root, candidate)) != owner_root:
            return None
    except ValueError:
        return None
    relative = os.path.relpath(candidate, owner_root)
    if not CAPTURE_PATTERN.fullmatch(relative):
        return None
    return relative


def open_beneath(root_fd, relative):
    # O_NONBLOCK makes a malicious or unexpected FIFO fail the following type
    # check without hanging the sole controller before it can retain evidence.
    how = OpenHow(os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0,
                  RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS)
    result = libc.syscall(SYS_OPENAT2, root_fd, relative.encode(),
                          ctypes.byref(how), ctypes.sizeof(how))
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, f'openat2({relative}): {os.strerror(code)}')
    return int(result)


def capture_target(pid, regs, owner_root, owner_fd, destination, held, events):
    syscall_number = regs.orig_rax & ~X32_SYSCALL_BIT
    if syscall_number == SYS_UNLINK:
        directory_fd = AT_FDCWD
        pointer = regs.rdi
    elif syscall_number == SYS_UNLINKAT:
        directory_fd = ctypes.c_int(regs.rdi & 0xFFFFFFFF).value
        pointer = regs.rsi
    else:
        return
    pathname = read_tracee_string(pid, pointer)
    relative = normalized_candidate(pid, directory_fd, pathname, owner_root)
    if relative is None:
        return
    descriptor = open_beneath(owner_fd, relative)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise TraceFailure(f'target is not a regular file: {relative}')
    if relative in held:
        previous = held[relative]
        if (metadata.st_dev, metadata.st_ino) != (previous['dev'], previous['ino']):
            os.close(descriptor)
            raise TraceFailure(f'target identity changed during capture: {relative}')
        os.close(descriptor)
        events.append({'pid': pid, 'syscall': 'unlinkat' if syscall_number == SYS_UNLINKAT else 'unlink',
                       'path': relative, 'dev': metadata.st_dev, 'ino': metadata.st_ino,
                       'opened_before_resume': True, 'already_retained': True})
        return
    target = destination / relative
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    held[relative] = {'fd': descriptor, 'dev': metadata.st_dev, 'ino': metadata.st_ino}
    events.append({'pid': pid, 'syscall': 'unlinkat' if syscall_number == SYS_UNLINKAT else 'unlink',
                   'path': relative, 'dev': metadata.st_dev, 'ino': metadata.st_ino,
                   'opened_before_resume': True})


def copy_fd(descriptor, target, deadline):
    digest = hashlib.sha256()
    size = 0
    temporary = target.with_name(target.name + '.partial')
    output = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    try:
        offset = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f'evidence copy exceeded common finalization deadline: {target}')
            chunk = os.pread(descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(output, view)
                view = view[written:]
            digest.update(chunk)
            size += len(chunk)
            offset += len(chunk)
        os.fsync(output)
    finally:
        os.close(output)
    os.replace(temporary, target)
    return {'bytes': size, 'sha256': digest.hexdigest()}


def capture_present_targets(owner_root, owner_fd, held, deadline):
    for current, directories, filenames in os.walk(owner_root, followlinks=False):
        if time.monotonic() >= deadline:
            raise TimeoutError('owner-buffer scan exceeded common finalization deadline')
        directories[:] = [name for name in directories
                          if not os.path.islink(os.path.join(current, name))]
        for filename in filenames:
            absolute = os.path.join(current, filename)
            relative = os.path.relpath(absolute, owner_root)
            if not CAPTURE_PATTERN.fullmatch(relative) or relative in held:
                continue
            descriptor = open_beneath(owner_fd, relative)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(descriptor)
                raise TraceFailure(f'present target is not a regular file: {relative}')
            held[relative] = {'fd': descriptor, 'dev': metadata.st_dev,
                              'ino': metadata.st_ino}


def write_result(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    os.replace(temporary, path)


def persist_owner_wait(destination, exit_path, owner_wait, owner_wait_path=None):
    write_result(destination / 'owner-step.wait.json', owner_wait)
    if owner_wait_path is not None:
        write_result(Path(owner_wait_path), owner_wait)
    temporary = exit_path.with_suffix(exit_path.suffix + '.tmp')
    temporary.write_text(f"{owner_wait['status']}\n")
    os.replace(temporary, exit_path)


def wait_for_tracee(pid, flags):
    try:
        return os.waitpid(pid, flags)
    except InterruptedError:
        return 0, 0


def run_capture(args, stop_requested=None):
    """Trace the owner synchronously inside the sole analysis controller."""
    global errors
    errors = []
    command = args.command[1:] if args.command and args.command[0] == '--' else args.command
    if not command or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise TraceFailure('tracer requires a Linux x86_64 owner command')
    owner_root = os.path.realpath(args.owner_tmp)
    owner_fd = os.open(owner_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    destination = Path(args.destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(args.ready).write_text('ptrace-child-tracer-ready\n')

    controller_pid = os.getpid()
    root_pid = os.fork()
    if root_pid == 0:
        tracee_exec(command, args.cwd, args.stdout, args.stderr, controller_pid)
        os._exit(125)

    tasks = {}
    held = {}
    events = []
    owner_wait = None
    trace_error = None
    exec_seen = False
    owner_exec = None
    security_attributes = None
    root_exit_seen = False
    orphan_term_started = None
    controller_stop = None
    controller_term_started = None
    cleanup_deadline = time.monotonic() + max(args.cleanup_deadline - time.time(), 0)
    tracer_self_before = resource.getrusage(resource.RUSAGE_SELF)
    tracer_children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    seccomp_event_count = 0
    try:
        waited, status = 0, 0
        while time.monotonic() < cleanup_deadline:
            waited, status = wait_for_tracee(root_pid, WAIT_ALL | os.WNOHANG)
            if waited != 0:
                break
            time.sleep(0.01)
        if waited == root_pid and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
            owner_wait = {'kind': 'exit' if os.WIFEXITED(status) else 'signal',
                          'status': os.waitstatus_to_exitcode(status),
                          'raw_wait_status': status, 'owner_exec_observed': False}
            root_exit_seen = True
            persist_owner_wait(destination, Path(args.owner_exit), owner_wait,
                               getattr(args, 'owner_wait', None))
        if waited != root_pid or not os.WIFSTOPPED(status) or os.WSTOPSIG(status) != signal.SIGSTOP:
            raise TraceFailure(f'owner bootstrap stop missing: wait={waited} status={status}')
        ptrace(PTRACE_SETOPTIONS, root_pid, 0, OPTIONS)
        tasks[root_pid] = {'identity': None, 'pidfd': None, 'bootstrap': False,
                           'bound': False}
        tasks[root_pid] = task_record(root_pid)
        tasks[root_pid]['bootstrap'] = False
        ptrace(PTRACE_CONT, root_pid, 0, 0)

        while tasks:
            requested_stop = stop_requested() if stop_requested else None
            if requested_stop and controller_stop is None:
                controller_stop = requested_stop
                controller_term_started = time.monotonic()
                requested_signal = signal.SIGTERM
                if requested_stop.startswith('signal-'):
                    try:
                        requested_signal = int(requested_stop.removeprefix('signal-'))
                    except ValueError:
                        raise TraceFailure(f'invalid controller signal request: {requested_stop}')
                target_pid = (owner_exec or {}).get('pid', root_pid)
                stop_task = tasks.get(target_pid)
                if stop_task and stop_task.get('pidfd') is not None:
                    send_pidfd_signal(stop_task, requested_signal)
                elif stop_task and not stop_task.get('bound'):
                    os.kill(target_pid, requested_signal)
                events.append({'event': 'controller-stop', 'reason': requested_stop,
                               'signal': requested_signal, 'target_pid': target_pid})
                expected_signal = (controller_stop.startswith('signal-') and
                                   getattr(args, 'expected_signal_for_fixtures', False))
                if not expected_signal:
                    errors.append(f'controller requested bounded stop: {controller_stop}')
            if controller_stop and controller_term_started is not None:
                if time.monotonic() >= min(controller_term_started + 2, cleanup_deadline):
                    for task in list(tasks.values()):
                        if task.get('pidfd') is not None and not task.get('controller_kill_sent'):
                            send_pidfd_signal(task, signal.SIGKILL)
                            task['controller_kill_sent'] = True
            live_tasks = {pid: task for pid, task in tasks.items() if task_is_live(pid, task)}
            if root_exit_seen and live_tasks:
                now = time.monotonic()
                if orphan_term_started is None:
                    orphan_term_started = now
                    errors.append('owned descendants remained after owner exit')
                    for task in list(live_tasks.values()):
                        if task.get('pidfd') is not None:
                            send_pidfd_signal(task, signal.SIGTERM)
                elif now >= min(orphan_term_started + 2, cleanup_deadline):
                    for task in list(live_tasks.values()):
                        if task.get('pidfd') is not None and not task.get('orphan_kill_sent'):
                            send_pidfd_signal(task, signal.SIGKILL)
                            task['orphan_kill_sent'] = True
                if now >= cleanup_deadline:
                    raise TraceFailure('owned descendants remained at the common cleanup deadline')
            waited, status = wait_for_tracee(-1, WAIT_ALL | os.WNOHANG)
            if waited == 0:
                time.sleep(0.01)
                continue
            task = tasks.get(waited)
            if os.WIFEXITED(status):
                code = os.WEXITSTATUS(status)
                if waited == root_pid:
                    owner_wait = {'kind': 'exit', 'status': code,
                                  'raw_wait_status': status}
                    root_exit_seen = True
                    try:
                        persist_owner_wait(destination, Path(args.owner_exit), owner_wait,
                                           getattr(args, 'owner_wait', None))
                    except OSError as error:
                        errors.append(f'persist owner exit before cleanup: {error}')
                if task:
                    if task.get('pidfd') is not None:
                        os.close(task['pidfd'])
                    del tasks[waited]
                continue
            if os.WIFSIGNALED(status):
                signum = os.WTERMSIG(status)
                if waited == root_pid:
                    owner_wait = {'kind': 'signal', 'status': -signum,
                                  'raw_wait_status': status, 'signal': signum}
                    root_exit_seen = True
                    try:
                        persist_owner_wait(destination, Path(args.owner_exit), owner_wait,
                                           getattr(args, 'owner_wait', None))
                    except OSError as error:
                        errors.append(f'persist owner exit before cleanup: {error}')
                if task:
                    if task.get('pidfd') is not None:
                        os.close(task['pidfd'])
                    del tasks[waited]
                continue
            if not os.WIFSTOPPED(status):
                continue

            if task is None:
                raise TraceFailure(f'wait event for unowned pid {waited}')

            stop = os.WSTOPSIG(status)
            event = status >> 16
            if task.get('bootstrap'):
                ptrace(PTRACE_SETOPTIONS, waited, 0, OPTIONS)
                task['bootstrap'] = False
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            if event in (PTRACE_EVENT_FORK, PTRACE_EVENT_VFORK, PTRACE_EVENT_CLONE):
                message = ctypes.c_ulong()
                ptrace(PTRACE_GETEVENTMSG, waited, 0, ctypes.byref(message))
                child_pid = int(message.value)
                if child_pid <= 0:
                    raise TraceFailure(f'ptrace birth event returned invalid pid {child_pid}')
                tasks[child_pid] = {'identity': None, 'pidfd': None,
                                    'bootstrap': True, 'bound': False}
                tasks[child_pid].update(task_record(child_pid))
                events.append({'event': 'birth', 'kind': event, 'parent': waited,
                               'child': child_pid})
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            if event == PTRACE_EVENT_SECCOMP:
                seccomp_event_count += 1
                registers = UserRegs()
                ptrace(PTRACE_GETREGS, waited, 0, ctypes.byref(registers))
                capture_target(waited, registers, owner_root, owner_fd,
                               destination, held, events)
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            if event == PTRACE_EVENT_EXEC and waited == root_pid:
                exec_seen = True
            if event == PTRACE_EVENT_EXEC:
                exec_message = ctypes.c_ulong()
                ptrace(PTRACE_GETEVENTMSG, waited, 0, ctypes.byref(exec_message))
                former_tid = int(exec_message.value)
                if former_tid and former_tid != waited:
                    former_task = tasks.pop(former_tid, None)
                    displaced_task = tasks.pop(waited, None)
                    if former_task is None:
                        raise TraceFailure(
                            f'non-leader exec remap references unowned tid {former_tid}'
                        )
                    if displaced_task and displaced_task.get('pidfd') is not None:
                        former_task['pidfd'] = displaced_task['pidfd']
                    else:
                        former_task['pidfd'] = os.pidfd_open(waited, 0)
                    former_task['identity'] = process_identity(waited)
                    former_task['bound'] = True
                    tasks[waited] = former_task
                    task = former_task
                    events.append({'event': 'exec-tid-remap', 'former_tid': former_tid,
                                   'leader_pid': waited})
                exec_epoch = time.time()
                exec_argv = Path(f'/proc/{waited}/cmdline').read_bytes().split(b'\0')
                exec_text = [item.decode('utf-8', 'replace') for item in exec_argv if item]
                executable = os.path.realpath(f'/proc/{waited}/exe')
                expected_executable = os.path.realpath(command[0])
                if waited == root_pid:
                    if executable != expected_executable or not any(
                            os.path.realpath(item) == os.path.realpath(args.command[-1])
                            for item in exec_text[1:] if item.startswith('/') ):
                        raise TraceFailure(
                            f'controller exec identity did not match the requested Actions step: '
                            f'executable={executable}; argv={exec_text}'
                        )
                expected_owner = os.path.realpath(args.owner_script_path)
                tracee_cwd = os.path.realpath(f'/proc/{waited}/cwd')
                owner_script_seen = any(
                    os.path.realpath(item if os.path.isabs(item)
                                     else os.path.join(tracee_cwd, item)) == expected_owner
                    for item in exec_text[1:] if not item.startswith('-')
                )
                if owner_script_seen and owner_exec is None:
                    if executable != expected_executable:
                        raise TraceFailure(
                            f'lint owner interpreter changed unexpectedly: {executable}'
                        )
                    owner_exec = {'pid': waited, 'executable': executable,
                                  'script': expected_owner, 'argv': exec_text,
                                  'epoch': exec_epoch,
                                  'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(exec_epoch))}
                    exec_deadline = getattr(args, 'exec_deadline_epoch', None)
                    if exec_deadline is not None and exec_epoch > exec_deadline:
                        raise TraceFailure(
                            f'lint owner exec crossed the one-minute readiness boundary: '
                            f'exec={exec_epoch:.6f}; deadline={exec_deadline:.6f}'
                        )
                    callback = getattr(args, 'owner_exec_callback', None)
                    if callback:
                        callback(owner_exec)
                attributes = {}
                for line in Path(f'/proc/{waited}/status').read_text().splitlines():
                    if line.startswith(('NoNewPrivs:', 'Seccomp:')):
                        key, value = line.split(':', 1)
                        attributes[key] = value.strip()
                if attributes.get('NoNewPrivs') != '1' or attributes.get('Seccomp') != '2':
                    raise TraceFailure(f'expected inherited no_new_privs/seccomp at exec, got {attributes}')
                if waited == root_pid:
                    security_attributes = attributes
                events.append({'event': 'exec', 'pid': waited,
                               'security_attributes': attributes})
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            if event:
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            # Forward genuine tracee signals, including INT, TERM, and HUP.
            ptrace(PTRACE_CONT, waited, 0, stop)

        if owner_wait is None:
            raise TraceFailure('owner wait status was not observed')
        if not exec_seen:
            raise TraceFailure('owner did not reach exec after installing the seccomp filter')
    except BaseException as error:
        trace_error = f'{type(error).__name__}: {error}'
        errors.append(trace_error)
        if root_pid not in tasks and not root_exit_seen:
            try:
                os.kill(root_pid, signal.SIGKILL)
                waited, status = wait_for_tracee(root_pid, WAIT_ALL | os.WNOHANG)
                if waited == 0:
                    ptrace(PTRACE_CONT, root_pid, 0, signal.SIGKILL)
                    waited, status = wait_for_tracee(root_pid, 0)
                if waited == root_pid and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
                    owner_wait = {'kind': 'exit' if os.WIFEXITED(status) else 'signal',
                                  'status': os.waitstatus_to_exitcode(status),
                                  'raw_wait_status': status,
                                  'owner_exec_observed': False}
                    root_exit_seen = True
                    try:
                        persist_owner_wait(destination, Path(args.owner_exit), owner_wait,
                                           getattr(args, 'owner_wait', None))
                    except OSError as write_error:
                        errors.append(f'persist bootstrap failure wait status: {write_error}')
            except (ChildProcessError, OSError) as cleanup_error:
                errors.append(f'bootstrap child cleanup/wait failed: {cleanup_error}')
        for pid, task in list(tasks.items()):
            if task.get('pidfd') is not None:
                try:
                    send_pidfd_signal(task, signal.SIGKILL)
                except (OSError, TraceFailure) as signal_error:
                    errors.append(f'kill traced child: {signal_error}')
            elif not task.get('bound'):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        while tasks and time.monotonic() < cleanup_deadline:
            waited, status = wait_for_tracee(-1, WAIT_ALL | os.WNOHANG)
            if waited == 0:
                time.sleep(0.01)
                continue
            if waited == root_pid and owner_wait is None and (
                    os.WIFEXITED(status) or os.WIFSIGNALED(status)):
                owner_wait = {'kind': 'exit' if os.WIFEXITED(status) else 'signal',
                              'status': os.waitstatus_to_exitcode(status),
                              'raw_wait_status': status, 'owner_exec_observed': exec_seen}
                root_exit_seen = True
                try:
                    persist_owner_wait(destination, Path(args.owner_exit), owner_wait,
                                       getattr(args, 'owner_wait', None))
                except OSError as write_error:
                    errors.append(f'persist owner wait during failure cleanup: {write_error}')
            task = tasks.pop(waited, None)
            if task is None and os.WIFSTOPPED(status):
                errors.append(f'cleanup wait observed unbound traced pid {waited}')
                task = {'pidfd': None, 'identity': None, 'bootstrap': False,
                        'bound': False}
            if task and os.WIFSTOPPED(status):
                try:
                    ptrace(PTRACE_CONT, waited, 0, signal.SIGKILL)
                    tasks[waited] = task
                except OSError as resume_error:
                    errors.append(f'resume killed tracee {waited}: {resume_error}')
            elif task and task.get('pidfd') is not None:
                os.close(task['pidfd'])
        if tasks:
            errors.append(f'{len(tasks)} ptrace-owned task(s) remained at the common cleanup deadline')
        if root_pid not in tasks and owner_wait is None:
            try:
                waited, status = wait_for_tracee(root_pid, WAIT_ALL | os.WNOHANG)
                if waited == root_pid and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
                    owner_wait = {'kind': 'exit' if os.WIFEXITED(status) else 'signal',
                                  'status': os.waitstatus_to_exitcode(status),
                                  'raw_wait_status': status}
            except ChildProcessError:
                pass
    finally:
        tracer_self_after = resource.getrusage(resource.RUSAGE_SELF)
        tracer_children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        files = {}
        for task in tasks.values():
            if task.get('pidfd') is not None:
                try:
                    os.close(task['pidfd'])
                except OSError as error:
                    errors.append(f'close traced pidfd: {error}')
        try:
            capture_present_targets(owner_root, owner_fd, held, cleanup_deadline)
        except (OSError, TraceFailure) as error:
            errors.append(f'capture still-present owner buffers: {error}')
        for relative, record in held.items():
            try:
                target = destination / relative
                files[relative] = {
                    **copy_fd(record['fd'], target, cleanup_deadline),
                    'dev': record['dev'], 'ino': record['ino'],
                }
            except OSError as error:
                errors.append(f'finalize {relative}: {error}')
            finally:
                try:
                    os.close(record['fd'])
                except OSError:
                    pass
        if owner_wait is None:
            try:
                (Path(args.owner_exit)).write_text('unavailable\n')
                unavailable_wait = {
                    'status': None, 'reason': 'owner wait status unavailable',
                    'trace_error': trace_error,
                }
                write_result(destination / 'owner-step.wait.json', unavailable_wait)
                if getattr(args, 'owner_wait', None) is not None:
                    write_result(Path(args.owner_wait), unavailable_wait)
            except OSError as error:
                errors.append(f'persist unavailable owner wait: {error}')
        summary = {
            'owner_tmp': owner_root,
            'owner_wait': owner_wait if exec_seen else None,
            'owner_exec': owner_exec,
            'tracee_setup_wait': owner_wait if not exec_seen else None,
            'trace_error': trace_error,
            'finalized': not errors and owner_wait is not None and owner_exec is not None,
            'files': files,
            'unlink_events': events,
            'seccomp_event_count': seccomp_event_count,
            'maximum_held_descriptors': len(held),
            'retained_bytes': sum(record['bytes'] for record in files.values()),
            'tracer_cpu_seconds': {
                'self_user': tracer_self_after.ru_utime - tracer_self_before.ru_utime,
                'self_system': tracer_self_after.ru_stime - tracer_self_before.ru_stime,
                'children_user': tracer_children_after.ru_utime - tracer_children_before.ru_utime,
                'children_system': tracer_children_after.ru_stime - tracer_children_before.ru_stime,
            },
            'errors': errors,
            'security_attributes': security_attributes,
            'tracing': 'own-child seccomp RET_TRACE + ptrace',
            'controller_stop': controller_stop,
        }
        try:
            write_result(destination / 'capture-summary.json', summary)
        except OSError as error:
            print(f'cannot write capture summary: {error}', file=sys.stderr)
            errors.append(f'write capture summary: {error}')
        os.close(owner_fd)

    if errors or owner_wait is None:
        status = 125
    elif owner_wait['status'] < 0:
        status = 128 + abs(owner_wait['status'])
    else:
        status = owner_wait['status']
    return status, summary
