#!/usr/bin/env bash
# Behavioral coverage for the deterministic Issues table and validation receipts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=tests/git-config-helpers.sh
. "$ROOT/tests/git-config-helpers.sh"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/fm-issues.XXXXXX")
if [ "${FM_ISSUES_TEST_KEEP:-0}" = 1 ]; then
  printf 'fixture home: %s/home\n' "$TMP_ROOT"
else
  trap 'rm -rf "$TMP_ROOT"' EXIT
fi
FM_TEST_ROOT="$ROOT" FM_TEST_TMP="$TMP_ROOT" python3 - <<'PY'
import hashlib
import json
import os
import pathlib
import re
import signal
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

root = pathlib.Path(os.environ["FM_TEST_ROOT"])
tmp = pathlib.Path(os.environ["FM_TEST_TMP"])
sys.path.insert(0, str(root / "bin"))
import importlib.util
spec = importlib.util.spec_from_file_location("fm_issues", root / "bin" / "fm-issues.py")
issues = importlib.util.module_from_spec(spec)
spec.loader.exec_module(issues)

home = tmp / "home"
for path in (home / "state", home / "data", home / "projects", home / "config", home / "state" / "issue-status", home / "data" / "task-a" / "lane-receipts"):
    path.mkdir(parents=True, exist_ok=True)
(home / "data" / "projects.md").write_text("- alpha [direct-PR] - fixture project (added 2026-10-08)\n")
(home / "config" / "project-lanes.json").write_text(json.dumps({"alpha": {"full": ["/bin/echo", "full-lane"], "verify": ["/bin/echo", "verify-lane"]}}))
(home / "data" / "task-a" / "lane-receipts" / ".instrumented").write_text("gen-1\n")

repo_url = "https://github.com/acme/widget.git"
project_clone = home / "projects" / "alpha"
worktree = tmp / "worktree"
for path in (project_clone, worktree):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", repo_url], check=True)
    (path / "tracked.txt").write_text("fixture\n")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
head = subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()

issue1 = "https://github.com/acme/widget/issues/1"
issue2 = "https://github.com/acme/widget/issues/2"
issue3 = "https://github.com/acme/widget/issues/3"
tasks = [
    {"id": "task-a", "kind": "ship", "project": "alpha", "spawn_gen": "gen-1", "current_state": {"state": "working"}, "backlog": {"repo": "alpha", "state": "in_flight", "links": [issue1]}, "pr": {"url": "https://github.com/acme/widget/pull/11", "head": "abc"}, "paths": {"worktree": {"path": str(worktree)}}},
    {"id": "task-b", "kind": "ship", "project": "alpha", "spawn_gen": "gen-b", "current_state": {"state": "working"}, "backlog": {"repo": "alpha", "state": "in_flight", "links": [issue1]}, "pr": {"url": "https://github.com/acme/widget/pull/12", "head": "def"}, "paths": {"worktree": {"path": str(worktree)}}},
]
snapshot = {"schema": "fm-fleet-snapshot.v1", "tasks": tasks, "backlog": {"records": [{"id": "queued-a", "kind": "ship", "repo": "alpha", "state": "queued", "links": [issue2], "blocked_by": "task-a", "blocked_by_ids": ["task-a"], "unresolved_blocker_ids": ["task-a"]}]}, "contributions": {"rows": []}}
(home / "state" / "issue-status" / "fleet.json").write_text(json.dumps({"schema": "fm-issue-fleet-cache.v1", "collected_epoch": int(time.time()), "snapshot": snapshot, "error": None}))
repo_key = hashlib.sha256(b"acme/widget").hexdigest()
issues_payload = [
    {"number": 1, "title": "Active issue", "url": issue1, "state": "open", "updated_at": "2026-10-08T18:00:00Z"},
    {"number": 2, "title": "Queued issue", "url": issue2, "state": "open", "updated_at": "2026-10-08T18:00:00Z"},
    {"number": 3, "title": "Old closed issue", "url": issue3, "state": "closed", "updated_at": "2020-01-01T00:00:00Z"},
]
cat_path = home / "state" / "issue-catalog" / f"{repo_key}.json"
cat_path.parent.mkdir(parents=True, exist_ok=True)
cat_path.write_text(json.dumps({"schema": "fm-issue-catalog.v1", "repo": "acme/widget", "checked_epoch": int(time.time()), "complete": True, "known": 3, "issues": [{**item, "updated_epoch": int(time.time())} for item in issues_payload], "error": None, "stale": False}))

