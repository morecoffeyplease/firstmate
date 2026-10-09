#!/usr/bin/env bash
# Focused deterministic status derivation and authority checks.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=tests/git-config-helpers.sh
. "$ROOT/tests/git-config-helpers.sh"
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
remote_task = {"id": "remote-ship", "generation": "gen-2", "kind": "ship", "current_state": {"state": "working", "source": "remote fixture"}, "source": {"head": "b" * 40, "dirty": False, "freshness": "fresh"}, "verification": {"focused": {"status": "passed"}, "full": {"status": "passed"}, "verify": {"status": "passed"}}, "backlog": {"state": "in_flight", "links": [issue_url], "repo": "alpha"}}
secondmate = {"id": "child", "provenance": {"summary_valid": True}, "freshness": {"status": "cached"}, "counts": {"issue_tasks": 1}, "issue_tasks": [remote_task], "omitted": [], "contributions": {"rows": []}}
snapshot = {"schema": "fm-fleet-snapshot.v1", "tasks": [], "backlog": {"records": []}, "contributions": {"rows": []}, "secondmate_current": {"records": [secondmate], "total": 1, "truncated": False}}
now = int(time.time())
(home / "state" / "issue-status" / "fleet.json").write_text(json.dumps({"schema": "fm-issue-fleet-cache.v1", "collected_epoch": now, "last_attempt_epoch": now, "snapshot": snapshot, "error": None}))
repo_hash = hashlib.sha256(b"acme/widget").hexdigest()
(home / "state" / "issue-catalog" / f"{repo_hash}.json").write_text(json.dumps({"schema": "fm-issue-catalog.v1", "repo": "acme/widget", "checked_epoch": now, "complete": True, "known": 1, "issues": [{"number": 9, "title": "Remote task", "url": issue_url, "state": "open", "updated_at": "2026-10-08T00:00:00Z"}], "identity_checks": [], "error": None, "stale": False}))
projection = issues._make_projection(home, "alpha")
assert projection["remote_issue_coverage"]["stale_home_summaries"] == 1 and not projection["remote_issue_coverage"]["complete"]
remote_row = next(row for row in projection["rows"] if row["url"] == issue_url)
assert remote_row["tasks"][0]["task_state"] == "working"
assert remote_row["tasks"][0]["stage"] == "Implementing"
assert remote_row["tasks"][0]["ready_for_approval"] == "unknown"
assert remote_row["tasks"][0]["source_head"] == "b" * 40 and remote_row["tasks"][0]["verification"]["full"]["status"] == "passed"
assert not any("stale" in conflict for conflict in remote_row["tasks"][0]["conflicts"])
page_task_a = {"id": "page-a", "current_state": {"state": "working"}, "backlog": {"repo": "alpha", "links": []}}
page_task_b = {"id": "page-b", "current_state": {"state": "done"}, "backlog": {"repo": "alpha", "links": []}}
page_summary = {"counts": {"issue_tasks": 2}, "issue_tasks": [page_task_a], "issue_inventory": {"complete": True, "total": 2, "page_size": 1, "page_count": 2, "next_offset": None, "pages": [{"offset": 0, "next_offset": 1, "tasks": [page_task_a]}, {"offset": 1, "next_offset": None, "tasks": [page_task_b]}]}}
page_rows, page_complete = issues.issue_inventory(page_summary)
assert page_complete and [item["id"] for item in page_rows] == ["page-a", "page-b"]
page_summary["issue_inventory"]["pages"][1]["offset"] = 0
assert issues.issue_inventory(page_summary) == ([], False), "invalid continuation offsets must not claim complete descendant coverage"

