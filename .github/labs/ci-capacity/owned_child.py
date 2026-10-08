import os
import select
import signal
from pathlib import Path


def process_identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return fields[19]


class OwnedChild:
    def __init__(self, process, pidfd, identity, identity_reader=process_identity,
                 signal_sender=None):
        self.process = process
        self.pid = process.pid if process is not None else None
        self.pidfd = pidfd
        self.identity = identity
        self.identity_reader = identity_reader
        self.signal_sender = signal_sender or getattr(signal, 'pidfd_send_signal', None)
        self.raw_wait_status = None

    @classmethod
    def bind(cls, process):
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise RuntimeError('pidfd operations are required for owned child control')
        identity = process_identity(process.pid)
        pidfd = os.pidfd_open(process.pid, 0)
        if process_identity(process.pid) != identity:
            os.close(pidfd)
            raise RuntimeError('child identity changed while binding its pidfd')
        return cls(process, pidfd, identity)

    @classmethod
    def bind_pid(cls, pid):
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise RuntimeError('pidfd operations are required for owned child control')
        identity = process_identity(pid)
        pidfd = os.pidfd_open(pid, 0)
        if process_identity(pid) != identity:
            os.close(pidfd)
            raise RuntimeError('child identity changed while binding its pidfd')
        owner = cls(None, pidfd, identity)
        owner.pid = pid
        return owner

    def record_wait(self, raw_status):
        self.raw_wait_status = raw_status

    def exited(self):
        if self.raw_wait_status is not None:
            return True
        if self.pidfd is None:
            return False
        poller = select.poll()
        poller.register(self.pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        return bool(poller.poll(0))

    def send(self, signum):
        if self.raw_wait_status is not None:
            return False
        try:
            current = self.identity_reader(self.pid)
        except (OSError, IndexError):
            return False
        if current != self.identity:
            return False
        if self.pidfd is None or self.signal_sender is None:
            return False
        try:
            self.signal_sender(self.pidfd, signum)
            return True
        except ProcessLookupError:
            return False

    def close(self):
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None