fakebin = tmp / "fakebin"
fakebin.mkdir()
(fakebin / "gh").write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FM_FAKE_GH_ARGS\"\nif [ -n \"${FM_FAKE_GH_FAIL:-}\" ]; then echo offline >&2; exit 7; fi\ncase \"$2\" in\n  'repos/acme/widget/issues?state=all&per_page=100') cat \"$FM_FAKE_ISSUES\" ;;\n  repos/acme/widget/issues/1) printf '%s\\n' '{\"html_url\":\"https://github.com/acme/widget/issues/1\"}' ;;\n  repos/acme/widget/issues/2) printf '%s\\n' '{\"html_url\":\"https://github.com/acme/widget/issues/2\"}' ;;\n  repos/acme/widget/issues/3) printf '%s\\n' '{\"html_url\":\"https://github.com/acme/widget/issues/3\"}' ;;\n  *) echo \"unexpected canonical forge call: $*\" >&2; exit 91 ;;\nesac\n")
(fakebin / "gh").chmod(0o755)
issues_file = tmp / "issues.ndjson"
def write_issues():
    issues_file.write_text("".join(json.dumps({"number": item["number"], "title": item["title"], "html_url": item["url"], "state": item["state"], "updated_at": item["updated_at"]}) + "\n" for item in issues_payload))
