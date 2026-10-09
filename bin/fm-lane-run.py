#!/usr/bin/env python3
"""Run one configured local validation lane and write durable receipts."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fm_project_lanes import read_project_lanes
from fm_lane_receipts import repository_identity, valid_receipt
SCHEMA = "fm-lane-receipt.v1"
LANES = {"focused", "full", "verify"}
MAX_RECEIPTS_PER_LANE = 20


def now() -> int:
    return int(time.time())


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def source_identity() -> tuple[str | None, bool | None, str | None]:
    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=all")
    dirty_hash = None if status is None else hashlib.sha256(status.encode()).hexdigest()
    dirty = None if status is None else bool(status)
    return head, dirty, dirty_hash


def process_identity(pid: int) -> str | None:
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return result.stdout.strip() or None if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def project_for_home(home: Path) -> str | None:
    try:
        origin = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        expected = repository_identity(origin)
        if not expected or not _origin_is_registered_safe(origin):
            return None
        repos = {}
        registry = home / "data" / "projects.md"
        for line in registry.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "-":
                name = fields[1]
                path = home / "projects" / name
                value = subprocess.run(["git", "-C", str(path), "config", "--get", "remote.origin.url"], capture_output=True, text=True, timeout=3)
                if value.returncode == 0 and _origin_is_registered_safe(value.stdout.strip()):
                    identity = repository_identity(value.stdout.strip())
                    if identity:
                        repos[identity.lower()] = name
        return repos.get(expected.lower())
    except (OSError, subprocess.SubprocessError):
        return None


def _origin_is_registered_safe(origin: str) -> bool:
    try:
        result = subprocess.run(["/bin/bash", "-c", 'source "$1"; fm_project_origin_safe "$2"',
                                 "fm-origin", str(ROOT / "bin" / "fm-project-origin-lib.sh"), origin],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def registered_repository_identity(home: Path | None, project: str | None) -> str | None:
    if home is None or project is None or not project or "/" in project or project in (".", ".."):
        return None
    try:
        origin = subprocess.run(["git", "-C", str(home / "projects" / project), "config", "--get", "remote.origin.url"],
                                capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        if not _origin_is_registered_safe(origin):
            return None
        return repository_identity(origin)
    except (OSError, subprocess.SubprocessError):
        return None


def load_commands(home: Path, project: str) -> dict[str, list[str]]:
    return read_project_lanes(home).get(project, {})


def bound_home(receipt_dir: str, task: str) -> Path | None:
    """Resolve the operational home only from the launch's validated receipt binding."""
    if not receipt_dir or not task or "/" in task or task in (".", ".."):
        return None
    try:
        resolved = Path(receipt_dir).resolve(strict=False)
        if resolved.name != "lane-receipts" or resolved.parent.name != task or resolved.parent.parent.name != "data":
            return None
        home = resolved.parent.parent.parent
        if (home / "data" / task / "lane-receipts").resolve(strict=False) != resolved:
            return None
        if (home / "data" / task).is_symlink() or resolved.is_symlink():
            return None
        return home
    except (OSError, RuntimeError):
        return None


