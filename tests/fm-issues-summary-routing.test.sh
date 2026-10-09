#!/usr/bin/env bash
# Focused summary request routing, deduplication, expiry, and basis checks.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=tests/git-config-helpers.sh
. "$ROOT/tests/git-config-helpers.sh"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/fm-summary-routing.XXXXXX")
if [ "${FM_ISSUES_TEST_KEEP:-0}" = 1 ]; then
  printf 'fixture root: %s\n' "$TMP_ROOT"
else
  trap 'rm -rf "$TMP_ROOT"' EXIT
fi
FM_TEST_ROOT="$ROOT" FM_TEST_TMP="$TMP_ROOT" python3 - <<'PY'
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

root = pathlib.Path(os.environ["FM_TEST_ROOT"])
tmp = pathlib.Path(os.environ["FM_TEST_TMP"])
home = tmp / "home"
for name in ("data", "state", "projects", "config"):
    (home / name).mkdir(parents=True, exist_ok=True)
projects = ("alpha", "beta")
for name in projects:
    (home / "projects" / name).mkdir()
    subprocess.run(["git", "init", "-q", str(home / "projects" / name)], check=True)
    subprocess.run(["git", "-C", str(home / "projects" / name), "remote", "add", "origin", f"https://github.com/acme/{name}.git"], check=True)
(home / "data" / "projects.md").write_text("".join(f"- {name} [direct-PR] - fixture\n" for name in projects))
(home / "state" / "issue-status").mkdir()
snapshot = {"schema": "fm-fleet-snapshot.v1", "tasks": [], "backlog": {"records": []}, "contributions": {"rows": []}, "secondmate_current": {"records": [], "total": 0, "truncated": False}}
(home / "state" / "issue-status" / "fleet.json").write_text(json.dumps({"schema": "fm-issue-fleet-cache.v1", "collected_epoch": int(time.time()), "last_attempt_epoch": int(time.time()), "snapshot": snapshot, "error": None}))
for name in projects:
    repo = f"acme/{name}"
    digest = hashlib.sha256(repo.lower().encode()).hexdigest()
    path = home / "state" / "issue-catalog" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "fm-issue-catalog.v1", "repo": repo, "checked_epoch": int(time.time()), "complete": True, "known": 0, "issues": [], "identity_checks": [], "error": None, "stale": False}))

env = {**os.environ, "FM_HOME": str(home)}
cli = [sys.executable, str(root / "bin" / "fm-issues.py"), "--home", str(home)]
def run(*args, ok=0):
    result = subprocess.run(cli + ["summary", *args], cwd=root, env=env, text=True, capture_output=True)
    assert result.returncode == ok, (args, result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout) if result.stdout.strip() else None

first = run("request", "--project", "alpha", "--project", "beta")
assert first["status"] == "pending" and set(first["projects"]) == set(projects)
request_path = home / "state" / "status-summary" / "requests" / f"{first['request']}.json"
request_record = json.loads(request_path.read_text())
assert request_record["routes"] == {name: {"route": "main-home", "target": None, "state": "pending"} for name in projects}
dedup = run("request", "--project", "alpha", "--project", "beta")
assert dedup["deduplicated"] is True and dedup["requests"]["alpha"] == first["request"] and dedup["requests"]["beta"] == first["request"]
listed = run("list")
project_results = {item["project"]: item for item in listed[0]["project_results"]}
assert project_results["alpha"]["state"] == "pending" and project_results["alpha"]["route"] == "main-home"

textfile = tmp / "summary.md"
textfile.write_text("A concise written project summary.\n")
record_path = home / "state" / "status-summary" / "requests" / f"{first['request']}.json"
record = json.loads(record_path.read_text())
put = run("put", first["request"], "alpha", "--basis-fingerprint", "a" * 64, "--basis-observed-at", str(record["requested_epoch"]), "--author", "fixture-worker", "--text-file", str(textfile))
assert put["status"] == "outdated", "basis changed during composition must be reported immediately"
summary_file = next((home / "state" / "status-summary" / "summaries").glob("*.json"))
summary_record = json.loads(summary_file.read_text())["summaries"][-1]
assert summary_record["text"] == textfile.read_text() and summary_record["state"] == "outdated"
assert summary_record["evidence"]["basis_fingerprint"] == "a" * 64 and summary_record["evidence"]["comparison_fingerprint"] != "a" * 64

record = json.loads(record_path.read_text())
record["expires_epoch"] = int(time.time()) - 1
record_path.write_text(json.dumps(record))
expired = run("resolve", first["request"], "beta", "unavailable", "--reason", "late reply", ok=1)
assert expired["status"] == "expired"

# Registry routing uses the validated structured project route and exact task
# metadata identity, while unassigned projects remain owned by the main home.
mate_home = tmp / "mate-a"
mate_home.mkdir()
(home / "state" / "mate-a.meta").write_text("kind=secondmate\n")
(home / "data" / "secondmates.md").write_text(f"- mate-a - Alpha owner (home: {mate_home}; scope: project work; projects: alpha; added 2026-10-08)\n")
sys.path.insert(0, str(root / "bin"))
import importlib.util
spec = importlib.util.spec_from_file_location("fm_issues", root / "bin" / "fm-issues.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
routes = module.summary_route_candidates(home, ["alpha", "beta"])
assert routes["alpha"] == {"route": "secondmate", "target": "mate-a", "state": "pending"}, routes
assert routes["beta"] == {"route": "main-home", "target": None, "state": "pending"}, routes
marker = f"request={first['request']} project=alpha"
create_reply = subprocess.run(["/bin/bash", "-c", '. "$1"; fm_pending_reply_create "$2" "$3" "$4" "$5"', "fixture", str(root / "bin" / "fm-pending-reply-lib.sh"), str(home), str(home / "state"), "mate-a", marker + " Request Manual Update"], text=True, capture_output=True, check=True)
assert module.summary_pending_correlation(home, "mate-a", first["request"], "alpha") == create_reply.stdout
print("pass: selected-project routing, per-project deduplication, expiry, and immediate stale-basis outcome")
PY