write_issues()
env = {**os.environ, "FM_HOME": str(home), "PATH": f"{fakebin}:{os.environ['PATH']}", "FM_FAKE_ISSUES": str(issues_file), "FM_FAKE_GH_ARGS": str(tmp / "gh-args")}
cli = [str(root / "bin" / "fm-issues.sh"), "--project", "alpha", "--json"]
def projection(extra=(), extra_env=None):
    result = subprocess.run(cli + list(extra), cwd=root, env={**env, **(extra_env or {})}, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(f"issue launcher failed: {result.stderr}")
    return json.loads(result.stdout)

first = projection()
assert len(first["rows"]) == 3, "catalog omitted an old closed issue from the full inventory"
by_url = {row["url"]: row for row in first["rows"]}
assert by_url[issue1]["task_count"] == 2 and by_url[issue1]["pr_count"] == 2, "multiple tasks and PRs did not aggregate"
assert by_url[issue1]["stage"] == "Unknown", "unobserved linked PR evidence must remain unknown"
assert by_url[issue2]["stage"] == "Queued" and "task-a" in by_url[issue2]["next_step"], "queued prerequisite was lost"
assert by_url[issue3]["stage"] == "Closed without delivery", "closed unowned issue was omitted"
cache = json.loads(cat_path.read_text())
cache["last_attempt_epoch"] = 0
cat_path.write_text(json.dumps(cache))
second = projection(("--refresh",))
assert first["fingerprint"] == second["fingerprint"], f"repoll/check ages changed the relevant fingerprint: {first['fingerprint']} -> {second['fingerprint']}; catalog={second['catalog']!r}; first={first['rows']!r}; second={second['rows']!r}"
assert "--paginate" in (tmp / "gh-args").read_text(), "canonical forge reader did not request all paginated issue inventory"
snapshot["tasks"].reverse()
(home / "state" / "issue-status" / "fleet.json").write_text(json.dumps({"schema": "fm-issue-fleet-cache.v1", "collected_epoch": int(time.time()), "snapshot": snapshot, "error": None}))
reordered = projection(("--refresh",))
assert reordered["fingerprint"] == second["fingerprint"], "task source ordering changed the relevant fingerprint"
snapshot["tasks"].append({"id": "unlinked-task", "kind": "ship", "project": "alpha", "spawn_gen": "gen-u", "current_state": {"state": "working"}, "backlog": {"repo": "alpha", "state": "in_flight", "links": []}, "pr": {"url": None}})
(home / "state" / "issue-status" / "fleet.json").write_text(json.dumps({"schema": "fm-issue-fleet-cache.v1", "collected_epoch": int(time.time()), "snapshot": snapshot, "error": None}))
unlinked_change = projection(("--refresh",))
assert len(unlinked_change["unlinked_tasks"]) == 1 and unlinked_change["fingerprint"] != second["fingerprint"], "unlinked task change did not invalidate project summary basis"

cache = json.loads(cat_path.read_text())
cache["last_attempt_epoch"] = 0
cat_path.write_text(json.dumps(cache))
result = projection(("--refresh",), {"FM_FAKE_GH_FAIL": "1"})
assert result["catalog"]["stale"] is True and result["catalog"]["known"] == 3 and result["catalog"]["error"], "catalog outage discarded last-known coverage or hid its error"
issues_payload[0]["title"] = "Renamed issue"
write_issues()
cache = json.loads(cat_path.read_text())
cache["last_attempt_epoch"] = 0
cat_path.write_text(json.dumps(cache))
result = projection(("--refresh",))
renamed = next(row for row in result["rows"] if row["url"] == issue1)
assert renamed["changed"]["class"] == "detected" and renamed["changed"]["to_epoch"] >= renamed["changed"]["from_epoch"], "rename was given an unsupported exact event timestamp"

assert issues.pt(int(datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp())).endswith("PST") and issues.pt(int(datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp())).endswith("PDT"), "IANA timezone display did not follow daylight-saving changes"
assert issues.pt(None) == "unknown", "missing legacy timestamp was not kept unknown"
event_result = subprocess.run(["/bin/bash", "-c", '. "$1"; fm_issue_event_append "$2" task-a gen-1 started \'{"kind":"ship"}\'; fm_issue_event_append "$2" task-a gen-1 decision-resolved \'{"key":"choice-a"}\'; fm_issue_event_validate_file "$2/events.jsonl" task-a', "fixture", str(root / "bin" / "fm-issue-events-lib.sh"), str(home / "data" / "task-a")], capture_output=True, text=True)
assert event_result.returncode == 0 and len((home / "data" / "task-a" / "events.jsonl").read_text().splitlines()) == 2, "typed task event journal did not append and validate durable generation facts"

child = tmp / "child.py"
child.write_text("import json,os,sys\nprint(json.dumps({'cwd':os.getcwd(),'marker':os.environ.get('FIXTURE_MARKER'),'argv':sys.argv[1:],'stdin':sys.stdin.read()}))\nprint('child-stderr',file=sys.stderr)\n")
receipt_env = {**env, "FM_TASK_ID": "task-a", "FM_TASK_GENERATION": "gen-1", "FM_LANE_RECEIPTS": str(home / "data" / "task-a" / "lane-receipts"), "FIXTURE_MARKER": "preserved"}
wrapper = root / "bin" / "fm-lane-run.sh"
run = subprocess.run([str(wrapper), "focused", "--", sys.executable, str(child), "arg with spaces"], cwd=worktree, env=receipt_env, input="stdin bytes\n", text=True, capture_output=True)
assert run.returncode == 0 and run.stderr == "child-stderr\n", "wrapper changed child exit or stderr"
child_result = json.loads(run.stdout)
assert child_result == {"cwd": str(worktree.resolve()), "marker": "preserved", "argv": ["arg with spaces"], "stdin": "stdin bytes\n"}, f"wrapper changed argv, environment, cwd, or stdin: {child_result!r}"
tty_child = tmp / "tty-child.py"
tty_child.write_text("import json,os\nprint(json.dumps([os.isatty(0),os.isatty(1),os.isatty(2)]))\n")
master_fd, slave_fd = pty.openpty()
tty_proc = subprocess.Popen([str(wrapper), "focused", "--", sys.executable, str(tty_child)], cwd=worktree, env=receipt_env, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True)
os.close(slave_fd)
tty_bytes = bytearray()
while True:
    ready, _, _ = select.select([master_fd], [], [], 5)
    if not ready:
        tty_proc.kill()
        raise AssertionError("TTY-preserving wrapped child stalled")
    try:
        chunk = os.read(master_fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    tty_bytes.extend(chunk)
os.close(master_fd)
assert tty_proc.wait(timeout=5) == 0 and b"[true, true, true]" in tty_bytes.lower(), "wrapper did not preserve the caller's TTY streams"
assert subprocess.run([str(wrapper), "full"], cwd=worktree, env=receipt_env, capture_output=True).returncode == 0, "configured full lane did not run"
time.sleep(1.05)
observed = projection()
task_a = next(task for row in observed["rows"] for task in row["tasks"] if task["id"] == "task-a")
assert task_a["verification"]["focused"]["status"] == "passed" and task_a["verification"]["full"]["status"] == "passed" and task_a["verification"]["verify"]["status"] == "not run", f"current focused/full receipt qualification was incorrect: {task_a['verification']!r}"
task_b = next(task for row in observed["rows"] for task in row["tasks"] if task["id"] == "task-b")
assert task_b["verification"]["full"]["status"] == "not instrumented", "legacy task without launch receipt binding was given a run status"
full_receipt = max((path for path in (home / "data" / "task-a" / "lane-receipts").glob("full-*.json") if json.loads(path.read_text()).get("phase") == "finish"), key=lambda path: json.loads(path.read_text())["started_epoch"])
full_value = json.loads(full_receipt.read_text())
full_value["argv"] = ["/bin/echo", "filtered-configured-argv"]
full_receipt.write_text(json.dumps(full_value))
filtered = projection(("--refresh",))
task_a = next(task for row in filtered["rows"] for task in row["tasks"] if task["id"] == "task-a")
assert task_a["verification"]["full"]["status"] == "not run" and task_a["verification"]["focused"]["status"] == "reclassified focused run; outcome passed", "filtered full lane receipt did not preserve its focused outcome"
(home / "data" / "task-a" / "lane-receipts" / ".instrumented").write_text("wrong-generation\n")
conflicted = projection(("--refresh",))
task_a = next(task for row in conflicted["rows"] for task in row["tasks"] if task["id"] == "task-a")
assert task_a["verification"]["full"]["status"] == "unknown", "launch generation conflict was silently treated as not instrumented"
(home / "data" / "task-a" / "lane-receipts" / ".instrumented").write_text("gen-1\n")
tracked = worktree / "tracked.txt"
tracked.write_text("dirty fixture\n")
dirty_run = subprocess.run([str(wrapper), "focused", "--", "/usr/bin/true"], cwd=worktree, env=receipt_env, capture_output=True)
assert dirty_run.returncode == 0, f"dirty focused run failed unexpectedly: {dirty_run.stderr!r}"
dirty = projection()
task_a = next(task for row in dirty["rows"] for task in row["tasks"] if task["id"] == "task-a")
assert task_a["verification"]["focused"]["status"] == "passed on uncommitted or changed source", f"dirty-source run received unexpected classification: {task_a['verification']['focused']}"
subprocess.run(["git", "-C", str(worktree), "checkout", "--", "tracked.txt"], check=True)
assert subprocess.run([str(wrapper), "focused", "--", "/usr/bin/true"], cwd=worktree, env=receipt_env, capture_output=True).returncode == 0
subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "new head"], check=True)
stale = projection()
task_a = next(task for row in stale["rows"] for task in row["tasks"] if task["id"] == "task-a")
assert task_a["verification"]["focused"]["status"] == "stale (passed on an older head)", "older clean receipt received current-head credit"
assert subprocess.run([str(wrapper), "focused", "--", "/bin/sh", "-c", "exit 23"], cwd=worktree, env=receipt_env, capture_output=True).returncode == 23, "wrapper changed child exit code"

