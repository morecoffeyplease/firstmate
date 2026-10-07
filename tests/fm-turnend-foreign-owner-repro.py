#!/usr/bin/env python3
"""Executable regression for the foreign session-lock owner turn-end loop.

This is adapted from Appendix A of the downstream reproduction report. It
runs the shipped lock, Claude auto-arm, and turn-end guard scripts against
isolated synthetic primary homes and harness-shaped processes.
"""
import json
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
LAB = pathlib.Path(tempfile.mkdtemp(prefix="fm-turnend-foreign-owner-"))
OUT = LAB / "evidence"
OUT.mkdir()
# Basename must be an exact FM_HARNESS_NAMES entry. Linux procps comm= is the
# 15-char kernel name, so "synthetic-claude" becomes "synthetic-claud" and
# never matches the claude regex, so fm-lock.sh exits without writing .lock.
FAKE = LAB / "claude"
FAKE.symlink_to("/bin/bash")
PROCS = []

BASE_ENV = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith(("FM_", "HERDR_", "PI_", "CLAUDE_PROJECT_DIR", "GROK_", "CURSOR_"))
}


def make(name):
    root = LAB / name
    root.mkdir()
    for directory in ("state", "config", "data", "projects"):
        (root / directory).mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=BASE_ENV)
    (root / "AGENTS.md").write_text("Synthetic diagnostic fixture. No fleet or project operations.\n")
    (root / "bin").symlink_to(REPO / "bin", target_is_directory=True)
    (root / "state/task.meta").write_text("project=synthetic\n")
    (root / "state/home-summary.json").write_text("{}\n")
    env = BASE_ENV | {
        "FM_HOME": str(root),
        "FM_ROOT_OVERRIDE": str(root),
        "FM_STATE_OVERRIDE": str(root / "state"),
        "FM_CONFIG_OVERRIDE": str(root / "config"),
        "FM_DATA_OVERRIDE": str(root / "data"),
        "FM_PROJECTS_OVERRIDE": str(root / "projects"),
        "FM_POLL": "1",
        "FM_HEARTBEAT": "999999",
        "FM_HOME_SUMMARY_INTERVAL": "999999",
        "FM_CHECK_INTERVAL": "999999",
        "FM_CLAUDE_AUTOARM_SYNC_WAIT_MS": "0",
    }
    return root, env


