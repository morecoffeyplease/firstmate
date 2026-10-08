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
import select
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
PTRACE_GETSIGINFO = 0x4202
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
PTRACE_LISTEN = 0x4208
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
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
SYS_OPENAT2 = 437
SYS_UNLINK = 87
SYS_UNLINKAT = 263
X32_SYSCALL_BIT = 0x40000000
CAPTURE_PATTERN = re.compile(r"fm-lint\.[^/]+/output/shard\.[0-9]+\.(?:out|rc)\Z")
SHARD_NAME = re.compile(r'shard\.[0-9]+\.(?:out|rc)\Z')
OWNER_TEMP_COMPONENT = re.compile(r'fm-lint\.[^/]+')
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
failure_injection = None


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


def signal_delivery_stop(pid):
    information = ctypes.create_string_buffer(128)
    try:
        ptrace(PTRACE_GETSIGINFO, pid, 0, ctypes.byref(information))
        return True
    except OSError as error:
        if error.errno == errno.EINVAL:
            return False
        raise


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


def tracee_exec(command, cwd, stdout_path, stderr_path, parent_pid, environment):
    try:
        check_call(libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0),
                   'PR_SET_PDEATHSIG')
        if os.getppid() != parent_pid:
            os._exit(125)
        if ptrace(PTRACE_TRACEME, 0) != 0:
            os._exit(125)
        os.kill(os.getpid(), signal.SIGSTOP)
        if failure_injection == 'seccomp-unavailable':
            raise OSError(errno.EPERM, 'injected seccomp installation refusal')
        install_unlink_filter()
        os.chdir(cwd)
        with open(stdout_path, 'wb') as stdout_stream, open(stderr_path, 'wb') as stderr_stream:
            os.dup2(stdout_stream.fileno(), 1)
            os.dup2(stderr_stream.fileno(), 2)
            os.execvpe(command[0], command, environment)
    except BaseException as error:
        try:
            Path(stderr_path).write_text(f'tracee setup failed: {type(error).__name__}: {error}\n')
        except OSError:
            pass
        os._exit(125)


def process_identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return fields[19]


def process_signal_state(pid):
    state = {}
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith(('SigBlk:', 'SigIgn:', 'SigCgt:')):
            key, value = line.split(':', 1)
            state[key] = value.strip()
    if len(state) != 3:
        raise TraceFailure(f'process signal state unavailable for pid {pid}: {state}')
    return state


def task_record(pid):
    try:
        identity = process_identity(pid)
        status = Path(f'/proc/{pid}/status').read_text().splitlines()
        tgid = int(next(line.split()[1] for line in status if line.startswith('Tgid:')))
        pidfd = os.pidfd_open(tgid, 0)
        leader_identity = process_identity(tgid)
        return {'pid': pid, 'identity': identity, 'leader_identity': leader_identity,
                'pidfd': pidfd, 'bootstrap': True, 'bound': True,
                'tgid': tgid, 'role': 'tracee-descendant'}
    except (OSError, AttributeError, IndexError, StopIteration) as error:
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


def openat2(root_fd, relative, flags, resolve):
    if failure_injection == 'openat2-unavailable':
        raise OSError(errno.ENOSYS, 'injected openat2 unavailability')
    how = OpenHow(flags, 0, resolve)
    result = libc.syscall(SYS_OPENAT2, root_fd, relative.encode(),
                          ctypes.byref(how), ctypes.sizeof(how))
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, f'openat2({relative}): {os.strerror(code)}')
    return int(result)


def open_beneath(root_fd, relative):
    # Refuse every symlink component so the held inode equals the unlink target.
    return openat2(root_fd, relative,
                   os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                   RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS)