def write_error(directory: Path, task: str, generation: str, lane: str, message: str) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ".errors"
        safe_message = " ".join(message.split())[:300]
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{now()} task={task} generation={generation} lane={lane} {safe_message}\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in LANES:
        print("usage: fm-lane-run.sh focused -- <command> [args...] | full | verify", file=sys.stderr)
        return 2
    lane = argv.pop(0)
    artifact = None
    if argv[:1] == ["--artifact"]:
        if len(argv) < 2:
            print("error: --artifact needs a command-owned path", file=sys.stderr)
            return 2
        artifact, argv = argv[1], argv[2:]
    receipt_dir_raw = os.environ.get("FM_LANE_RECEIPTS", "")
    task = os.environ.get("FM_TASK_ID", "")
    generation = os.environ.get("FM_TASK_GENERATION", "")
    home = bound_home(receipt_dir_raw, task)
    if lane == "focused":
        if argv[:1] == ["--"]:
            argv = argv[1:]
        command = argv
    else:
        if argv:
            print("error: full and verify commands come from config/project-lanes.json", file=sys.stderr)
            return 2
        project = project_for_home(home) if home else None
        if not project:
            print("error: cannot match this repository to a registered project", file=sys.stderr)
            return 2
        try:
            command = load_commands(home, project).get(lane, [])
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if not command:
        print(f"error: {lane} lane has no configured command", file=sys.stderr)
        return 2
    # Missing launch binding is not an error: run the command unchanged and
    # leave the lane visibly not instrumented.
    receipt_dir = Path(receipt_dir_raw) if receipt_dir_raw else None
    receipt: dict = {}
    receipt_path = None
    start_head, start_dirty, start_dirty_hash = source_identity()
    start_epoch = now()
    start_order_ns = time.time_ns()
    identity = process_identity(os.getpid())
    receipt_id = f"{start_epoch}-{secrets.token_hex(8)}"
    project = project_for_home(home) if home else None
    expected_repository = registered_repository_identity(home, project)
    validated_receipt_dir: Path | None = None
    if receipt_dir and task and generation:
        try:
            # Reject paths outside this task's durable data directory.
            expected = (home / "data" / task / "lane-receipts").resolve() if home else None
            if expected is None or receipt_dir.resolve() != expected or (home / "data" / task).is_symlink() or receipt_dir.is_symlink():
                raise OSError("receipt directory does not match task home")
            validated_receipt_dir = expected
            receipt_dir.mkdir(parents=True, exist_ok=True)
            receipt_path = receipt_dir / f"{lane}-{receipt_id}.json"
            receipt = {"schema": SCHEMA, "phase": "start", "id": receipt_id, "lane": lane, "argv": command, "task": task, "generation": generation, "project": project, "repository": git("remote", "get-url", "origin"), "head_before": start_head, "dirty_before": start_dirty, "dirty_source_hash_before": start_dirty_hash, "started_epoch": start_epoch, "started_order_ns": start_order_ns, "os": os.uname().sysname + " " + os.uname().release + " " + os.uname().machine, "runtime": "python " + sys.version.split()[0], "pid": os.getpid(), "process_start": identity, "artifact": artifact, "log": "not retained" if artifact is None else artifact}
            if not valid_receipt(receipt, receipt_path, lane, task, generation, project or "", expected_repository):
                write_error(receipt_dir, task, generation, lane, "start receipt did not satisfy the shared schema")
            atomic_json(receipt_path, receipt)
        except OSError as exc:
            if validated_receipt_dir:
                write_error(receipt_dir, task, generation, lane, str(exc))
            receipt_path = None
    child = None
    received: list[int] = []
    sigint_stop = threading.Event()
    sigint_thread: threading.Thread | None = None
    sigint_mask = None
    sigint_ignored = signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    tty_attached = any(os.isatty(fd) for fd in (0, 1, 2))
    tty_foreground_fd = None
    if tty_attached and os.name == "posix":
        for descriptor in (0, 1, 2):
            if not os.isatty(descriptor):
                continue
            try:
                if os.tcgetpgrp(descriptor) == os.getpgrp():
                    tty_foreground_fd = descriptor
                    break
            except OSError:
                continue
    child_group_isolated = not tty_attached and os.name == "posix"
    signal_codes = {b"I": signal.SIGINT, b"T": signal.SIGTERM, b"H": signal.SIGHUP}
    witness_signal_read = witness_stop_write = None
    signal_witness = None
    witness_thread: threading.Thread | None = None
    witness_ready_read = None

    def record_received(signum: int) -> None:
        if signum not in received:
            received.append(signum)

    def read_witness_signals() -> None:
        while witness_signal_read is not None:
            try:
                code = os.read(witness_signal_read, 1)
            except OSError:
                return
            if not code:
                return
            signum = signal_codes.get(code)
            if signum is not None:
                record_received(signum)

    def deliver_to_child(signum: int) -> None:
        if child is None or child.poll() is not None:
            return
        try:
            if child_group_isolated:
                os.killpg(child.pid, signum)
            else:
                child.send_signal(signum)
        except OSError:
            pass

    def forward(signum: int, _frame: object) -> None:
        record_received(signum)
        if tty_attached and not child_group_isolated and signum == signal.SIGINT:
            # A terminal signal already reached the command in the shared
            # process group, so do not deliver it twice.
            return
        deliver_to_child(signum)

    def child_unblock_sigint() -> None:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})

    def wait_for_sigint() -> None:
        while not sigint_stop.is_set():
            try:
                info = signal.sigtimedwait({signal.SIGINT}, 0.1)
            except InterruptedError:
                continue
            if info is None:
                continue
            record_received(signal.SIGINT)
            # With an attached TTY, terminal SIGINT reaches the shared process
            # group directly. Without a TTY, the child owns a new process group
            # and this wrapper forwards to that entire group. No sender-PID
            # inference is needed.
            if child_group_isolated:
                deliver_to_child(signal.SIGINT)

    previous_handlers = {}
    if not sigint_ignored and hasattr(signal, "pthread_sigmask") and hasattr(signal, "sigtimedwait"):
        sigint_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    signals_to_handle = (signal.SIGTERM, signal.SIGHUP) if sigint_mask is not None or sigint_ignored else (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    for sig in signals_to_handle:
        inherited = signal.getsignal(sig)
        if inherited == signal.SIG_IGN:
            continue
        previous_handlers[sig] = signal.signal(sig, forward)
    return_code = 127
    child_signal = None
    tty_original_group = None
    tty_handoff_active = False
    witness_ready = False
    gate_read = gate_write = None
    try:
        if tty_foreground_fd is not None:
            gate_read, gate_write = os.pipe()
            helper = Path(__file__).with_name("fm-lane-exec.py")
            child = subprocess.Popen(
                [sys.executable, str(helper), str(gate_read), str(os.getpgrp()),
                 "unblock" if sigint_mask is not None else "keep", *command],
                pass_fds=(gate_read,), preexec_fn=os.setpgrp,
            )
            os.close(gate_read)
            gate_read = None
            child_group_isolated = True
            witness_signal_read, signal_write = os.pipe()
            stop_read, witness_stop_write = os.pipe()
            witness_ready_read, ready_write = os.pipe()
            witness_path = Path(__file__).with_name("fm-lane-signal-witness.py")
            try:
                try:
                    signal_witness = subprocess.Popen(
                        [sys.executable, str(witness_path), str(signal_write), str(stop_read), str(ready_write)],
                        pass_fds=(signal_write, stop_read, ready_write),
                        preexec_fn=lambda: os.setpgid(0, child.pid),
                    )
                except OSError:
                    signal_witness = None
            finally:
                os.close(signal_write)
                os.close(stop_read)
                os.close(ready_write)
            if signal_witness is not None:
                if os.read(witness_ready_read, 1) == b"R":
                    witness_ready = True
                    witness_thread = threading.Thread(target=read_witness_signals, name="fm-lane-signal-witness", daemon=True)
                    witness_thread.start()
                else:
                    signal_witness.wait()
                    signal_witness = None
                    os.close(witness_signal_read)
                    os.close(witness_stop_write)
                    witness_signal_read = witness_stop_write = None
            os.close(witness_ready_read)
            witness_ready_read = None
            tty_original_group = os.getpgrp()
            previous_ttou = signal.getsignal(signal.SIGTTOU)
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            try:
                if witness_ready:
                    os.tcsetpgrp(tty_foreground_fd, child.pid)
                    tty_handoff_active = True
                    child_group_isolated = True
                else:
                    child_group_isolated = False
            except OSError:
                tty_handoff_active = False
                child_group_isolated = False
                if witness_stop_write is not None:
                    try:
                        os.write(witness_stop_write, b"S")
                    except OSError:
                        pass
                    os.close(witness_stop_write)
                    witness_stop_write = None
                if signal_witness is not None:
                    signal_witness.wait()
                    signal_witness = None
            finally:
                signal.signal(signal.SIGTTOU, previous_ttou)
            os.write(gate_write, b"I" if tty_handoff_active else b"S")
            os.close(gate_write)
            gate_write = None
        else:
            child = subprocess.Popen(
                command,
                preexec_fn=child_unblock_sigint if sigint_mask is not None else None,
                start_new_session=not tty_attached and os.name == "posix",
            )
        if sigint_mask is not None:
            sigint_thread = threading.Thread(target=wait_for_sigint, name="fm-lane-sigint", daemon=True)
            sigint_thread.start()
        return_code = child.wait()
        if return_code < 0:
            child_signal = -return_code
            return_code = 128 + child_signal
    except OSError as exc:
        print(f"fm-lane-run: {exc}", file=sys.stderr)
        return_code = 127
    finally:
        if witness_stop_write is not None:
            try:
                os.write(witness_stop_write, b"S")
            except OSError:
                pass
            os.close(witness_stop_write)
            witness_stop_write = None
        if signal_witness is not None:
            signal_witness.wait()
            signal_witness = None
        if witness_signal_read is not None:
            os.close(witness_signal_read)
            witness_signal_read = None
        if witness_thread is not None:
            witness_thread.join(timeout=1)
        if witness_ready_read is not None:
            os.close(witness_ready_read)
        if gate_read is not None:
            os.close(gate_read)
        if gate_write is not None:
            os.close(gate_write)
        sigint_stop.set()
        if sigint_thread is not None:
            sigint_thread.join(timeout=1)
        if sigint_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, sigint_mask)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        if tty_handoff_active and tty_foreground_fd is not None and tty_original_group is not None:
            previous_ttou = signal.getsignal(signal.SIGTTOU)
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            try:
                os.tcsetpgrp(tty_foreground_fd, tty_original_group)
            except OSError:
                pass
            finally:
                signal.signal(signal.SIGTTOU, previous_ttou)
    end_head, end_dirty, end_dirty_hash = source_identity()
    finish = {"phase": "finish", "ended_epoch": now(), "exit_code": return_code if child_signal is None else None, "signal": child_signal, "received_signal": received[0] if received else None, "head_after": end_head, "dirty_after": end_dirty, "dirty_source_hash_after": end_dirty_hash}
    if artifact:
        path = Path(artifact)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            finish["artifact_record"] = {"path": artifact, "size": path.stat().st_size, "sha256": digest}
        except OSError:
            finish["artifact_record"] = {"path": artifact, "missing": True}
    if receipt_path:
        try:
            receipt.update(finish)
            if not valid_receipt(receipt, receipt_path, lane, task, generation, project or "", expected_repository):
                write_error(validated_receipt_dir or receipt_dir, task, generation, lane, "finish receipt did not satisfy the shared schema")
            atomic_json(receipt_path, receipt)
            completed = []
            for candidate in receipt_dir.glob(f"{lane}-*.json"):
                try:
                    value = json.loads(candidate.read_text(encoding="utf-8"))
                    if value.get("schema") == SCHEMA and value.get("lane") == lane and value.get("phase") == "finish":
                        completed.append((int(value.get("ended_epoch", 0)), candidate))
                except (OSError, ValueError):
                    continue
            completed.sort()
            for _ended, candidate in completed[:-MAX_RECEIPTS_PER_LANE]:
                candidate.unlink(missing_ok=True)
        except OSError as exc:
            write_error(validated_receipt_dir or receipt_dir, task, generation, lane, str(exc))
    if child_signal is not None:
        signal.signal(child_signal, signal.SIG_DFL)
        os.kill(os.getpid(), child_signal)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