# A process-bound start receipt must become interrupted if its owner is dead.
proc = subprocess.Popen([str(wrapper), "focused", "--", "/bin/sleep", "10"], cwd=worktree, env=receipt_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    starts = list((home / "data" / "task-a" / "lane-receipts").glob("focused-*.json"))
    live_start = next((path for path in starts if json.loads(path.read_text()).get("phase") == "start" and json.loads(path.read_text()).get("pid") == proc.pid), None)
    if live_start:
        break
    time.sleep(0.03)
assert live_start is not None, "start receipt was not durable while the child was running"
running = projection()
task_a = next(task for row in running["rows"] for task in row["tasks"] if task["id"] == "task-a")
assert task_a["verification"]["focused"]["status"] == "running", "live process identity was not observed as running"
proc.terminate()
assert proc.wait(timeout=5) == -signal.SIGTERM, "wrapper did not preserve signal termination"
assert subprocess.run([str(wrapper), "focused", "--", "/bin/sh", "-c", "exit 0"], cwd=worktree, env={**receipt_env, "FM_LANE_RECEIPTS": str(tmp / "receipt-failure")}, capture_output=True).returncode == 0, "receipt failure changed the wrapped command result"

# Summary request deduplication, basis comparison, and expiry use the isolated home only.
request_cmd = [sys.executable, str(root / "bin" / "fm-issues.py"), "summary", "request", "--project", "alpha"]
req1 = json.loads(subprocess.check_output(request_cmd, cwd=root, env=env, text=True))
req2 = json.loads(subprocess.check_output(request_cmd, cwd=root, env=env, text=True))
assert req1["status"] == "pending" and req2.get("deduplicated") is True, "pending manual requests did not deduplicate"
request_file = next((home / "state" / "status-summary" / "requests").glob("*.json"))
record = json.loads(request_file.read_text())
textfile = tmp / "summary.txt"
textfile.write_text("Fixture project summary.\n")
basis = json.loads(subprocess.check_output([sys.executable, str(root / "bin" / "fm-issues.py"), "summary", "basis", record["id"], "--project", "alpha"], cwd=root, env=env, text=True))
put = [sys.executable, str(root / "bin" / "fm-issues.py"), "summary", "put", record["id"], "alpha", "--basis-fingerprint", basis["fingerprint"], "--basis-transition-watermark", basis["transition_watermark"], "--basis-observed-at", str(basis["observed_epoch"]), "--author", "main-home", "--text-file", str(textfile)]
written = json.loads(subprocess.check_output(put, cwd=root, env=env, text=True))
assert written["status"] == "written", "summary with unchanged basis did not publish"
req3 = json.loads(subprocess.check_output(request_cmd, cwd=root, env=env, text=True))
assert req3["status"] == "pending", "completed request could not be retried as a fresh bounded request"
request_file = home / "state" / "status-summary" / "requests" / f"{req3['request']}.json"
record = json.loads(request_file.read_text())
next_basis = json.loads(subprocess.check_output([sys.executable, str(root / "bin" / "fm-issues.py"), "summary", "basis", record["id"], "--project", "alpha"], cwd=root, env=env, text=True))
snapshot["tasks"][0]["current_state"]["state"] = "paused"
(home / "state" / "issue-status" / "fleet.json").write_text(json.dumps({"schema": "fm-issue-fleet-cache.v1", "collected_epoch": int(time.time()), "snapshot": snapshot, "error": None}))
put = [sys.executable, str(root / "bin" / "fm-issues.py"), "summary", "put", record["id"], "alpha", "--basis-fingerprint", next_basis["fingerprint"], "--basis-transition-watermark", next_basis["transition_watermark"], "--basis-observed-at", str(next_basis["observed_epoch"]), "--author", "main-home", "--text-file", str(textfile)]
outdated = json.loads(subprocess.check_output(put, cwd=root, env=env, text=True))
assert outdated["status"] == "outdated", "status change during summary composition was not marked outdated"
record = json.loads(request_file.read_text())
assert record["state"] == "outdated" and record["supervisor_availability"] in ("available", "unavailable", "unknown"), "summary request state or supervisor availability was not explicit"
req4 = json.loads(subprocess.check_output(request_cmd, cwd=root, env=env, text=True))
expired_file = home / "state" / "status-summary" / "requests" / f"{req4['request']}.json"
expired_record = json.loads(expired_file.read_text())
expired_record["expires_epoch"] = 0
expired_file.write_text(json.dumps(expired_record))
listed = json.loads(subprocess.check_output([sys.executable, str(root / "bin" / "fm-issues.py"), "summary", "list"], cwd=root, env=env, text=True))
assert next(item for item in listed if item["id"] == req4["request"])["state"] == "expired", "pending request expiry was not exposed"
expired_put = subprocess.run(put[:4] + [req4["request"], "alpha"] + put[6:], cwd=root, env=env, text=True, capture_output=True)
assert expired_put.returncode == 1 and json.loads(expired_put.stdout)["status"] == "expired", "expired request accepted a late summary"

# The supported home-local backlog route stamps task blocker changes without
# changing the underlying tasks-axi command result.
if shutil.which("tasks-axi"):
    blocker_home = tmp / "blocker-home"
    blocker_code = tmp / "blocker-code"
    (blocker_home / "data").mkdir(parents=True)
    (blocker_home / "state").mkdir()
    (blocker_home / "config").mkdir()
    (blocker_code / "data").mkdir(parents=True)
    (blocker_home / "data" / "backlog.md").write_text("## In flight\n\n## Queued\n\n## Done\n")
    (blocker_code / ".tasks.toml").write_bytes((root / ".tasks.toml").read_bytes())
    (blocker_code / "data" / "backlog.md").symlink_to(blocker_home / "data" / "backlog.md")
    event_task = "event-task"
    (blocker_home / "state" / f"{event_task}.meta").write_text("spawn_gen=gen-blocker\n")
    (blocker_home / "data" / event_task).mkdir()
    wrapper = root / "bin" / "fm-tasks-axi.sh"
    wrapper_env = {**os.environ, "FM_HOME": str(blocker_home), "FM_ROOT_OVERRIDE": str(blocker_code)}
    for command in (["add", "blocker-task", "Blocker"], ["add", event_task, "Blocked task"], ["block", event_task, "--by", "blocker-task"], ["unblock", event_task, "--by", "blocker-task"]):
        subprocess.run([str(wrapper), *command], cwd=blocker_code, env=wrapper_env, check=True, capture_output=True, text=True)
    events = [json.loads(line) for line in (blocker_home / "data" / event_task / "events.jsonl").read_text().splitlines()]
    assert [(item["kind"], item["fields"]["blocker"]) for item in events] == [("blocked-by", "blocker-task"), ("unblocked", "blocker-task")], events
    assert all(item["generation"] == "gen-blocker" for item in events), events

# Loopback POST rejects missing origin/token and accepts the exact launch token/origin.
server_env = {**env, "BROWSER": "/usr/bin/true"}
server = subprocess.Popen([sys.executable, str(root / "bin" / "fm-issues.py"), "--project", "alpha", "--port", "0"], cwd=root, env=server_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    url = server.stdout.readline().strip()
    assert url.startswith("http://127.0.0.1:"), "browser service did not bind to IPv4 loopback"
    page = urllib.request.urlopen(url, timeout=5).read().decode()
    token = re.search(r"const token=(.*),initial=", page).group(1)
    endpoint = url.rstrip("/") + "/api/summary-requests"
    body = json.dumps({"projects": ["alpha"]}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}), timeout=5)
        raise AssertionError("summary endpoint accepted a request without Origin and token")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    port = url.rstrip("/").rsplit(":", 1)[1]
    valid_headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}", "X-FM-Token": json.loads(token)}
    try:
        urllib.request.urlopen(urllib.request.Request(endpoint, data=body, headers={**valid_headers, "Host": "evil.example"}), timeout=5)
        raise AssertionError("summary endpoint accepted a non-loopback Host")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    try:
        urllib.request.urlopen(urllib.request.Request(endpoint, data=json.dumps({"projects": ["unregistered"]}).encode(), headers=valid_headers), timeout=5)
        raise AssertionError("summary endpoint accepted an unregistered project")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    response = urllib.request.urlopen(urllib.request.Request(endpoint, data=body, headers=valid_headers), timeout=10)
    assert response.status == 200, "authorized loopback request was rejected"
finally:
    server.terminate()
    server.wait(timeout=5)
print("ok - issue inventory, cached fingerprint, outage coverage, timestamps, summary requests, loopback CSRF, and validation receipts")
PY