# The semantic reducer is pure and shared by every row. Poll/read metadata and
# task ordering do not enter the relevant status fingerprint.
from fm_issues_derive import choose_change, owner_task_fact, semantic_fingerprint, task_rank, transition_watermark
row_task = {**task, "stage": "In review", "waiting": None, "next_step": "Review required", "ready_for_approval": "not ready"}
row = {"url": issue_url, "forge_state": "open", "title": "Review", "stage": "In review", "conflicts": [], "tasks": [row_task]}
unlinked_task = {"id": "other", "generation": "gen-1", "stage": "Queued", "waiting": None, "next_step": "Queued", "backlog": {"state": "queued"}, "prs": [], "verification": {}}
first_fp = semantic_fingerprint([row], [unlinked_task])
read_only_copy = {**row, "tasks": [{**row_task, "last_checked_epoch": 12345, "cache_age_seconds": 9}]}
assert semantic_fingerprint([read_only_copy], [unlinked_task]) == first_fp
assert task_rank({**row_task, "id": "decision", "waiting": "your decision"}) < task_rank({**row_task, "id": "ready", "stage": "Ready for approval"})
common_fact = {"id": "same", "kind": "ship", "task_state": "working", "current_state": {"state": "working"}, "backlog": {"state": "in_flight"}, "prs": [], "verification": {}}
local_fact = owner_task_fact(common_fact, "main-home", "same", [])
descendant_fact = owner_task_fact({**common_fact, "evidence_freshness": "fresh"}, "child", "same", [])
assert (local_fact["stage"], local_fact["waiting"], local_fact["next_step"], local_fact["ready_for_approval"]) == (descendant_fact["stage"], descendant_fact["waiting"], descendant_fact["next_step"], descendant_fact["ready_for_approval"])
assert local_fact["fact_schema"] == descendant_fact["fact_schema"] == "fm-issue-task-fact.v1"
assert choose_change([{"class": "event", "at_epoch": 123}], {"from_epoch": 100, "to_epoch": 150}) == {"class": "event", "at_epoch": 123}
assert choose_change([], {"from_epoch": 100, "to_epoch": 150}) == {"class": "detected", "from_epoch": 100, "to_epoch": 150}
assert choose_change([], {"from_epoch": 100, "to_epoch": 90000}) == {"class": "unknown"}
transition_a = {"schema": "fm-task-event.v1", "generation": "gen-1", "class": "event", "at_epoch": 10, "kind": "started", "fields": {"kind": "ship"}}
transition_b = {"schema": "fm-task-event.v1", "generation": "gen-1", "class": "event", "at_epoch": 20, "kind": "held", "fields": {"source": "owner"}}
watermark_a = transition_watermark([], [{**common_fact, "generation": "gen-1", "event_history": [transition_a]}])
watermark_aba = transition_watermark([], [{**common_fact, "generation": "gen-1", "event_history": [transition_a, transition_b, {**transition_a, "at_epoch": 30}]}])
assert watermark_a != watermark_aba, "retained owner transitions must distinguish an A-B-A history"
from fm_lane_receipts import classify_finished_receipt
finished = {"generation": "gen-1", "signal": None, "received_signal": None, "exit_code": 0, "dirty_before": False, "dirty_after": False, "head_before": "a" * 40, "head_after": "a" * 40}
assert classify_finished_receipt(finished, "gen-1", "a" * 40) == "passed"
assert classify_finished_receipt({**finished, "generation": "old"}, "gen-1", "a" * 40) == "unknown"
assert classify_finished_receipt({**finished, "dirty_after": True}, "gen-1", "a" * 40) == "passed-dirty"
assert classify_finished_receipt({**finished, "exit_code": None}, "gen-1", "a" * 40) == "unknown"

# A lane-specific validated receipt-write error remains unknown even when the
# first write failed before any start receipt could be retained.
(home / "config" / "project-lanes.json").write_text(json.dumps({"alpha": {"full": ["/bin/echo", "configured"]}}))
receipt_task = "receipt-task"
receipt_data = home / "data" / receipt_task
receipt_dir = receipt_data / "lane-receipts"
receipt_dir.mkdir(parents=True)
(receipt_dir / ".instrumented").write_text("gen-7\n")
(receipt_dir / ".errors").write_text(f"{int(time.time())} task={receipt_task} generation=gen-7 lane=full atomic receipt write failed\n")
lane_task = {"id": receipt_task, "spawn_gen": "gen-7", "paths": {"worktree": {"path": str(clone)}}}
lane = issues.lane_status(home, lane_task, "alpha")
assert lane["full"]["status"] == "unknown" and lane["full"]["error"] == "atomic receipt write failed", lane
print("pass: canonical issue identity, ready predicate, multiple wait facts, and UTC/PT timestamp classes")
PY
