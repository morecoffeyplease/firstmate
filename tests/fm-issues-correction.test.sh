#!/usr/bin/env bash
# Focused deterministic status derivation and authority checks.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/fm-issues-correction.XXXXXX")
if [ "${FM_ISSUES_TEST_KEEP:-0}" = 1 ]; then
  printf 'fixture root: %s\n' "$TMP_ROOT"
else
  trap 'rm -rf "$TMP_ROOT"' EXIT
fi
FM_TEST_ROOT="$ROOT" FM_TEST_TMP="$TMP_ROOT" python3 - <<'PY'
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

root = pathlib.Path(__import__("os").environ["FM_TEST_ROOT"])
sys.path.insert(0, str(root / "bin"))
spec = importlib.util.spec_from_file_location("fm_issues", root / "bin" / "fm-issues.py")
issues = importlib.util.module_from_spec(spec)
spec.loader.exec_module(issues)

assert issues.canonical_issue_urls(["https://github.com/ACME/Widget/issues/7"], "acme/widget") == ["https://github.com/acme/widget/issues/7"]
assert issues.canonical_issue_urls(["https://github.com/acme/other/issues/7", "https://github.com/acme/widget/issues/0", "https://github.com/acme/widget/issues/8?x=1"], "acme/widget") == []

base_pr = {"url": "https://github.com/acme/widget/pull/7", "forge_checked": True, "state": "open", "head": "a" * 40, "draft": False, "checks": [{"status": "completed", "conclusion": "success"}], "reviews": [], "outstanding_changes_requested": False, "missing_verdicts": 0, "stale_verdicts": 0}
task = {"id": "ship", "kind": "ship", "task_state": "working", "backlog": {"state": "in_flight"}, "prs": [base_pr], "merge_requests": [], "source_head": "a" * 40, "verification": {"focused": {"status": "passed"}, "full": {"status": "passed"}, "verify": {"status": "passed"}}}
stage, waiting = issues.stage(task, [base_pr], task["backlog"])
assert stage == "In review" and waiting is None
assert issues.ready_state({**task, "stage": stage, "waiting": waiting}, {"full": ["make", "all"], "verify": ["make", "verify"]}) == "ready for approval"
assert issues.ready_state({**task, "stage": stage, "waiting": waiting, "source_head": "b" * 40}, {"full": ["make", "all"]}) == "not ready"
assert issues.ready_state({**task, "stage": stage, "waiting": waiting, "verification": {"full": {"status": "passed"}}}, {"full": ["make", "all"], "verify": ["make", "verify"]}) == "not ready"

queued = {"id": "queued", "kind": "ship", "task_state": "queued", "backlog": {"state": "queued", "unresolved_blocker_ids": ["prerequisite"]}, "hints": {}}
assert issues.stage(queued, [], queued["backlog"]) == ("Queued", "prerequisite")
decision = {"id": "held", "kind": "ship", "task_state": "working", "backlog": {"state": "in_flight", "hold_bucket": "live", "hold_reason": "decision"}, "hints": {"blocked_event": True}}
assert issues.stage(decision, [], decision["backlog"])[1] == "your decision", "decision evidence must take precedence over a generic blocked hint"

assert issues.pt(int(datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc).timestamp())).endswith("PST")
assert issues.pt(int(datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc).timestamp())).endswith("PDT")
assert issues.pt(None) == "unknown"
event_task = pathlib.Path(os.environ["FM_TEST_TMP"]) / "event-home" / "data" / "task-a"
event_task.mkdir(parents=True)
event_lib = root / "bin" / "fm-issue-events-lib.sh"
event_writer = subprocess.run(
    ["/bin/bash", "-c", '. "$1"; fm_issue_event_append "$2" task-a gen-1 started \'{"kind":"ship"}\'; fm_issue_event_append "$2" task-a gen-1 decision-resolved \'{"key":"choice-a"}\'; fm_issue_event_validate_file "$2/events.jsonl" task-a', "fixture", str(event_lib), str(event_task)],
    capture_output=True, text=True,
)
assert event_writer.returncode == 0 and issues.task_event_fact(event_task.parents[1], "task-a", "gen-1")["kind"] == "decision-resolved"
with (event_task / "events.jsonl").open("a") as stream:
    stream.write("{malformed}\n")
assert issues.task_event_fact(event_task.parents[1], "task-a", "gen-1") is None, "invalid lifecycle journal must remain unknown"

# Remote secondmate inventory is link-bound and cached summaries remain visibly
# stale instead of claiming complete current-task evidence.
home = pathlib.Path(os.environ["FM_TEST_TMP"]) / "remote-home"
for part in ("data", "state/issue-status", "state/issue-catalog", "projects/alpha", "config"):
    (home / part).mkdir(parents=True, exist_ok=True)
(home / "data" / "projects.md").write_text("- alpha [direct-PR] - fixture\n")
clone = home / "projects" / "alpha"
subprocess.run(["git", "init", "-q", str(clone)], check=True)
subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", "https://github.com/acme/widget.git"], check=True)
issue_url = "https://github.com/acme/widget/issues/9"
remote_task = {"id": "remote-ship", "generation": "gen-2", "kind": "ship", "current_state": {"state": "working", "source": "remote fixture"}, "backlog": {"state": "in_flight", "links": [issue_url], "repo": "alpha"}}
secondmate = {"id": "child", "provenance": {"summary_valid": True}, "freshness": {"status": "cached"}, "counts": {"issue_tasks": 1}, "issue_tasks": [remote_task], "omitted": [], "contributions": {"rows": []}}
snapshot = {"schema": "fm-fleet-snapshot.v1", "tasks": [], "backlog": {"records": []}, "contributions": {"rows": []}, "secondmate_current": {"records": [secondmate], "total": 1, "truncated": False}}
now = int(time.time())
(home / "state" / "issue-status" / "fleet.json").write_text(json.dumps({"schema": "fm-issue-fleet-cache.v1", "collected_epoch": now, "last_attempt_epoch": now, "snapshot": snapshot, "error": None}))
repo_hash = hashlib.sha256(b"acme/widget").hexdigest()
(home / "state" / "issue-catalog" / f"{repo_hash}.json").write_text(json.dumps({"schema": "fm-issue-catalog.v1", "repo": "acme/widget", "checked_epoch": now, "complete": True, "known": 1, "issues": [{"number": 9, "title": "Remote task", "url": issue_url, "state": "open", "updated_at": "2026-10-08T00:00:00Z"}], "identity_checks": [], "error": None, "stale": False}))
projection = issues._make_projection(home, "alpha")
assert projection["remote_issue_coverage"]["stale_home_summaries"] == 1 and not projection["remote_issue_coverage"]["complete"]
remote_row = next(row for row in projection["rows"] if row["url"] == issue_url)
assert remote_row["tasks"][0]["task_state"] == "unknown" and "stale" in remote_row["tasks"][0]["conflicts"][0]
print("pass: canonical issue identity, ready predicate, multiple wait facts, and UTC/PT timestamp classes")
PY
