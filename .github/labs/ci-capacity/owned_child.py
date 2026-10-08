import os
import signal
from pathlib import Path


def process_identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return fields[19]


class OwnedChild:
    def __init__(self, process, pidfd, identity, identity_reader=process_identity,
                 signal_sender=None):
        self.process = process
        self.pidfd = pidfd
        self.identity = identity
        self.identity_reader = identity_reader
        self.signal_sender = signal_sender or getattr(signal, 'pidfd_send_signal', None)

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

    def send(self, signum):
        if self.process.poll() is not None:
            return False
        try:
            current = self.identity_reader(self.process.pid)
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
