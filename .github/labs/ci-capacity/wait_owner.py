#!/usr/bin/env python3
"""Route every direct-child and ptrace wait through one durable role ledger."""

import json
import os
import signal
import time
from pathlib import Path


WAIT_ALL = 0x40000000


class WaitOwner:
    def __init__(self, journal, controller_pid=None):
        self.journal = Path(journal)
        self.controller_pid = controller_pid or os.getpid()
        self.roles = {}
        self.terminal = {}
        self.terminal_history = []

    def register(self, pid, role, identity=None, pidfd=None, tgid=None):
        self.roles[pid] = {
            'role': role,
            'identity': identity,
            'pidfd': pidfd,
            'tgid': tgid or pid,
            'trace_owned': role in ('step-shell', 'executable-owner',
                                    'tracee-descendant', 'tracee', 'pending-tracee',
                                    'resource-sampler'),
        }

    def role(self, pid):
        return self.roles.get(pid)

    def remap(self, old_pid, new_pid, record):
        staged = dict(record)
        staged['trace_owned'] = True
        self.roles[new_pid] = staged
        if old_pid != new_pid:
            self.roles.pop(old_pid, None)

    def _append(self, pid, status, record, source):
        if os.WIFEXITED(status):
            kind = 'exit'
            value = os.WEXITSTATUS(status)
            terminal = True
        elif os.WIFSIGNALED(status):
            kind = 'signal'
            value = os.WTERMSIG(status)
            terminal = True
        elif os.WIFSTOPPED(status):
            kind = 'stop'
            value = os.WSTOPSIG(status)
            terminal = False
        else:
            kind = 'other'
            value = status
            terminal = False
        entry = {
            'observed_monotonic': time.monotonic(),
            'pid': pid,
            'role': record.get('role', 'unclassified'),
            'identity': record.get('identity'),
            'tgid': record.get('tgid'),
            'kind': kind,
            'value': value,
            'raw_wait_status': status,
            'source': source,
        }
        descriptor = os.open(self.journal, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            payload = (json.dumps(entry, sort_keys=True) + '\n').encode()
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError('short write to wait journal')
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if terminal:
            self.terminal[pid] = entry
            self.terminal_history.append(entry)
        return entry

    def wait_any(self):
        try:
            pid, status = os.waitpid(-1, WAIT_ALL | os.WNOHANG)
        except InterruptedError:
            return None
        if pid == 0:
            return None
        record = self.roles.get(pid)
        if record is None and os.WIFSTOPPED(status):
            try:
                status_lines = Path(f'/proc/{pid}/status').read_text().splitlines()
                fields = {line.split(':', 1)[0]: line.split(':', 1)[1].strip()
                          for line in status_lines if ':' in line}
                if int(fields.get('TracerPid', '0')) == self.controller_pid:
                    stat_fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                    record = {'role': 'pending-tracee', 'identity': stat_fields[19],
                              'pidfd': None, 'tgid': int(fields.get('Tgid', pid))}
                    self.roles[pid] = record
            except (OSError, IndexError, ValueError):
                record = None
        if record is None:
            # A wait has already consumed this kernel status. Preserve it before
            # refusing the run so an ownership bug cannot erase the raw result.
            record = {'role': 'unclassified', 'identity': None, 'pidfd': None,
                      'tgid': None, 'trace_owned': False}
            entry = self._append(pid, status, record, 'waitpid-any-unclassified')
            raise RuntimeError(f'unowned child wait retained: {entry}')
        entry = self._append(pid, status, record, 'waitpid-any')
        return pid, status, record, entry

    def wait_specific_blocking(self, pid, deadline, source='waitpid-specific'):
        while time.monotonic() < deadline:
            event = self.wait_specific(pid, source=source)
            if event is not None:
                return event
            time.sleep(min(0.01, max(deadline - time.monotonic(), 0)))
        return None

    def wait_specific(self, pid, source='waitpid-specific', options=os.WNOHANG):
        record = self.roles.get(pid, {'role': 'unclassified', 'identity': None,
                                      'pidfd': None, 'tgid': pid})
        try:
            waited, status = os.waitpid(pid, options | WAIT_ALL)
        except InterruptedError:
            return None
        if waited == 0:
            return None
        entry = self._append(waited, status, record, source)
        return waited, status, record, entry

    def signal(self, pid, signum):
        if pid in self.terminal:
            return {'pid': pid, 'signum': signum, 'method': 'already-terminal',
                    'delivered': False}
        record = self.roles.get(pid)
        if record is None:
            raise RuntimeError(f'cannot signal unregistered child {pid}')
        pidfd = record.get('pidfd')
        if pidfd is not None and hasattr(signal, 'pidfd_send_signal'):
            signal.pidfd_send_signal(pidfd, signum)
            return {'pid': pid, 'signum': signum, 'method': 'pidfd'}
        identity = record.get('identity')
        if identity is None and record.get('trace_owned') and pid not in self.terminal:
            os.kill(pid, signum)
            return {'pid': pid, 'signum': signum,
                    'method': 'unreaped-ptrace-child'}
        if identity is None:
            raise RuntimeError(f'cannot identity-qualify signal to child {pid}')
        stat_fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if stat_fields[19] != identity:
            raise RuntimeError(f'child identity changed before signal: pid={pid}')
        os.kill(pid, signum)
        return {'pid': pid, 'signum': signum, 'method': 'verified-child-pid'}

    def already_reaped(self, pid):
        return self.terminal.get(pid)