def open_absolute_tracee_path(pid, absolute_path, owner_root):
    tracee_root = os.open(f'/proc/{pid}/root', os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        tracee_identity = os.fstat(tracee_root)
        controller_identity = os.stat('/')
        if (tracee_identity.st_dev, tracee_identity.st_ino) != (
                controller_identity.st_dev, controller_identity.st_ino):
            raise TraceFailure('tracee root differs from controller root for absolute unlink')
        root_relative = owner_root.lstrip('/')
        path_relative = absolute_path.lstrip('/')
        if not path_relative.startswith(root_relative.rstrip('/') + '/'):
            raise TraceFailure(f'absolute unlink path escaped exact owner root: {absolute_path}')
        return openat2(tracee_root, path_relative,
                       os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                       RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS)
    finally:
        os.close(tracee_root)


def tracee_directory(pid, dirfd, owner_root, owner_fd):
    proc_path = Path(f'/proc/{pid}/cwd') if dirfd == AT_FDCWD else Path(f'/proc/{pid}/fd/{dirfd}')
    try:
        actual_fd = os.open(proc_path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        actual = os.fstat(actual_fd)
        resolved = os.readlink(proc_path)
    except OSError as error:
        raise TraceFailure(f'cannot bind original unlink dirfd {dirfd}: {error}') from error
    if resolved.endswith(' (deleted)'):
        os.close(actual_fd)
        raise TraceFailure(f'unlink directory handle is deleted: {resolved}')
    canonical = os.path.realpath(resolved)
    try:
        if os.path.commonpath((owner_root, canonical)) != owner_root:
            os.close(actual_fd)
            return None, None, None
    except ValueError:
        os.close(actual_fd)
        return None, None, None
    relative = os.path.relpath(canonical, owner_root)
    relative = '' if relative == '.' else relative
    if relative not in ('', 'output'):
        return actual_fd, relative, (actual.st_dev, actual.st_ino)
    verify_path = relative or '.'
    verified_fd = openat2(owner_fd, verify_path,
                          os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC,
                          RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS)
    verified = os.fstat(verified_fd)
    os.close(verified_fd)
    if (actual.st_dev, actual.st_ino) != (verified.st_dev, verified.st_ino):
        os.close(actual_fd)
        raise TraceFailure(f'proc dirfd identity does not match owner-root path: {relative}')
    return actual_fd, relative, (actual.st_dev, actual.st_ino)


def exact_candidate(pid, directory_fd, path_text, owner_root, owner_fd):
    if not isinstance(path_text, str) or not path_text:
        raise TraceFailure('unlink path is empty or malformed')
    if '\x00' in path_text:
        raise TraceFailure('unlink path contains NUL')
    if os.path.isabs(path_text):
        prefix = owner_root.rstrip('/') + '/'
        if not path_text.startswith(prefix):
            return None
        relative = path_text[len(prefix):]
        parts = relative.split('/')
        if any(part in ('', '.', '..') for part in parts):
            if OWNER_TEMP_COMPONENT.search(relative) and SHARD_NAME.search(relative):
                raise TraceFailure(f'unsupported absolute unlink form: {path_text}')
            return None
        if not CAPTURE_PATTERN.fullmatch(relative):
            if OWNER_TEMP_COMPONENT.search(relative) and SHARD_NAME.search(relative):
                raise TraceFailure(f'unsupported owner-buffer unlink form: {path_text}')
            return None
        descriptor = open_absolute_tracee_path(pid, path_text, owner_root)
        return relative, descriptor

    actual_fd, directory_relative, _identity = tracee_directory(
        pid, directory_fd, owner_root, owner_fd
    )
    if actual_fd is None:
        if OWNER_TEMP_COMPONENT.search(path_text) and SHARD_NAME.search(path_text):
            raise TraceFailure(
                f'capture-shaped unlink uses a dirfd outside the exact owner root: {path_text}'
            )
        return None
    try:
        if (directory_relative not in ('', 'output') and
                OWNER_TEMP_COMPONENT.search(directory_relative) and SHARD_NAME.search(path_text)):
            raise TraceFailure(f'unsupported owner-buffer dirfd path: {directory_relative}/{path_text}')
        if directory_relative == 'output' and CAPTURE_PATTERN.fullmatch(
                f'output/{path_text}'):
            relative = f'output/{path_text}'
        elif directory_relative == '' and CAPTURE_PATTERN.fullmatch(path_text):
            relative = path_text
        elif directory_relative == '' and CAPTURE_PATTERN.fullmatch(f'output/{path_text}'):
            relative = f'output/{path_text}'
        else:
            if ((OWNER_TEMP_COMPONENT.search(path_text) and SHARD_NAME.search(path_text)) or
                    (directory_relative in ('', 'output') and SHARD_NAME.search(path_text))):
                raise TraceFailure(f'unsupported relative unlink form: {path_text}')
            return None
        if '/' in path_text and not (directory_relative == '' and path_text.startswith('output/')):
            raise TraceFailure(f'unsupported relative unlink path: {path_text}')
        if path_text in ('.', '..'):
            raise TraceFailure(f'unsupported relative unlink path: {path_text}')
        descriptor = open_beneath(actual_fd, path_text)
        return relative, descriptor
    finally:
        os.close(actual_fd)


def capture_target(pid, regs, owner_root, owner_fd, destination, held, events):
    raw_syscall_number = regs.orig_rax
    if raw_syscall_number & X32_SYSCALL_BIT:
        raise TraceFailure(f'unsupported x32 unlink ABI syscall {raw_syscall_number:#x}')
    syscall_number = raw_syscall_number
    if syscall_number == SYS_UNLINK:
        directory_fd = AT_FDCWD
        pointer = regs.rdi
    elif syscall_number == SYS_UNLINKAT:
        directory_fd = ctypes.c_int(regs.rdi & 0xFFFFFFFF).value
        pointer = regs.rsi
    else:
        return
    pathname = read_tracee_string(pid, pointer)
    candidate = exact_candidate(pid, directory_fd, pathname, owner_root, owner_fd)
    if candidate is None:
        return
    relative, descriptor = candidate
    if failure_injection == 'descriptor-exhaustion':
        os.close(descriptor)
        raise OSError(errno.EMFILE, 'injected descriptor exhaustion')
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
    if failure_injection == 'storage-failure':
        raise OSError(errno.ENOSPC, 'injected evidence storage failure')
    if time.monotonic() >= deadline:
        raise TimeoutError(f'evidence copy refused at common finalization deadline: {target}')
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
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'evidence write exceeded common finalization deadline: {target}')
                written = os.write(output, view)
                if written <= 0:
                    raise OSError('short evidence file write')
                view = view[written:]
            digest.update(chunk)
            size += len(chunk)
            offset += len(chunk)
        os.fsync(output)
        if time.monotonic() >= deadline:
            raise TimeoutError(f'evidence fsync completed at or after common finalization deadline: {target}')
    finally:
        os.close(output)
    os.replace(temporary, target)
    if time.monotonic() >= deadline:
        raise TimeoutError(f'evidence rename completed at or after common finalization deadline: {target}')
    directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if time.monotonic() >= deadline:
        raise TimeoutError(f'evidence directory sync completed at or after common finalization deadline: {target}')
    return {'bytes': size, 'sha256': digest.hexdigest()}


def capture_present_targets(owner_root, owner_fd, held, deadline):
    for current, directories, filenames in os.walk(owner_root, followlinks=False):
        if time.monotonic() >= deadline:
            raise TimeoutError('owner-buffer scan exceeded common finalization deadline')
        directories[:] = [name for name in directories
                          if not os.path.islink(os.path.join(current, name))]
        for filename in filenames:
            if time.monotonic() >= deadline:
                raise TimeoutError('owner-buffer file scan exceeded common finalization deadline')
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
            if time.monotonic() >= deadline:
                raise TimeoutError('owner-buffer scan completed at or after common finalization deadline')


def write_result(path, value, deadline=None):
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f'evidence write started after common deadline: {path}')
    temporary = path.with_suffix(path.suffix + '.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    try:
        payload = (json.dumps(value, indent=2) + '\n').encode()
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError('short write to evidence record')
            view = view[written:]
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f'evidence write crossed common deadline: {path}')
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
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f'evidence record finalized at or after common deadline: {path}')


