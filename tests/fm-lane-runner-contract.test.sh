#!/usr/bin/env bash
# Focused behavioral contract for task-bound validation receipts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=tests/git-config-helpers.sh
. "$ROOT/tests/git-config-helpers.sh"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/fm-lane-runner-contract.XXXXXX")
if [ "${FM_LANE_TEST_KEEP:-0}" = 1 ]; then
  printf 'fixture root: %s\n' "$TMP_ROOT"
else
  trap 'rm -rf "$TMP_ROOT"' EXIT
fi
FM_TEST_ROOT="$ROOT" FM_TEST_TMP="$TMP_ROOT" python3 - <<'PY'
import json
import os
import pathlib
import pty
import select
import signal
import subprocess
import sys
import time

root = pathlib.Path(os.environ["FM_TEST_ROOT"])
tmp = pathlib.Path(os.environ["FM_TEST_TMP"])
home = tmp / "home"
task = "task-receipts"
receipt_dir = home / "data" / task / "lane-receipts"
repo = home / "projects" / "widget"
receipt_dir.mkdir(parents=True)
repo.mkdir(parents=True)
(home / "data" / "projects.md").write_text("- widget [direct-PR] - fixture\n")
subprocess.run(["git", "init", "-q", str(repo)], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/widget.git"], check=True)
(repo / "tracked").write_text("fixture\n")
subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
(home / "config").mkdir()
(home / "config" / "project-lanes.json").write_text(json.dumps({"widget": {"full": ["/bin/echo", "full"]}}))

env = {**os.environ, "FM_HOME": str(home), "FM_TASK_ID": task, "FM_TASK_GENERATION": "g-1", "FM_LANE_RECEIPTS": str(receipt_dir), "FM_PROJECT": "widget"}
wrapper = root / "bin" / "fm-lane-run.sh"
child = tmp / "child.py"
child.write_text("import json,os,sys\nprint(json.dumps([os.getcwd(),os.environ.get('KEEP_ME'),sys.argv[1:],sys.stdin.read()]))\nprint('stderr-line',file=sys.stderr)\n")
run = subprocess.run([str(wrapper), "focused", "--", sys.executable, str(child), "space arg"], cwd=repo, env={**env, "KEEP_ME": "yes"}, input="stdin-data", text=True, capture_output=True)
assert run.returncode == 0 and run.stderr == "stderr-line\n", (run.returncode, run.stdout, run.stderr)
assert json.loads(run.stdout) == [str(repo.resolve()), "yes", ["space arg"], "stdin-data"], run.stdout
receipt = next(receipt_dir.glob("focused-*.json"))
value = json.loads(receipt.read_text())
assert value["phase"] == "finish" and value["task"] == task and value["generation"] == "g-1"
assert value["repository"].endswith("acme/widget.git") and value["head_before"] == value["head_after"]
assert value["dirty_before"] is False and value["dirty_after"] is False and value["exit_code"] == 0
full = subprocess.run([str(wrapper), "full"], cwd=repo, env=env, text=True, capture_output=True, check=True)
full_value = next(json.loads(path.read_text()) for path in receipt_dir.glob("full-*.json") if json.loads(path.read_text())["phase"] == "finish")
assert full.stdout == "full\n" and full_value["argv"] == ["/bin/echo", "full"]

# A signal received by the wrapper is recorded even when its child handles it
# and exits successfully; it does not rewrite the child's exact exit result.
signal_child = tmp / "signal-child.py"
signal_child.write_text("import signal,time\nsignal.signal(signal.SIGTERM, lambda *_: exit(0))\nprint('ready',flush=True)\ntime.sleep(10)\n")
proc = subprocess.Popen([str(wrapper), "focused", "--", sys.executable, str(signal_child)], cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
assert proc.stdout.readline().strip() == "ready"
proc.send_signal(signal.SIGTERM)
stdout, stderr = proc.communicate(timeout=5)
assert proc.returncode == 0, (proc.returncode, stdout, stderr)
signal_receipt = max((json.loads(path.read_text()) for path in receipt_dir.glob("focused-*.json")), key=lambda item: item["started_order_ns"])
assert signal_receipt["received_signal"] == signal.SIGTERM and signal_receipt["exit_code"] == 0 and signal_receipt["signal"] is None

# A direct SIGINT to the wrapper is forwarded to the child and keeps signal
# termination semantics in the caller.
interrupt_child = tmp / "interrupt-child.py"
interrupt_child.write_text("import time\nprint('ready',flush=True)\ntime.sleep(10)\n")
proc = subprocess.Popen([str(wrapper), "focused", "--", sys.executable, str(interrupt_child)], cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
assert proc.stdout.readline().strip() == "ready"
proc.send_signal(signal.SIGINT)
stdout, stderr = proc.communicate(timeout=5)
assert proc.returncode == -signal.SIGINT, (proc.returncode, stdout, stderr)
interrupt_receipt = max((json.loads(path.read_text()) for path in receipt_dir.glob("focused-*.json")), key=lambda item: item["started_order_ns"])
assert interrupt_receipt["received_signal"] == signal.SIGINT and interrupt_receipt["signal"] == signal.SIGINT

# A terminal Ctrl-C reaches the child through the foreground process group and
# is not forwarded a second time by the wrapper.
tty_child = tmp / "tty-child.py"
tty_child.write_text("import signal,time\ndef stop(*_):\n print('handled',flush=True)\n raise SystemExit(0)\nsignal.signal(signal.SIGINT,stop)\nprint('ready',flush=True)\ntime.sleep(10)\n")
pid, master = pty.fork()
if pid == 0:
    os.chdir(repo)
    os.execvpe(str(wrapper), [str(wrapper), "focused", "--", sys.executable, str(tty_child)], env)
pty_output = b""
deadline = time.monotonic() + 5
while b"ready" not in pty_output and time.monotonic() < deadline:
    ready, _, _ = select.select([master], [], [], 0.2)
    if ready:
        pty_output += os.read(master, 4096)
assert b"ready" in pty_output, pty_output
os.write(master, b"\x03")
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    ended, status = os.waitpid(pid, os.WNOHANG)
    if ended == pid:
        break
    ready, _, _ = select.select([master], [], [], 0.2)
    if ready:
        try:
            pty_output += os.read(master, 4096)
        except OSError:
            pass
else:
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    raise AssertionError("PTY lane wrapper did not exit after Ctrl-C")
assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (status, pty_output)
assert b"handled" in pty_output, pty_output
tty_receipt = max((json.loads(path.read_text()) for path in receipt_dir.glob("focused-*.json")), key=lambda item: item["started_order_ns"])
assert tty_receipt["received_signal"] == signal.SIGINT and tty_receipt["exit_code"] == 0 and tty_receipt["signal"] is None

# Invalid receipt ownership must not prevent the wrapped command from running.
result = subprocess.run([str(wrapper), "focused", "--", "/bin/sh", "-c", "exit 23"], cwd=repo, env={**env, "FM_LANE_RECEIPTS": str(tmp / "foreign" / "lane-receipts")}, capture_output=True)
assert result.returncode == 23, result.returncode

# A missing receipt destination also leaves the caller's result untouched.
result = subprocess.run([str(wrapper), "focused", "--", "/bin/sh", "-c", "exit 0"], cwd=repo, env={**env, "FM_LANE_RECEIPTS": str(receipt_dir / "missing" / "lane-receipts")}, capture_output=True)
assert result.returncode == 0, result.returncode

print("pass: lane receipt identity, process transparency, signal classification, and best-effort receipt failure")
PY
