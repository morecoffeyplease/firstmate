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
        repos = {}
        registry = home / "data" / "projects.md"
        for line in registry.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "-":
                name = fields[1]
                path = home / "projects" / name
                value = subprocess.run(["git", "-C", str(path), "config", "--get", "remote.origin.url"], capture_output=True, text=True, timeout=3)
                if value.returncode == 0:
                    repos[value.stdout.strip().removesuffix(".git")] = name
        return repos.get(origin.removesuffix(".git"))
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
    def forward(signum: int, _frame: object) -> None:
        received.append(signum)
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except OSError:
                pass

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
            received.append(signal.SIGINT)
            # TTY-generated SIGINT reaches the whole foreground process group,
            # including the child. A user-originated signal to this wrapper only
            # must be forwarded so the child receives the same cancellation.
            if info.si_pid and child is not None and child.poll() is None:
                try:
                    child.send_signal(signal.SIGINT)
                except OSError:
                    pass

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
    try:
        child = subprocess.Popen(command, preexec_fn=child_unblock_sigint if sigint_mask is not None else None)
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
        sigint_stop.set()
        if sigint_thread is not None:
            sigint_thread.join(timeout=1)
        if sigint_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, sigint_mask)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
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