def run(env, command):
    return subprocess.run(
        [str(FAKE), "-c", command],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def start(env, command, name):
    output = (OUT / name).open("w")
    process = subprocess.Popen(
        [str(FAKE), "-c", command],
        env=env,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    output.close()
    PROCS.append(process)
    return process


def until(test, seconds=20, message="condition timed out"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(0.1)
    raise RuntimeError(message() if callable(message) else message)


def session_lock_text(path):
    try:
        if path.is_symlink() or not path.is_file():
            return None
        text = path.read_text().strip()
    except OSError:
        return None
    return text if text.isdigit() else None


PAYLOAD = json.dumps({"session_id": "synthetic-second", "stop_hook_active": True})


def guard(env, label):
    process = run(
        env,
        "printf '%s\\n' '" + PAYLOAD + "' | \"$FM_ROOT_OVERRIDE/bin/fm-turnend-guard.sh\" --claude",
    )
    print(label, "rc=" + str(process.returncode), "stdout=" + repr(process.stdout), "stderr=" + repr(process.stderr), flush=True)
    return process


def autoarm(env, label):
    process = run(
        env,
        "printf '%s\\n' '" + PAYLOAD + "' | \"$FM_ROOT_OVERRIDE/bin/fm-claude-stop-autoarm.sh\"; "
        "rc=$?; printf 'autoarm_rc=%s\\n' \"$rc\"; true",
    )
    print(label, "rc=" + str(process.returncode), "stdout=" + repr(process.stdout), "stderr=" + repr(process.stderr), flush=True)
    return process


def watcher_healthy(env):
    process = run(
        env,
        '. "$FM_ROOT_OVERRIDE/bin/fm-wake-lib.sh"; '
        'if fm_watcher_healthy "$FM_HOME/state" "$FM_ROOT_OVERRIDE/bin/fm-watch.sh" 300 "$FM_HOME"; then '
        'printf "watcher_healthy=1\\n"; fi',
    )
    return process.returncode == 0 and "watcher_healthy=1" in process.stdout


def uncertain_ancestry_guard(env, mode):
    fake_bin = LAB / f"fake-ps-{mode}"
    fake_bin.mkdir()
    ps = fake_bin / "ps"
    ps.write_text(
        """#!/bin/bash
field= pid=
while [ "$#" -gt 0 ]; do case "$1" in -o) field=$2; shift 2;; -p) pid=$2; shift 2;; *) shift;; esac; done
if [ "$pid" = "$OWNER_PID" ]; then
 case "$field" in comm=) echo claude;; args=) echo claude;; ppid=) echo 1;; esac
elif [ "$pid" = 600 ]; then
 case "$field" in comm=) echo claude;; args=) echo claude;; ppid=) if [ "$MODE" = partial ]; then exit 1; else echo 601; fi;; esac
elif [ "$MODE" = depth ] && [ "$pid" -ge 601 ] && [ "$pid" -le 615 ]; then
 case "$field" in comm=) echo claude;; args=) echo claude;; ppid=) echo $((pid+1));; esac
elif [ "$pid" = 1 ]; then echo init
else
 case "$field" in comm=) echo bash;; args=) echo bash;; ppid=) echo 600;; esac
fi
"""
    )
    ps.chmod(0o755)
    fault_env = env | {
        "PATH": str(fake_bin) + ":" + env["PATH"],
        "OWNER_PID": lock_owner,
        "MODE": mode,
    }
    result = guard(fault_env, f"uncertain ancestry ({mode})")
    require(result.returncode == 2, f"{mode} ancestry uncertainty must retain the ordinary guard block")
    require("TURN WOULD END BLIND" in result.stderr, f"{mode} ancestry uncertainty lost the guard banner")
    require("SUPERVISION IS OWNED BY ANOTHER LIVE SESSION" not in result.stdout,
            f"{mode} ancestry uncertainty was treated as foreign-owner proof")
    return fault_env


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


try:
    root, env = make("nonowner")
    owner = start(
        env,
        '"$FM_ROOT_OVERRIDE/bin/fm-lock.sh" && touch "$FM_HOME/state/owner-ready" && while :; do sleep 1; done',
        "owner-idle.txt",
    )
    lock_path = root / "state/.lock"
    until(
        lambda: session_lock_text(lock_path) is not None,
        message=lambda: "synthetic owner did not publish a readable state/.lock; owner log="
        + (OUT / "owner-idle.txt").read_text(errors="replace"),
    )
    until(
        lambda: (root / "state/owner-ready").exists(),
        message="synthetic owner published state/.lock but did not reach owner-ready",
    )
    beat = root / "state/.last-watcher-beat"
    beat.touch()
    old_time = time.time() - 600
    os.utime(beat, (old_time, old_time))
    lock_owner = session_lock_text(lock_path)
    require(lock_owner is not None, "state/.lock vanished after the owner-ready wait")
    print("SETUP live synthetic owner=", owner.pid, "lock=", lock_owner, flush=True)

    acquisition = run(
        env,
        '"$FM_ROOT_OVERRIDE/bin/fm-lock.sh"; rc=$?; printf "lock_rc=%s\\n" "$rc"; true',
    )
    print("second-session acquisition", "rc=" + str(acquisition.returncode), "stdout=" + repr(acquisition.stdout), "stderr=" + repr(acquisition.stderr), flush=True)
    require("lock_rc=1" in acquisition.stdout, "foreign session unexpectedly acquired the session lock")
    require("another live firstmate session holds the lock" in acquisition.stderr, "lock refusal lost its ownership diagnostic")

    auto = autoarm(env, "nonowner autoarm")
    require(auto.returncode == 0, "foreign-owner auto-arm must exit safely")
    require(not (root / "state/.claude-autoarm-epoch").exists(), "foreign-owner auto-arm must not claim a generation")

    for number in range(1, 6):
        result = guard(env, f"nonowner stop {number}")
        require(result.returncode == 0, f"foreign-owner Stop {number} must end safely")
        require("SUPERVISION IS OWNED BY ANOTHER LIVE SESSION" in result.stdout, "foreign-owner Stop lost its clear diagnostic")
        require("cannot and should not arm or repair" in result.stdout, "diagnostic did not explain the safe ownership boundary")
    require(not (root / "state/.turnend-claude-blocks").exists(), "foreign-owner guard must not consume its block budget")
    print("FIXED repeated non-owner Stops: all five ended safely", flush=True)

    beat.touch()
    fresh = guard(env, "fresh-beat-only counterfactual")
    require(fresh.returncode == 0, "a fresh leftover beat must not restore foreign-owner blocking")

    lock_path.write_text("600\n")
    partial_env = uncertain_ancestry_guard(env, "partial")
    membership = run(
        partial_env,
        '. "$FM_ROOT_OVERRIDE/bin/fm-session-lock-lib.sh"; '
        'if fm_session_lock_owned_by_self "$FM_HOME/state"; then printf "partial_positive_membership=1\\n"; fi',
    )
    require("partial_positive_membership=1" in membership.stdout,
            "positive lock-owner membership must survive an incomplete ancestry walk")
    lock_path.write_text(lock_owner + "\n")
    uncertain_ancestry_guard(env, "depth")

    stop(owner)
    replacement_command = (
        '"$FM_ROOT_OVERRIDE/bin/fm-lock.sh" >/dev/null 2>&1; '
        'printf \'%s\\n\' \'{"session_id":"replacement","stop_hook_active":true}\' | '
        '"$FM_ROOT_OVERRIDE/bin/fm-claude-stop-autoarm.sh" > "$FM_HOME/state/autoarm.out" 2>&1 & '
        'printf \'%s\\n\' "$!" > "$FM_HOME/state/autoarm-pid"; '
        '. "$FM_ROOT_OVERRIDE/bin/fm-wake-lib.sh"; '
        'for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40; do '
        'fm_watcher_healthy "$FM_HOME/state" "$FM_ROOT_OVERRIDE/bin/fm-watch.sh" 300 "$FM_HOME" && break; sleep 0.05; done; '
        'printf \'%s\\n\' \'{"session_id":"replacement-owner","stop_hook_active":true}\' | '
        '"$FM_ROOT_OVERRIDE/bin/fm-turnend-guard.sh" --claude > "$FM_HOME/state/owning-guard.out" 2>&1; '
        'printf \'%s\\n\' "$?" > "$FM_HOME/state/owning-guard-rc"; '
        'while [ ! -e "$FM_HOME/state/replacement-finish" ]; do sleep 0.05; done'
    )
    replacement = start(
        env,
        replacement_command,
        "replacement.txt",
    )
    until(
        lambda: (root / "state/.watch.lock/pid").is_file(),
        message="replacement owner did not publish state/.watch.lock/pid",
    )
    watcher_pid = (root / "state/.watch.lock/pid").read_text().strip()
    until(lambda: watcher_healthy(env), message="replacement watcher never passed fm_watcher_healthy")
    until(lambda: (root / "state/owning-guard-rc").is_file(),
          message="replacement owner did not run its own turn-end guard")
    owner_guard_rc = (root / "state/owning-guard-rc").read_text().strip()
    owner_guard_out = (root / "state/owning-guard.out").read_text()
    require(owner_guard_rc == "0", "a replacement owner with a healthy watcher must allow its Stop")
    require(not owner_guard_out, f"a healthy owning-session Stop must be silent: {owner_guard_out!r}")
    print("COUNTERFACTUAL dead original owner: fm_watcher_healthy=1 watcher=", watcher_pid,
          "owning_guard_rc=0 silent=1", flush=True)
    healthy = guard(env, "replacement-owned healthy watcher")
    require(healthy.returncode == 0, "the watcher-health predicate must allow a non-owner Stop")
    require(not healthy.stdout, "the healthy watcher must allow the stop before foreign-owner diagnostics")
    os.kill(int(watcher_pid), signal.SIGTERM)
    until(lambda: not watcher_healthy(env), message="dead replacement watcher still passed fm_watcher_healthy")
    require(session_lock_text(lock_path) == str(replacement.pid),
            "dead-watcher counterfactual must retain the live replacement session lock")
    print("COUNTERFACTUAL dead replacement watcher: fm_watcher_healthy=0 while session lock remains live",
          flush=True)
    (root / "state/replacement-finish").touch()
    stop(replacement)

    single, single_env = make("single-idle")
    stale = single / "state/.last-watcher-beat"
    stale.touch()
    os.utime(stale, (old_time, old_time))
    sole_owner = run(
        single_env,
        '"$FM_ROOT_OVERRIDE/bin/fm-lock.sh"; . "$FM_ROOT_OVERRIDE/bin/fm-session-lock-lib.sh"; '
        'if fm_session_lock_owned_by_self "$FM_HOME/state"; then printf "single_owner_verified=1\\n"; fi; '
        'printf \'%s\\n\' \'{"session_id":"synthetic-second","stop_hook_active":true}\' | '
        '"$FM_ROOT_OVERRIDE/bin/fm-turnend-guard.sh" --claude; rc=$?; printf "single_owner_guard_rc=%s\\n" "$rc"; true',
    )
    print("single owner, no autoarm firing", "rc=" + str(sole_owner.returncode), "stdout=" + repr(sole_owner.stdout), "stderr=" + repr(sole_owner.stderr), flush=True)
    require("single_owner_verified=1" in sole_owner.stdout,
            "single-owner test did not prove that the guard owns the session lock")
    require("single_owner_guard_rc=2" in sole_owner.stdout, "a sole owner without supervision must retain the guard")
    print("COMPLETE", flush=True)
finally:
    for process in reversed(PROCS):
        stop(process)
    shutil.rmtree(LAB, ignore_errors=True)