def write_text_result(path, value, deadline=None):
    path = Path(path)
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f'evidence write started after common deadline: {path}')
    temporary = path.with_suffix(path.suffix + '.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    try:
        payload = value.encode()
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError('short write to evidence text')
            view = view[written:]
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f'evidence text write crossed common deadline: {path}')
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
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f'evidence text finalized at or after common deadline: {path}')


def persist_owner_wait(destination, exit_path, owner_wait, owner_wait_path=None,
                       deadline=None):
    write_result(destination / 'owner-step.wait.json', owner_wait, deadline)
    if owner_wait_path is not None:
        write_result(Path(owner_wait_path), owner_wait, deadline)
    write_text_result(exit_path, f"{owner_wait['status']}\n", deadline)


def run_capture(args, stop_requested=None):
    """Trace the owner synchronously inside the sole analysis controller."""
    global errors, failure_injection
    errors = []
    failure_injection = getattr(args, 'failure_injection', None)
    command = args.command[1:] if args.command and args.command[0] == '--' else args.command
    if not command or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise TraceFailure('tracer requires a Linux x86_64 owner command')
    if getattr(args, 'failure_injection', None) == 'ptrace-denied':
        raise TraceFailure('injected ptrace permission refusal before child creation')
    owner_root = os.path.realpath(args.owner_tmp)
    owner_fd = os.open(owner_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    destination = Path(args.destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(args.ready).write_text('ptrace-child-tracer-ready\n')

    controller_pid = os.getpid()
    root_pid = os.fork()
    if root_pid == 0:
        tracee_exec(command, args.cwd, args.stdout, args.stderr, controller_pid,
                    args.environment)
        os._exit(125)

    wait_owner = args.wait_owner
    root_placeholder = {'identity': None, 'leader_identity': None, 'pidfd': None,
                        'bootstrap': True, 'bound': False, 'tgid': root_pid,
                        'role': 'step-shell'}
    tasks = {root_pid: root_placeholder}
    wait_owner.register(root_pid, 'step-shell', None, None, root_pid)
    held = {}
    events = []
    owner_wait = None
    step_wait = None
    sampler_wait = None
    trace_error = None
    exec_seen = False
    owner_exec = None
    security_attributes = None
    root_exit_seen = False
    orphan_term_started = None
    controller_stop = None
    controller_term_started = None
    cleanup_deadline = args.cleanup_deadline_monotonic
    tracer_self_before = resource.getrusage(resource.RUSAGE_SELF)
    tracer_children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    seccomp_event_count = 0
    injected_failure = False
    try:
        bootstrap_event = None
        if failure_injection == 'bootstrap-failure':
            raise TraceFailure('injected tracer failure during root bootstrap')
        while time.monotonic() < cleanup_deadline:
            if stop_requested and stop_requested():
                raise TraceFailure('analysis stop requested before tracee bootstrap completed')
            bootstrap_event = wait_owner.wait_any()
            if bootstrap_event is None:
                time.sleep(0.01)
                continue
            waited, status, role_record, wait_entry = bootstrap_event
            if waited == args.sampler_pid:
                sampler_wait = wait_entry if wait_entry['kind'] in ('exit', 'signal') else sampler_wait
                events.append({'event': 'auxiliary-wait', 'wait': wait_entry})
                continue
            if waited == root_pid:
                break
            if role_record.get('role') == 'pending-tracee':
                task = dict(role_record)
                task.update({'bootstrap': True, 'bound': False, 'pidfd': None})
                tasks[waited] = task
                ptrace(PTRACE_SETOPTIONS, waited, 0, OPTIONS)
                task['bootstrap'] = False
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            raise TraceFailure(f'unexpected child wait before root bootstrap: {wait_entry}')
        if bootstrap_event is None:
            raise TraceFailure('tracee bootstrap did not produce a wait before cleanup deadline')
        waited, status, _role_record, _wait_entry = bootstrap_event
        if waited == root_pid and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
            step_wait = {'kind': 'exit' if os.WIFEXITED(status) else 'signal',
                         'status': os.waitstatus_to_exitcode(status),
                         'raw_wait_status': status, 'owner_exec_observed': False,
                         'role': 'step-shell'}
            root_exit_seen = True
            tasks.pop(root_pid, None)
            persist_owner_wait(destination, Path(args.step_exit), step_wait,
                               args.step_wait, cleanup_deadline)
        if waited != root_pid or not os.WIFSTOPPED(status) or os.WSTOPSIG(status) != signal.SIGSTOP:
            raise TraceFailure(f'owner bootstrap stop missing: wait={waited} status={status}')
        ptrace(PTRACE_SETOPTIONS, root_pid, 0, OPTIONS)
        bound_root = task_record(root_pid)
        tasks[root_pid].update(bound_root)
        tasks[root_pid]['bootstrap'] = False
        tasks[root_pid]['role'] = 'step-shell'
        wait_owner.register(root_pid, 'step-shell', tasks[root_pid]['identity'],
                            tasks[root_pid]['pidfd'], tasks[root_pid]['tgid'])
        ptrace(PTRACE_CONT, root_pid, 0, 0)

        while tasks:
            if time.monotonic() >= cleanup_deadline:
                raise TraceFailure('common cleanup deadline reached before every owned wait retired')
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
                if stop_task:
                    events.append({'event': 'signal-delivery-request',
                                   **wait_owner.signal(target_pid, requested_signal)})
                else:
                    raise TraceFailure(f'owner signal target is not owned: {target_pid}')
                events.append({'event': 'controller-stop', 'reason': requested_stop,
                               'signal': requested_signal, 'target_pid': target_pid})
                expected_signal = (controller_stop.startswith('signal-') and
                                   getattr(args, 'expected_signal_for_fixtures', False))
                if not expected_signal:
                    errors.append(f'controller requested bounded stop: {controller_stop}')
            if controller_stop and controller_term_started is not None:
                if time.monotonic() >= min(controller_term_started + 2, cleanup_deadline):
                    for task in list(tasks.values()):
                        if not task.get('controller_kill_sent'):
                            wait_owner.signal(task['pid'], signal.SIGKILL)
                            task['controller_kill_sent'] = True
            live_tasks = {pid: task for pid, task in tasks.items() if task_is_live(pid, task)}
            if root_exit_seen and live_tasks:
                now = time.monotonic()
                if orphan_term_started is None:
                    orphan_term_started = now
                    errors.append('owned descendants remained after owner exit')
                    for task in list(live_tasks.values()):
                        wait_owner.signal(task['pid'], signal.SIGTERM)
                elif now >= min(orphan_term_started + 2, cleanup_deadline):
                    for task in list(live_tasks.values()):
                        if not task.get('orphan_kill_sent'):
                            wait_owner.signal(task['pid'], signal.SIGKILL)
                            task['orphan_kill_sent'] = True
                if now >= cleanup_deadline:
                    raise TraceFailure('owned descendants remained at the common cleanup deadline')
            if time.monotonic() >= cleanup_deadline:
                raise TraceFailure('common cleanup deadline reached while tracing')
            wait_event = wait_owner.wait_any()
            if wait_event is None:
                time.sleep(0.01)
                continue
            waited, status, role_record, wait_entry = wait_event
            if waited == args.sampler_pid:
                sampler_wait = wait_entry if wait_entry['kind'] in ('exit', 'signal') else sampler_wait
                events.append({'event': 'auxiliary-wait', 'wait': wait_entry})
                continue
            task = tasks.get(waited)
            if task is None and role_record.get('role') == 'pending-tracee':
                task = dict(role_record)
                task.update({'pid': waited, 'bootstrap': False, 'bound': False, 'pidfd': None,
                             'role': 'pending-tracee', 'tgid': role_record.get('tgid', waited)})
                tasks[waited] = task
            if os.WIFEXITED(status):
                code = os.WEXITSTATUS(status)
                observed = {'kind': 'exit', 'status': code,
                            'raw_wait_status': status, 'role': task.get('role') if task else role_record.get('role'),
                            'identity': task.get('identity') if task else role_record.get('identity'),
                            'pid': waited}
                if waited == root_pid:
                    step_wait = observed
                    root_exit_seen = True
                    try:
                        persist_owner_wait(destination, Path(args.step_exit), step_wait,
                                           args.step_wait, cleanup_deadline)
                    except OSError as error:
                        errors.append(f'persist step-shell wait before cleanup: {error}')
                if task and task.get('role') == 'executable-owner':
                    owner_wait = observed
                    try:
                        persist_owner_wait(destination, Path(args.owner_exit), owner_wait,
                                           args.owner_wait, cleanup_deadline)
                    except OSError as error:
                        errors.append(f'persist executable-owner wait before cleanup: {error}')
                if task:
                    if task.get('pidfd') is not None:
                        os.close(task['pidfd'])
                    del tasks[waited]
                continue
            if os.WIFSIGNALED(status):
                signum = os.WTERMSIG(status)
                observed = {'kind': 'signal', 'status': -signum,
                            'raw_wait_status': status, 'signal': signum,
                            'role': task.get('role') if task else role_record.get('role'),
                            'identity': task.get('identity') if task else role_record.get('identity'),
                            'pid': waited}
                if waited == root_pid:
                    step_wait = observed
                    root_exit_seen = True
                    try:
                        persist_owner_wait(destination, Path(args.step_exit), step_wait,
                                           args.step_wait, cleanup_deadline)
                    except OSError as error:
                        errors.append(f'persist step-shell wait before cleanup: {error}')
                if task and task.get('role') == 'executable-owner':
                    owner_wait = observed
                    try:
                        persist_owner_wait(destination, Path(args.owner_exit), owner_wait,
                                           args.owner_wait, cleanup_deadline)
                    except OSError as error:
                        errors.append(f'persist executable-owner wait before cleanup: {error}')
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
                if time.monotonic() >= cleanup_deadline:
                    raise TraceFailure('cleanup deadline reached before ptrace birth reduction')
                message = ctypes.c_ulong()
                ptrace(PTRACE_GETEVENTMSG, waited, 0, ctypes.byref(message))
                child_pid = int(message.value)
                if child_pid <= 0:
                    raise TraceFailure(f'ptrace birth event returned invalid pid {child_pid}')
                child_placeholder = {'pid': child_pid, 'identity': None,
                                     'leader_identity': None, 'pidfd': None,
                                     'bootstrap': True, 'bound': False,
                                     'tgid': child_pid, 'role': 'tracee-descendant'}
                tasks.setdefault(child_pid, child_placeholder)
                pending_task = tasks[child_pid]
                wait_owner.register(child_pid, 'tracee-descendant',
                                    pending_task.get('identity'), pending_task.get('pidfd'),
                                    pending_task.get('tgid', child_pid))
                if failure_injection == 'birth-enrichment-failure':
                    raise TraceFailure('injected failure after ptrace child birth registration')
                try:
                    enriched = task_record(child_pid)
                    pending_task.update(enriched)
                    pending_task['pid'] = child_pid
                    wait_owner.register(child_pid, tasks[child_pid]['role'],
                                        tasks[child_pid]['identity'],
                                        tasks[child_pid]['pidfd'], tasks[child_pid]['tgid'])
                except TraceFailure as error:
                    events.append({'event': 'birth-enrichment-failed', 'pid': child_pid,
                                   'error': str(error), 'placeholder-retained': True})
                    raise
                events.append({'event': 'birth', 'kind': event, 'parent': waited,
                               'child': child_pid})
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            if event == PTRACE_EVENT_SECCOMP:
                if time.monotonic() >= cleanup_deadline:
                    raise TraceFailure('cleanup deadline reached before unlink target acquisition')
                seccomp_event_count += 1
                registers = UserRegs()
                ptrace(PTRACE_GETREGS, waited, 0, ctypes.byref(registers))
                if (getattr(args, 'failure_injection', None) == 'before-unlink' and
                        not injected_failure):
                    injected_failure = True
                    events.append({'event': 'injected-failure', 'stage': 'before-unlink',
                                   'pid': waited, 'raw_wait_status': status})
                    raise TraceFailure('injected tracer failure before owner unlink')
                capture_target(waited, registers, owner_root, owner_fd,
                               destination, held, events)
                if (getattr(args, 'failure_injection', None) == 'after-buffer-hold' and
                        not injected_failure and held):
                    injected_failure = True
                    events.append({'event': 'injected-failure', 'stage': 'after-buffer-hold',
                                   'pid': waited, 'raw_wait_status': status,
                                   'held_paths': sorted(held)})
                    raise TraceFailure('injected tracer failure after exact buffer acquisition')
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            if event == PTRACE_EVENT_EXEC and waited == root_pid:
                exec_seen = True
            if event == PTRACE_EVENT_EXEC:
                exec_message = ctypes.c_ulong()
                ptrace(PTRACE_GETEVENTMSG, waited, 0, ctypes.byref(exec_message))
                former_tid = int(exec_message.value)
                if former_tid and former_tid != waited:
                    former_task = tasks.get(former_tid)
                    displaced_task = tasks.get(waited)
                    if former_task is None:
                        raise TraceFailure(
                            f'non-leader exec remap references unowned tid {former_tid}'
                        )
                    staged_identity = process_identity(waited)
                    staged_status = Path(f'/proc/{waited}/status').read_text().splitlines()
                    staged_tgid = int(next(line.split()[1] for line in staged_status
                                           if line.startswith('Tgid:')))
                    staged_pidfd = (displaced_task or {}).get('pidfd')
                    opened_pidfd = staged_pidfd is None
                    if opened_pidfd:
                        staged_pidfd = os.pidfd_open(staged_tgid, 0)
                    staged = dict(former_task)
                    staged.update({'pid': waited, 'identity': staged_identity,
                                   'leader_identity': process_identity(staged_tgid),
                                   'tgid': staged_tgid, 'pidfd': staged_pidfd,
                                   'bootstrap': False, 'bound': True})
                    if process_identity(staged_tgid) != staged['leader_identity']:
                        if opened_pidfd:
                            os.close(staged_pidfd)
                        raise TraceFailure('exec remap leader identity changed during binding')
                    tasks[waited] = staged
                    tasks.pop(former_tid, None)
                    if former_task.get('pidfd') is not None and former_task['pidfd'] != staged_pidfd:
                        os.close(former_task['pidfd'])
                    if displaced_task is not None and displaced_task is not former_task:
                        tasks.pop(waited, None)
                        tasks[waited] = staged
                        if displaced_task.get('pidfd') is not None and displaced_task['pidfd'] != staged_pidfd:
                            os.close(displaced_task['pidfd'])
                    wait_owner.remap(former_tid, waited, {
                        'role': staged.get('role', 'tracee-descendant'),
                        'identity': staged_identity, 'pidfd': staged_pidfd,
                        'tgid': staged_tgid,
                    })
                    task = staged
                    events.append({'event': 'exec-tid-remap', 'former_tid': former_tid,
                                   'leader_pid': waited, 'identity': staged_identity,
                                   'tgid': staged_tgid, 'transaction': 'committed-after-enrichment'})
                exec_epoch = time.time()
                exec_monotonic = time.monotonic()
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
                expected_owner_executable = os.path.realpath(
                    getattr(args, 'owner_executable_path', command[0]))
                tracee_cwd = os.path.realpath(f'/proc/{waited}/cwd')
                owner_script_seen = any(
                    os.path.realpath(item if os.path.isabs(item)
                                     else os.path.join(tracee_cwd, item)) == expected_owner
                    for item in exec_text[1:] if not item.startswith('-')
                )
                if owner_script_seen and owner_exec is None:
                    if executable == expected_owner_executable:
                        owner_exec = {'pid': waited, 'executable': executable,
                                      'script': expected_owner, 'argv': exec_text,
                                      'epoch': exec_epoch,
                                      'monotonic': exec_monotonic,
                                      'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(exec_epoch))}
                        exec_deadline = getattr(args, 'exec_deadline_monotonic', None)
                        if exec_deadline is not None and exec_monotonic > exec_deadline:
                            raise TraceFailure(
                                f'lint owner exec crossed the one-minute readiness boundary: '
                                f'exec={exec_monotonic:.9f}; deadline={exec_deadline:.9f}'
                            )
                        task['role'] = 'executable-owner'
                        wait_owner.register(waited, 'executable-owner', task['identity'],
                                            task['pidfd'], task['tgid'])
                        callback = getattr(args, 'owner_exec_callback', None)
                        if callback:
                            callback(owner_exec)
                attributes = {}
                for line in Path(f'/proc/{waited}/status').read_text().splitlines():
                    if line.startswith(('NoNewPrivs:', 'Seccomp:')):
                        key, value = line.split(':', 1)
                        attributes[key] = value.strip()
                signal_attributes = process_signal_state(waited)
                if attributes.get('NoNewPrivs') != '1' or attributes.get('Seccomp') != '2':
                    raise TraceFailure(f'expected inherited no_new_privs/seccomp at exec, got {attributes}')
                if waited == root_pid:
                    security_attributes = attributes
                events.append({'event': 'exec', 'pid': waited,
                               'security_attributes': attributes,
                               'signal_attributes': signal_attributes})
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            if event == PTRACE_EVENT_STOP:
                events.append({'event': 'group-stop', 'pid': waited, 'signal': stop,
                               'role': task.get('role'), 'supported': False})
                raise TraceFailure(f'job-control group stop is unsupported: pid={waited} signal={stop}')
            if event:
                ptrace(PTRACE_CONT, waited, 0, 0)
                continue
            if not signal_delivery_stop(waited):
                events.append({'event': 'group-stop', 'pid': waited, 'signal': stop,
                               'role': task.get('role'), 'supported': False})
                raise TraceFailure(f'job-control group stop is unsupported: pid={waited} signal={stop}')
            events.append({'event': 'signal-delivery-stop', 'pid': waited,
                           'signal': stop, 'role': task.get('role'),
                           'disposition': 'forward-original'})
            ptrace(PTRACE_CONT, waited, 0, stop)

        if owner_wait is None:
            raise TraceFailure('owner wait status was not observed')
        if getattr(args, 'failure_injection', None) == 'after-owner-exit':
            raise TraceFailure('injected tracer failure after durable executable-owner wait')
        if not exec_seen:
            raise TraceFailure('owner did not reach exec after installing the seccomp filter')
        phase_callback = getattr(args, 'lifecycle_callback', None)
        if phase_callback:
            phase_callback('RETIRE', 'all role-owned terminal waits reduced')
    except BaseException as error:
        trace_error = f'{type(error).__name__}: {error}'
        errors.append(trace_error)
        for pid, task in list(tasks.items()):
            try:
                if task.get('identity') is not None or task.get('pidfd') is not None:
                    wait_owner.signal(pid, signal.SIGKILL)
                elif pid == root_pid:
                    os.kill(pid, signal.SIGKILL)
                else:
                    errors.append(f'cannot identity-qualify cleanup signal for pending child {pid}')
            except (OSError, RuntimeError, TraceFailure) as signal_error:
                errors.append(f'kill exact traced child {pid}: {signal_error}')
        while tasks and time.monotonic() < cleanup_deadline:
            try:
                wait_event = wait_owner.wait_any()
            except (OSError, RuntimeError) as wait_error:
                errors.append(f'role-aware cleanup wait failed: {wait_error}')
                break
            if wait_event is None:
                time.sleep(0.01)
                continue
            waited, status, role_record, wait_entry = wait_event
            if waited == args.sampler_pid:
                sampler_wait = wait_entry if wait_entry['kind'] in ('exit', 'signal') else sampler_wait
                events.append({'event': 'auxiliary-wait-during-cleanup', 'wait': wait_entry})
                continue
            task = tasks.pop(waited, None)
            if task is None and os.WIFSTOPPED(status):
                task = dict(role_record)
                task.update({'pid': waited, 'pidfd': None, 'bootstrap': False,
                             'bound': False, 'tgid': role_record.get('tgid', waited)})
                tasks[waited] = task
                try:
                    wait_owner.signal(waited, signal.SIGKILL)
                except (OSError, RuntimeError) as signal_error:
                    errors.append(f'kill newly discovered owned child {waited}: {signal_error}')
            if task and os.WIFSTOPPED(status):
                try:
                    ptrace(PTRACE_CONT, waited, 0, signal.SIGKILL)
                except OSError as resume_error:
                    errors.append(f'resume killed tracee {waited}: {resume_error}')
                    tasks[waited] = task
            elif task:
                if waited == root_pid:
                    step_wait = {'kind': 'exit' if os.WIFEXITED(status) else 'signal',
                                 'status': os.waitstatus_to_exitcode(status),
                                 'raw_wait_status': status, 'role': 'step-shell',
                                 'pid': waited, 'identity': task.get('identity')}
                    try:
                        persist_owner_wait(destination, Path(args.step_exit), step_wait,
                                           args.step_wait, cleanup_deadline)
                    except OSError as write_error:
                        errors.append(f'persist step wait during failure cleanup: {write_error}')
                if task.get('role') == 'executable-owner':
                    owner_wait = {'kind': 'exit' if os.WIFEXITED(status) else 'signal',
                                  'status': os.waitstatus_to_exitcode(status),
                                  'raw_wait_status': status, 'role': 'executable-owner',
                                  'pid': waited, 'identity': task.get('identity')}
                    try:
                        persist_owner_wait(destination, Path(args.owner_exit), owner_wait,
                                           args.owner_wait, cleanup_deadline)
                    except OSError as write_error:
                        errors.append(f'persist owner wait during failure cleanup: {write_error}')
                if task.get('pidfd') is not None:
                    os.close(task['pidfd'])
        if tasks:
            errors.append(f'{len(tasks)} ptrace-owned task(s) remained at the common cleanup deadline')
    finally:
        tracer_self_after = resource.getrusage(resource.RUSAGE_SELF)
        tracer_children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        files = {}
        if tasks:
            errors.append('buffer finalization refused because owned writers remain unreaped')
        elif time.monotonic() >= cleanup_deadline:
            errors.append('buffer finalization refused at common deadline')
        else:
            phase_callback = getattr(args, 'lifecycle_callback', None)
            if phase_callback:
                try:
                    phase_callback('FINALIZE', 'all writers retired; bounded buffer drain begins')
                except BaseException as error:
                    errors.append(f'lifecycle FINALIZE transition refused: {error}')
            try:
                capture_present_targets(owner_root, owner_fd, held, cleanup_deadline)
            except (OSError, TraceFailure, TimeoutError) as error:
                errors.append(f'capture still-present owner buffers: {error}')
            for relative, record in held.items():
                try:
                    target = destination / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    files[relative] = {
                        **copy_fd(record['fd'], target, cleanup_deadline),
                        'dev': record['dev'], 'ino': record['ino'],
                    }
                except (OSError, TimeoutError) as error:
                    errors.append(f'finalize {relative}: {error}')
        for record in held.values():
            try:
                os.close(record['fd'])
            except OSError as error:
                errors.append(f'close retained buffer descriptor: {error}')
        if step_wait is None:
            unavailable_step_wait = {
                'status': None, 'reason': 'step-shell wait status unavailable',
                'trace_error': trace_error,
            }
            try:
                write_result(destination / 'step-shell.wait.json', unavailable_step_wait)
                write_result(Path(args.step_wait), unavailable_step_wait)
                temporary = Path(args.step_exit).with_suffix('.exit.tmp')
                temporary.write_text('unavailable\n')
                os.replace(temporary, Path(args.step_exit))
            except OSError as error:
                errors.append(f'persist unavailable step-shell wait: {error}')
        if owner_wait is None:
            unavailable_wait = {
                'status': None, 'reason': 'executable-owner wait status unavailable',
                'trace_error': trace_error,
            }
            try:
                write_result(destination / 'owner-step.wait.json', unavailable_wait)
                write_result(Path(args.owner_wait), unavailable_wait)
                temporary = Path(args.owner_exit).with_suffix('.exit.tmp')
                temporary.write_text('unavailable\n')
                os.replace(temporary, Path(args.owner_exit))
            except OSError as error:
                errors.append(f'persist unavailable executable-owner wait: {error}')
        finalization_deadline_met = time.monotonic() < cleanup_deadline
        summary = {
            'owner_tmp': owner_root,
            'step_wait': step_wait,
            'owner_wait': owner_wait,
            'sampler_wait': sampler_wait,
            'terminal_waits': list(wait_owner.terminal_history),
            'owner_exec': owner_exec,
            'tracee_setup_wait': step_wait if not exec_seen else None,
            'trace_error': trace_error,
            'finalization_deadline_met': finalization_deadline_met,
            'unretired_tasks': [
                {'pid': pid, 'role': task.get('role'), 'identity': task.get('identity'),
                 'tgid': task.get('tgid')}
                for pid, task in tasks.items()
            ],
            'finalized': (not errors and not tasks and finalization_deadline_met and
                          step_wait is not None and owner_wait is not None and owner_exec is not None),
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
            write_result(destination / 'capture-summary.json', summary, cleanup_deadline)
        except OSError as error:
            print(f'cannot write capture summary: {error}', file=sys.stderr)
            errors.append(f'write capture summary: {error}')
            summary['finalized'] = False
            summary['finalization_deadline_met'] = False
        for task in tasks.values():
            if task.get('pidfd') is not None:
                # Keep handles open until the owning controller exits and PTRACE_O_EXITKILL fires.
                continue
        os.close(owner_fd)
        failure_injection = None

    if errors or owner_wait is None:
        status = 125
    elif owner_wait['status'] < 0:
        status = 128 + abs(owner_wait['status'])
    else:
        status = owner_wait['status']
    return status, summary
