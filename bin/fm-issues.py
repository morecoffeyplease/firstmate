#!/usr/bin/env python3
"""Local deterministic project issue table and status-summary request owner."""

from __future__ import annotations

import argparse
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from fm_project_lanes import read_project_lanes

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "fm-issues.v1"
FP_SCHEMA = "fm-issues-fingerprint.v1"
MAX_CATALOG_AGE = 3600
MAX_OBSERVATION_BRACKET = 86400
COLLECT_LOCK = threading.Lock()


def utc_now() -> int:
    return int(time.time())


def iso_utc(epoch: int | float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def pt(epoch: int | float | None) -> str:
    if epoch is None:
        return "unknown"
    value = datetime.fromtimestamp(epoch, ZoneInfo("America/Los_Angeles"))
    return value.strftime("%Y-%m-%d %I:%M:%S %p %Z")


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(5)}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path, max_bytes: int = 20_000_000) -> object | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
            return None
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def projects(home: Path) -> dict[str, dict[str, str]]:
    registry = home / "data" / "projects.md"
    found: dict[str, dict[str, str]] = {}
    try:
        for raw in registry.read_text(encoding="utf-8").splitlines():
            fields = raw.split()
            if len(fields) >= 4 and fields[0] == "-" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", fields[1]):
                found[fields[1]] = {"name": fields[1], "description": raw}
    except OSError:
        pass
    return found


def canonical_repo(home: Path, project: str) -> tuple[str | None, str | None]:
    clone = home / "projects" / project
    if not clone.is_dir() or clone.is_symlink():
        return None, "registered project clone is unavailable"
    try:
        origin = subprocess.run(
            ["git", "-C", str(clone), "config", "--get", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, "registered project origin is unavailable"
    check = subprocess.run(
        ["/bin/bash", "-c", 'source "$1"; fm_project_origin_safe "$2"', "fm-origin", str(ROOT / "bin" / "fm-project-origin-lib.sh"), origin],
        capture_output=True,
        timeout=3,
    )
    if check.returncode != 0:
        return None, "registered project origin failed validation"
    parsed = urllib.parse.urlparse(origin)
    host = parsed.hostname
    path = parsed.path.strip("/")
    if origin.startswith("git@") and ":" in origin:
        host = origin.split("@", 1)[1].split(":", 1)[0]
        path = origin.split(":", 1)[1].strip("/")
    path = path.removesuffix(".git")
    if host == "github.com" and len(path.split("/")) == 2:
        return path, None
    return None, "registered origin is not a supported GitHub repository"


def run_snapshot(home: Path) -> tuple[dict | None, str | None]:
    env = os.environ.copy()
    env["FM_HOME"] = str(home)
    env.pop("FM_ROOT_OVERRIDE", None)
    try:
        result = subprocess.run(
            [str(ROOT / "bin" / "fm-fleet-snapshot.sh"), "--json"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            return None, result.stderr[-1000:] or f"snapshot exited {result.returncode}"
        value = json.loads(result.stdout)
        if value.get("schema") != "fm-fleet-snapshot.v1":
            return None, "unsupported fleet snapshot schema"
        return value, None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, str(exc)


def catalog(home: Path, project: str, repo: str, refresh: bool) -> dict:
    digest = hashlib.sha256(repo.lower().encode()).hexdigest()
    path = home / "state" / "issue-catalog" / f"{digest}.json"
    previous = read_json(path)
    now = utc_now()
    if isinstance(previous, dict) and (previous.get("schema") != "fm-issue-catalog.v1" or not isinstance(previous.get("issues"), list) or not isinstance(previous.get("known"), int)):
        previous = None
    if not refresh and isinstance(previous, dict) and previous.get("repo") == repo and now - int(previous.get("checked_epoch", 0)) < MAX_CATALOG_AGE:
        return previous
    last_attempt = int(previous.get("last_attempt_epoch", previous.get("checked_epoch", 0))) if isinstance(previous, dict) else 0
    if isinstance(previous, dict) and previous.get("repo") == repo and now - last_attempt < 60:
        stale = dict(previous)
        stale["throttled"] = True
        return stale
    command = [str(ROOT / "bin" / "fm-contributions.sh"), "catalog", repo]
    env = os.environ.copy()
    env["FM_HOME"] = str(home)
    env.pop("FM_ROOT_OVERRIDE", None)
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env, cwd=ROOT, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr[-500:] or f"catalog reader exited {result.returncode}")
        parsed = json.loads(result.stdout)
        if not isinstance(parsed, dict) or parsed.get("schema") != "fm-issue-catalog.v1" or parsed.get("repository") != repo or parsed.get("complete") is not True or not isinstance(parsed.get("issues"), list):
            raise RuntimeError("catalog reader returned an invalid or partial schema")
        issues = []
        for item in parsed["issues"]:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            title = item.get("title")
            url = item.get("url")
            state = item.get("state")
            if not isinstance(number, int) or not isinstance(title, str) or not isinstance(url, str) or state not in ("open", "closed"):
                continue
            updated = item.get("updated_at")
            try:
                updated_epoch = int(datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp())
            except (TypeError, ValueError):
                updated_epoch = None
            issues.append({"number": number, "title": title, "url": url, "state": state, "updated_at": updated, "updated_epoch": updated_epoch})
        issues.sort(key=lambda row: row["number"])
        checked_epoch = now
        try:
            checked_epoch = int(datetime.fromisoformat(parsed["observed_at"].replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            pass
        value = {"schema": "fm-issue-catalog.v1", "repo": repo, "checked_epoch": checked_epoch, "complete": True, "known": len(issues), "issues": issues, "error": None, "stale": False}
        atomic_json(path, value)
        return value
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        if isinstance(previous, dict) and previous.get("repo") == repo:
            stale = dict(previous)
            stale["error"] = str(exc)
            stale["stale"] = True
            stale["last_attempt_epoch"] = now
            atomic_json(path, stale)
            return stale
        value = {"schema": "fm-issue-catalog.v1", "repo": repo, "checked_epoch": None, "complete": False, "known": 0, "issues": [], "error": str(exc), "stale": False, "last_attempt_epoch": now}
        atomic_json(path, value)
        return value


def task_links(task: dict, repo: str | None) -> list[str]:
    backlog = task.get("backlog") or {}
    links = backlog.get("links") or []
    return canonical_issue_urls(links, repo)


def canonical_issue_urls(links: list, repo: str | None) -> list[str]:
    prefix = f"https://github.com/{repo}/issues/" if isinstance(repo, str) else None
    return [link for link in links if isinstance(link, str) and prefix and link.startswith(prefix) and re.fullmatch(re.escape(prefix) + r"[1-9][0-9]*", link)]


def stage(task: dict, linked_prs: list[dict], backlog: dict | None) -> tuple[str, str | None]:
    current = task.get("current_state", {}).get("state")
    if backlog and backlog.get("hold_bucket") == "live":
        return "Unknown", "your decision"
    if backlog and backlog.get("state") == "queued":
        return "Queued", "prerequisite" if backlog.get("blocked_by") else None
    if backlog and backlog.get("blocked_by"):
        return "Unknown", "blocked"
    if linked_prs:
        if any(pr.get("state") == "merged" for pr in linked_prs):
            return "Completed", None
        if any(pr.get("merge_authority") for pr in linked_prs):
            return "Merging", None
        if any(pr.get("review_decision") == "CHANGES_REQUESTED" or pr.get("failed_checks", 0) for pr in linked_prs):
            return "Revising", None
    if task.get("kind") == "scout" and current in ("working", "busy"):
        return "Investigating", None
    if task.get("kind") == "ship" and current in ("working", "busy", "paused"):
        return "Implementing", "paused" if current == "paused" else None
    if linked_prs:
        return "In review", None
    return "Unknown", None


def next_step(task: dict) -> str:
    waiting = task.get("waiting")
    backlog = task.get("backlog") or {}
    if waiting == "your decision":
        return f"Waiting on your decision: {backlog.get('hold_reason') or task.get('id')}"
    if waiting == "prerequisite":
        return f"Queued behind {backlog.get('blocked_by')}"
    if waiting == "blocked":
        return f"Blocked by {backlog.get('blocked_by') or 'untyped blocker'}"
    if waiting == "paused":
        return "Paused"
    if task.get("task_state") == "failed":
        return "Failed; next step not recorded"
    if task.get("stage") == "Queued":
        return "Queued"
    return "Next step not recorded"


def ready_state(task: dict, configured: dict[str, list[str]]) -> str:
    if task.get("waiting") or task.get("stage") in ("Queued", "Unknown", "Implementing", "Investigating"):
        return "not ready"
    prs = task.get("prs", [])
    if not prs:
        return "not ready"
    for pr in prs:
        if pr.get("state") in (None, "unknown") or pr.get("draft") is None or pr.get("checks") is None:
            return "unknown"
        if pr.get("state") != "open" or pr.get("draft") or pr.get("review_decision") == "CHANGES_REQUESTED":
            return "not ready"
        checks = pr.get("checks", [])
        if not checks or any(check.get("status") != "completed" or check.get("conclusion") not in ("success", "skipped", "neutral") for check in checks):
            return "not ready"
    lanes = read_project_lanes_for_ready(configured)
    if any(task.get("verification", {}).get(lane, {}).get("status") != "passed" for lane in lanes):
        return "not ready"
    return "ready for approval"


def read_project_lanes_for_ready(configured: dict[str, list[str]]) -> list[str]:
    return [name for name in ("full", "verify") if name in configured]


def fingerprint_pr(pr: dict) -> dict:
    checks = pr.get("checks")
    latest = {}
    for check in checks if isinstance(checks, list) else []:
        name = check.get("name")
        if not isinstance(name, str):
            continue
        prior = latest.get(name)
        candidate_key = (str(check.get("started_at") or ""), int(check.get("id") or 0))
        prior_key = (str(prior.get("started_at") or ""), int(prior.get("id") or 0)) if prior else None
        if prior is None or candidate_key > prior_key:
            latest[name] = check
    return {"url": pr.get("url"), "state": pr.get("state"), "head": pr.get("head"), "draft": pr.get("draft"), "review_decision": pr.get("review_decision"), "checks": [{"name": name, "status": value.get("status"), "conclusion": value.get("conclusion")} for name, value in sorted(latest.items())]}


def fingerprint_task(task: dict) -> dict:
    event = task.get("event")
    event_value = None
    if event:
        event_value = {"class": event.get("class"), "kind": event.get("kind"), "fields": {key: value for key, value in event.get("fields", {}).items() if key not in ("from_epoch", "to_epoch")}}
    return {
        "id": task.get("id"),
        "generation": task.get("generation"),
        "kind": task.get("kind"),
        "task_state": task.get("task_state"),
        "stage": task.get("stage"),
        "waiting": task.get("waiting"),
        "next_step": task.get("next_step"),
        "backlog_state": (task.get("backlog") or {}).get("state"),
        "blocked_by": (task.get("backlog") or {}).get("blocked_by"),
        "hold_reason": (task.get("backlog") or {}).get("hold_reason"),
        "conflicts": sorted(task.get("conflicts", [])),
        "prs": sorted((fingerprint_pr(pr) for pr in task.get("prs", [])), key=lambda pr: pr.get("url") or ""),
        "verification": {lane: task.get("verification", {}).get(lane, {}).get("status") for lane in ("focused", "full", "verify")},
        "ready_for_approval": task.get("ready_for_approval"),
        "event": event_value,
    }


def process_start_matches(pid: int, expected: str | None) -> bool:
    if not expected:
        return False
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return result.returncode == 0 and result.stdout.strip() == expected
    except (OSError, subprocess.SubprocessError):
        return False


def lane_status(home: Path, task: dict, project: str) -> dict[str, dict]:
    configured = read_project_lanes(home).get(project, {})
    task_id = task.get("id")
    generation = task.get("spawn_gen") or task.get("generation")
    result = {lane: {"status": "not instrumented", "basis": "receipts from wrapped runs; unwrapped runs are invisible"} for lane in ("focused", "full", "verify")}
    if not configured or not generation or not task_id:
        return result
    receipt_dir = home / "data" / task_id / "lane-receipts"
    if receipt_dir.is_symlink():
        return {lane: {"status": "unknown", "basis": "receipt directory identity is unsafe"} for lane in ("focused", "full", "verify")}
    marker = receipt_dir / ".instrumented"
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 128:
            return result
        if marker.read_text(encoding="utf-8").strip() != generation:
            return {lane: {"status": "unknown", "basis": "launch generation conflicts with the current task generation"} for lane in ("focused", "full", "verify")}
    except OSError:
        return result
    result["focused"] = {"status": "not run", "basis": "receipts from wrapped runs; unwrapped runs are invisible"}
    for name in ("full", "verify"):
        if name in configured:
            result[name] = {"status": "not run", "basis": "receipts from wrapped runs; unwrapped runs are invisible"}
    if receipt_dir.is_symlink() or not receipt_dir.is_dir():
        return result
    worktree = (task.get("paths") or {}).get("worktree", {}).get("path")
    current_head = None
    if isinstance(worktree, str) and worktree:
        try:
            current_head = subprocess.run(["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    records = []
    for path in receipt_dir.glob("*.json"):
        value = read_json(path, 2_000_000)
        if isinstance(value, dict) and value.get("schema") == "fm-lane-receipt.v1" and value.get("phase") in ("start", "finish"):
            records.append(value)
    focused_order = -1
    for lane in ("focused", "full", "verify"):
        matches = [item for item in records if item.get("lane") == lane]
        matches.sort(key=lambda item: int(item.get("started_order_ns", item.get("started_epoch", 0))), reverse=True)
        chosen = matches[0] if matches else None
        if not chosen:
            continue
        if chosen.get("generation") != generation:
            state = "unknown"
        elif chosen.get("phase") == "start" or "ended_epoch" not in chosen:
            pid = chosen.get("pid")
            try:
                live = isinstance(pid, int) and process_start_matches(pid, chosen.get("process_start"))
                if live:
                    os.kill(pid, 0)
            except (OSError, ProcessLookupError):
                live = False
            state = "running" if live else "interrupted"
        elif chosen.get("signal") is not None:
            state = "canceled"
        elif chosen.get("exit_code") != 0:
            state = "failed"
        elif chosen.get("dirty_before") is True or chosen.get("dirty_after") is True or chosen.get("head_before") != chosen.get("head_after"):
            state = "passed on uncommitted or changed source"
        elif current_head and chosen.get("head_after") == current_head:
            state = "passed"
        else:
            state = "stale (passed on an older head)"
        order = int(chosen.get("started_order_ns", chosen.get("started_epoch", 0)))
        if lane in ("full", "verify") and chosen.get("argv") != configured.get(lane):
            state = "reclassified focused run"
            if order > focused_order:
                result["focused"] = {"status": state, "receipt": chosen.get("id"), "started_epoch": chosen.get("started_epoch"), "ended_epoch": chosen.get("ended_epoch"), "argv": chosen.get("argv"), "artifact": chosen.get("artifact_record"), "log": chosen.get("log", "not retained"), "basis": "receipts from wrapped runs; unwrapped runs are invisible"}
                focused_order = order
            continue
        detail = {"status": state, "receipt": chosen.get("id"), "started_epoch": chosen.get("started_epoch"), "ended_epoch": chosen.get("ended_epoch"), "argv": chosen.get("argv"), "artifact": chosen.get("artifact_record"), "log": chosen.get("log", "not retained"), "basis": "receipts from wrapped runs; unwrapped runs are invisible"}
        if lane == "focused":
            result[lane] = detail
            focused_order = order
        else:
            result[lane] = detail
    return result


def task_event_fact(home: Path, task_id: str | None, generation: str | None) -> dict | None:
    if not task_id or not generation:
        return None
    path = home / "data" / task_id / "events.jsonl"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_194_304:
        return None
    latest = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item.get("schema") != "fm-task-event.v1" or item.get("task") != task_id or item.get("generation") != generation:
                continue
            if item.get("class") not in ("event", "detected") or not isinstance(item.get("fields"), dict):
                continue
            latest = item
    except (OSError, ValueError):
        return None
    return latest


def _make_projection(home: Path, project: str, refresh: bool = False) -> dict:
    routes = projects(home)
    if project not in routes:
        raise ValueError(f"project is not registered: {project}")
    repo, repo_error = canonical_repo(home, project)
    cache_dir = home / "state" / "issue-status"
    if cache_dir.is_symlink():
        raise ValueError("issue status cache directory is a symlink")
    cache_dir.mkdir(parents=True, exist_ok=True)
    snapshot_file = cache_dir / "fleet.json"
    checked = read_json(snapshot_file)
    if isinstance(checked, dict) and (checked.get("schema") != "fm-issue-fleet-cache.v1" or not isinstance(checked.get("snapshot"), (dict, type(None)))):
        checked = None
    now = utc_now()
    last_snapshot_attempt = int(checked.get("last_attempt_epoch", checked.get("collected_epoch", 0))) if isinstance(checked, dict) else 0
    if not isinstance(checked, dict) or now - last_snapshot_attempt >= 60:
        fresh, error = run_snapshot(home)
        if fresh is not None:
            checked = {"schema": "fm-issue-fleet-cache.v1", "collected_epoch": now, "snapshot": fresh, "error": None}
            atomic_json(snapshot_file, checked)
        elif not isinstance(checked, dict):
            checked = {"schema": "fm-issue-fleet-cache.v1", "collected_epoch": None, "last_attempt_epoch": now, "snapshot": None, "error": error, "stale": False}
            atomic_json(snapshot_file, checked)
        else:
            checked = dict(checked)
            checked["error"] = error
            checked["last_attempt_epoch"] = now
            checked["stale"] = True
            atomic_json(snapshot_file, checked)
    snap = checked.get("snapshot") if isinstance(checked, dict) else None
    candidates = []
    if isinstance(snap, dict):
        contribution_rows = (snap.get("contributions") or {}).get("rows", [])
        configured_lanes = read_project_lanes(home).get(project, {})
        for task in snap.get("tasks", []):
            repo_name = (task.get("backlog") or {}).get("repo") or task.get("project")
            if repo_name != project or task.get("kind") == "secondmate":
                continue
            pr_url = (task.get("pr") or {}).get("url")
            backlog = task.get("backlog")
            linked = []
            if isinstance(pr_url, str) and "/pull/" in pr_url:
                contribution = next((item for item in contribution_rows if item.get("url") == pr_url), None)
                forge = contribution.get("forge") if isinstance(contribution, dict) else None
                forge = forge if isinstance(forge, dict) else {}
                checks = forge.get("checks") if isinstance(forge.get("checks"), list) else None
                failed_checks = sum(1 for check in checks or [] if check.get("status") == "completed" and check.get("conclusion") not in (None, "success", "skipped", "neutral"))
                linked.append({"url": pr_url, "state": forge.get("state", "unknown"), "head": forge.get("head") or (task.get("pr") or {}).get("head"), "draft": forge.get("draft"), "review_decision": forge.get("review_decision"), "mergeable": forge.get("mergeable"), "checks": checks, "checked_at": forge.get("checked_at"), "merge_authority": task.get("merge_authority"), "failed_checks": failed_checks, "association": "task-linked; issue-specific PR relation not recorded"})
            task_id = task.get("id")
            generation = task.get("spawn_gen")
            task_row = {"id": task_id, "generation": generation, "kind": task.get("kind"), "task_state": task.get("current_state", {}).get("state", "unknown"), "backlog": backlog, "issues": task_links(task, repo), "prs": linked, "verification": lane_status(home, task, project), "event": task_event_fact(home, task_id, generation)}
            task_row["stage"], task_row["waiting"] = stage(task, linked, backlog)
            task_row["next_step"] = next_step(task_row)
            task_row["ready_for_approval"] = ready_state(task_row, configured_lanes)
            if task_row["ready_for_approval"] == "ready for approval":
                task_row["stage"] = "Ready for approval"
            task_row["source_accepted"] = "not recorded"
            task_row["canonical_journeys"] = "not recorded"
            candidates.append(task_row)
        for row in (snap.get("backlog") or {}).get("records", []):
            if row.get("repo") == project and row.get("state") in ("queued", "in_flight", "done") and not any(item["id"] == row.get("id") for item in candidates):
                if row.get("state") == "done":
                    artifact = row.get("pr_url") or row.get("report_path") or row.get("local_note")
                    stage_name = "Completed" if artifact and (row.get("completion") or {}).get("verb") in ("merged", "reported", "done") else "Closed without delivery"
                    candidates.append({"id": row.get("id"), "generation": None, "kind": row.get("kind"), "task_state": "done", "backlog": row, "issues": canonical_issue_urls(row.get("links", []), repo), "prs": ([{"url": row.get("pr_url"), "state": "merged" if (row.get("completion") or {}).get("verb") == "merged" else "unknown", "association": "backlog artifact"}] if row.get("pr_url") else []), "stage": stage_name, "waiting": None, "next_step": "Completed" if stage_name == "Completed" else "Closed without delivery", "verification": {lane: {"status": "not instrumented"} for lane in ("focused", "full", "verify")}, "ready_for_approval": "not ready", "source_accepted": "not recorded", "canonical_journeys": "not recorded", "event": None})
                    continue
                orphan = row.get("state") == "in_flight"
                candidates.append({"id": row.get("id"), "generation": None, "kind": row.get("kind"), "task_state": "unknown" if orphan else "queued", "backlog": row, "issues": canonical_issue_urls(row.get("links", []), repo), "prs": [], "stage": "Unknown" if orphan else "Queued", "waiting": row.get("blocked_by") and "prerequisite" or None, "next_step": "Next step not recorded" if orphan else (f"Queued behind {row.get('blocked_by')}" if row.get("blocked_by") else "Queued"), "conflicts": ["backlog is in flight but task metadata is missing"] if orphan else [], "verification": {lane: {"status": "not instrumented"} for lane in ("focused", "full", "verify")}, "ready_for_approval": "not ready", "source_accepted": "not recorded", "canonical_journeys": "not recorded", "event": None})
    cat = {"schema": "fm-issue-catalog.v1", "repo": None, "checked_epoch": None, "complete": False, "known": 0, "issues": [], "error": repo_error, "stale": False}
    if repo:
        cat = catalog(home, project, repo, refresh)
    by_url: dict[str, list[dict]] = {}
    for task in candidates:
        for url in task["issues"]:
            by_url.setdefault(url, []).append(task)
    for issue in cat.get("issues", []):
        by_url.setdefault(issue["url"], [])
    rows = []
    for url, linked in sorted(by_url.items()):
        linked.sort(key=lambda task: task.get("id") or "")
        issue = next((item for item in cat.get("issues", []) if item["url"] == url), None)
        if issue is None:
            rows.append({"url": url, "number": None, "title": None, "forge_state": "unavailable", "visibility": "known identity unavailable", "tasks": linked, "stage": "Unknown", "changed": {"class": "unknown"}})
            continue
        def actionable_rank(task: dict) -> tuple[int, int]:
            if task.get("waiting") == "your decision":
                return (0, 0)
            if task.get("waiting") in ("blocked", "failed") or task.get("task_state") == "failed":
                return (1, 0)
            if task.get("stage") == "Ready for approval":
                return (2, 0)
            order = {"Investigating": 3, "Implementing": 4, "In review": 5, "Merging": 6, "Queued": 7, "Completed": 8}
            return (order.get(task.get("stage"), 1), 0)
        chosen_task = min(linked, key=actionable_rank) if linked else None
        chosen = chosen_task["stage"] if chosen_task else ("Unstarted" if issue["state"] == "open" else "Closed without delivery")
        conflicts = [conflict for task in linked for conflict in task.get("conflicts", [])]
        if issue["state"] == "closed" and any(task.get("task_state") in ("working", "busy", "paused", "running") for task in linked):
            conflicts.append("forge issue is closed while local task remains active")
        rows.append({**issue, "forge_state": issue["state"], "tasks": linked, "task_count": len(linked), "pr_count": sum(len(task.get("prs", [])) for task in linked), "conflicts": conflicts, "freshness": "conflicting" if conflicts else ("stale" if cat.get("stale") else "current"), "stage": chosen, "next_step": chosen_task.get("next_step") if chosen_task else ("No recorded Firstmate work" if issue["state"] == "open" else "Closed; delivery not recorded"), "changed": {"class": "unknown"}})
    unlinked = sorted((task for task in candidates if not task["issues"]), key=lambda task: task.get("id") or "")
    state_cache = home / "state" / "issue-status" / f"{hashlib.sha256(project.encode()).hexdigest()}.json"
    previous = read_json(state_cache)
    relevant_rows = [{"url": row["url"], "state": row.get("forge_state"), "title": row.get("title"), "stage": row.get("stage"), "conflicts": sorted(row.get("conflicts", [])), "tasks": [fingerprint_task(task) for task in row.get("tasks", [])]} for row in rows]
    relevant_rows.sort(key=lambda value: value["url"])
    fingerprint = hashlib.sha256(json.dumps({"schema": FP_SCHEMA, "rows": relevant_rows, "unlinked_tasks": [fingerprint_task(task) for task in unlinked]}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous_fp = previous.get("fingerprint") if isinstance(previous, dict) else None
    previous_seen = previous.get("observed_epoch") if isinstance(previous, dict) else None
    changed = {"class": "unknown"}
    if previous_fp and previous_fp != fingerprint and previous_seen and now - int(previous_seen) <= MAX_OBSERVATION_BRACKET:
        changed = {"class": "detected", "from_epoch": int(previous_seen), "to_epoch": now}
    elif previous_fp == fingerprint:
        changed = previous.get("changed", changed)
    prior_rows = previous.get("rows", {}) if isinstance(previous, dict) and isinstance(previous.get("rows"), dict) else {}
    next_rows = {}
    for row in rows:
        row_value = {"url": row["url"], "state": row.get("forge_state"), "title": row.get("title"), "stage": row.get("stage"), "conflicts": sorted(row.get("conflicts", [])), "tasks": [fingerprint_task(task) for task in row.get("tasks", [])]}
        row_fingerprint = hashlib.sha256(json.dumps(row_value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        old = prior_rows.get(row["url"], {})
        row_changed = {"class": "unknown"}
        if old.get("fingerprint") == row_fingerprint:
            row_changed = old.get("changed", row_changed)
        elif old.get("fingerprint") and now - int(old.get("observed_epoch", 0)) <= MAX_OBSERVATION_BRACKET:
            if old.get("title") != row.get("title") or old.get("state") != row.get("forge_state"):
                row_changed = {"class": "detected", "from_epoch": int(old.get("observed_epoch", 0)), "to_epoch": now} if old.get("observed_epoch") else {"class": "unknown"}
            else:
                old_events = old.get("events", {})
                current_events = {task.get("id"): task.get("event") for task in row.get("tasks", []) if task.get("event")}
                changed_events = [event for task_id, event in current_events.items() if old_events.get(task_id) != event]
                event_stamps = [event for event in changed_events if event.get("class") == "event" and isinstance(event.get("at_epoch"), int)]
                detected = [event for event in changed_events if event.get("class") == "detected" and isinstance(event.get("fields", {}).get("from_epoch"), int) and isinstance(event.get("fields", {}).get("to_epoch"), int)]
                if event_stamps:
                    newest = max(event_stamps, key=lambda event: event["at_epoch"])
                    row_changed = {"class": "event", "at_epoch": newest["at_epoch"]}
                elif detected:
                    newest = max(detected, key=lambda event: event["fields"]["to_epoch"])
                    if newest["fields"]["to_epoch"] - newest["fields"]["from_epoch"] <= MAX_OBSERVATION_BRACKET:
                        row_changed = {"class": "detected", "from_epoch": newest["fields"]["from_epoch"], "to_epoch": newest["fields"]["to_epoch"]}
                else:
                    row_changed = {"class": "detected", "from_epoch": int(old.get("observed_epoch", 0)), "to_epoch": now}
        row["changed"] = row_changed
        next_rows[row["url"]] = {"fingerprint": row_fingerprint, "observed_epoch": now, "title": row.get("title"), "state": row.get("forge_state"), "changed": row_changed, "events": {task.get("id"): task.get("event") for task in row.get("tasks", []) if task.get("event")}}
    if previous_fp != fingerprint or prior_rows != next_rows:
        atomic_json(state_cache, {"schema": FP_SCHEMA, "fingerprint": fingerprint, "observed_epoch": now, "changed": changed, "rows": next_rows})
    summary_path = home / "state" / "status-summary" / "summaries" / f"{hashlib.sha256(project.encode()).hexdigest()}.json"
    summary_file = read_json(summary_path, 1_000_000)
    summary = summary_file.get("summaries", [])[-1] if isinstance(summary_file, dict) and isinstance(summary_file.get("summaries"), list) and summary_file["summaries"] else None
    if isinstance(summary, dict) and summary.get("schema") == "fm-status-summary.v1":
        summary = dict(summary)
        summary["state"] = "written" if summary.get("basis_fingerprint") == fingerprint else "outdated"
    else:
        summary = None
    beat = home / "state" / ".last-watcher-beat"
    try:
        beat_epoch = int(beat.stat().st_mtime) if beat.is_file() and not beat.is_symlink() else None
    except OSError:
        beat_epoch = None
    session_lock = home / "state" / ".lock"
    supervisor = {"watcher_beat_epoch": beat_epoch, "watcher_age_seconds": now - beat_epoch if beat_epoch else None, "session_lock_present": session_lock.exists()}
    return {"schema": SCHEMA, "project": project, "repository": repo, "generated_epoch": now, "last_checked_epoch": cat.get("checked_epoch"), "catalog": {key: cat.get(key) for key in ("complete", "known", "error", "stale", "checked_epoch", "last_attempt_epoch", "throttled")}, "snapshot": {"collected_epoch": checked.get("collected_epoch"), "error": checked.get("error"), "stale": checked.get("stale", False)}, "supervisor": supervisor, "fingerprint_schema": FP_SCHEMA, "fingerprint": fingerprint, "project_changed": changed, "rows": rows, "unlinked_tasks": unlinked, "summary_requests": list_summary_requests(home, project), "summary": summary}


def make_projection(home: Path, project: str, refresh: bool = False) -> dict:
    state = home / "state"
    if state.is_symlink() or not state.is_dir():
        raise ValueError("operational state directory is unavailable")
    lock_path = state / ".issue-status.lock"
    if lock_path.is_symlink():
        raise ValueError("issue status lock is a symlink")
    with COLLECT_LOCK:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            return _make_projection(home, project, refresh)
        finally:
            os.close(fd)


def list_summary_requests(home: Path, project: str | None = None) -> list[dict]:
    root = home / "state" / "status-summary" / "requests"
    result = []
    if not root.is_dir() or root.is_symlink():
        return result
    for path in root.glob("*.json"):
        value = read_json(path, 1_000_000)
        if not isinstance(value, dict) or value.get("schema") != "fm-status-summary-request.v1":
            continue
        if project is None or project in value.get("projects", []):
            value = dict(value)
            if value.get("state") == "pending" and int(value.get("expires_epoch", 0)) <= utc_now():
                value["state"] = "expired"
            availability = supervisor_availability(home)
            value["supervisor_availability"] = availability
            value["display_state"] = "unavailable" if value.get("state") == "pending" and availability == "unavailable" else value.get("state")
            result.append(value)
    return sorted(result, key=lambda item: item.get("requested_epoch", 0), reverse=True)


def supervisor_availability(home: Path) -> str:
    lock = home / "state" / ".lock"
    if lock.is_symlink():
        return "unknown"
    try:
        pid_text = lock.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return "unavailable"
    except OSError:
        return "unknown"
    if not pid_text.isdecimal() or len(pid_text) > 10:
        return "unknown"
    try:
        os.kill(int(pid_text), 0)
        beat = home / "state" / ".last-watcher-beat"
        if beat.is_symlink() or not beat.is_file():
            return "unknown"
        age = utc_now() - int(beat.stat().st_mtime)
        return "available" if 0 <= age <= 60 else "unknown"
    except ProcessLookupError:
        return "unavailable"
    except (PermissionError, OSError):
        return "unknown"


def summary_command(home: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fm-status-summary")
    sub = parser.add_subparsers(dest="command", required=True)
    request = sub.add_parser("request")
    request.add_argument("--project", action="append", required=True)
    put = sub.add_parser("put")
    put.add_argument("request_id")
    put.add_argument("project")
    put.add_argument("--basis-fingerprint", required=True)
    put.add_argument("--basis-observed-at", type=int, required=True)
    put.add_argument("--author", required=True)
    put.add_argument("--text-file", required=True)
    sub.add_parser("list")
    args = parser.parse_args(argv)
    registered = projects(home)
    root = home / "state" / "status-summary"
    if root.is_symlink():
        print("error: status summary state is a symlink", file=sys.stderr)
        return 2
    reqdir = root / "requests"
    sumdir = root / "summaries"
    root.mkdir(parents=True, exist_ok=True)
    if reqdir.is_symlink() or sumdir.is_symlink():
        print("error: status summary child directory is a symlink", file=sys.stderr)
        return 2
    import fcntl
    lock_fd = os.open(root / ".writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    if args.command == "request":
        selected = sorted(set(args.project))
        if not selected or any(name not in registered for name in selected):
            print("error: request must select registered projects", file=sys.stderr)
            return 2
        projections = {}
        for name in selected:
            projections[name] = make_projection(home, name)
        now = utc_now()
        open_requests = list_summary_requests(home)
        existing_by_project = {}
        for item in open_requests:
            if item.get("state") != "pending" or now - int(item.get("requested_epoch", 0)) >= 1800:
                continue
            for name in item.get("projects", []):
                existing_by_project.setdefault(name, item)
        attached = {name: existing_by_project[name]["id"] for name in selected if name in existing_by_project}
        selected = [name for name in selected if name not in existing_by_project]
        availability = supervisor_availability(home)
        if not selected:
            print(json.dumps({"status": "pending", "requests": attached, "deduplicated": True, "supervisor_availability": availability}))
            return 0
        ident = f"{now}-{secrets.token_hex(6)}"
        record = {"schema": "fm-status-summary-request.v1", "id": ident, "requested_epoch": now, "requested_at": iso_utc(now), "projects": selected, "basis_fingerprints": {name: projections[name]["fingerprint"] for name in selected}, "state": "pending", "results": {name: "pending" for name in selected}, "expires_epoch": now + 1800, "supervisor_availability": supervisor_availability(home)}
        atomic_json(reqdir / f"{ident}.json", record)
        note = [str(ROOT / "bin" / "fm-inbox.sh"), "note", f"Request Manual Update id={ident} projects={','.join(selected)}"]
        env = os.environ.copy()
        env["FM_HOME"] = str(home)
        sent = subprocess.run(note, env=env, cwd=ROOT, capture_output=True, text=True, timeout=10)
        if sent.returncode:
            record["state"] = "failed"
            record["error"] = sent.stderr[-500:] or "inbox notification failed"
            atomic_json(reqdir / f"{ident}.json", record)
            print(json.dumps({"status": "failed", "request": ident, "error": record["error"]}))
            return 1
        print(json.dumps({"status": "pending", "request": ident, "projects": selected, "attached": attached, "supervisor_availability": availability}))
        return 0
    if args.command == "put":
        path = reqdir / f"{args.request_id}.json"
        record = read_json(path)
        if not isinstance(record, dict) or record.get("schema") != "fm-status-summary-request.v1" or args.project not in record.get("projects", []):
            print("error: unknown request or project", file=sys.stderr)
            return 2
        if int(record.get("expires_epoch", 0)) <= utc_now():
            record["state"] = "expired"
            atomic_json(path, record)
            print(json.dumps({"status": "expired", "project": args.project}))
            return 1
        if record.get("state") != "pending" or record.get("results", {}).get(args.project) != "pending":
            print(json.dumps({"status": record.get("results", {}).get(args.project, record.get("state", "unavailable")), "project": args.project}))
            return 1
        text = Path(args.text_file).read_text(encoding="utf-8")
        if not text.strip() or len(text.encode()) > 16_384:
            print("error: summary must be nonempty and at most 16384 bytes", file=sys.stderr)
            return 2
        current = make_projection(home, args.project)
        outdated = current["fingerprint"] != args.basis_fingerprint
        summary = {"schema": "fm-status-summary.v1", "request": args.request_id, "project": args.project, "author": args.author, "basis_fingerprint": args.basis_fingerprint, "basis_observed_epoch": args.basis_observed_at, "written_epoch": utc_now(), "text": text, "state": "outdated" if outdated else "written", "current_fingerprint": current["fingerprint"]}
        summary_path = sumdir / f"{hashlib.sha256(args.project.encode()).hexdigest()}.json"
        prior = read_json(summary_path, 1_000_000)
        history = prior.get("summaries", []) if isinstance(prior, dict) and prior.get("schema") == "fm-status-summaries.v1" and isinstance(prior.get("summaries"), list) else []
        history.append(summary)
        atomic_json(summary_path, {"schema": "fm-status-summaries.v1", "project": args.project, "summaries": history[-20:]})
        record.setdefault("results", {})[args.project] = summary["state"]
        if all(value in ("written", "outdated") for value in record["results"].values()):
            record["state"] = "outdated" if any(value == "outdated" for value in record["results"].values()) else "written"
        else:
            record["state"] = "pending"
        atomic_json(path, record)
        print(json.dumps({"status": summary["state"], "project": args.project}))
        return 0
    if args.command == "list":
        print(json.dumps(list_summary_requests(home), sort_keys=True))
        return 0
    return 2


def render_page(token: str, project: str) -> bytes:
    page = r"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Firstmate Issues</title>
<style>
:root{color-scheme:light dark;font:15px system-ui,sans-serif}body{margin:2rem auto;max-width:1320px;padding:0 1rem;color:CanvasText;background:Canvas}header{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}h1{margin-right:auto}button,select{font:inherit;padding:.55rem .8rem;border:1px solid #7b8794;border-radius:.35rem;background:Canvas;color:CanvasText}button{cursor:pointer}button:focus-visible,select:focus-visible,a:focus-visible{outline:3px solid #367bf5;outline-offset:2px}.muted{color:GrayText}.error{color:#b42318}.table-wrap{overflow-x:auto;border:1px solid #8885;border-radius:.4rem}table{border-collapse:collapse;width:100%;min-width:940px}th,td{padding:.7rem;border-bottom:1px solid #8885;text-align:left;vertical-align:top}th{background:color-mix(in srgb,CanvasText 8%,Canvas);position:sticky;top:0}tbody tr:hover{background:color-mix(in srgb,CanvasText 5%,Canvas)}a{color:LinkText}small{color:GrayText}.conflict{color:#b54708}#summary{white-space:pre-wrap;padding:.8rem;background:color-mix(in srgb,CanvasText 6%,Canvas);border-radius:.4rem}details{margin-top:.45rem}ul{padding-left:1.3rem}
</style><header><h1>Firstmate Issues</h1><label>Project <select id="project" aria-label="Project"></select></label><button id="refresh">Refresh</button><button id="request">Request Manual Update for all projects</button></header><p id="status" class="muted" role="status">Loading cached issue status…</p><p id="changed" class="muted"></p><section id="summary" aria-label="Manual project summary"></section><div class="table-wrap"><table><thead><tr><th>Issue</th><th>Work stage</th><th>Next step / blocker</th><th>Linked PRs</th><th>Verification</th><th>Status changed</th></tr></thead><tbody id="rows"></tbody></table></div><h2>Unlinked local tasks</h2><ul id="unlinked"></ul><script>
const token=__TOKEN__,initial=__PROJECT__;let selected=localStorage.getItem('fm-issues-project')||initial,projects=[];const el=id=>document.getElementById(id);
function time(v){if(!v)return 'unknown';return new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(v*1000))}
function text(value){return document.createTextNode(value==null?'':String(value))}
function detail(task){const box=document.createElement('details'),summary=document.createElement('summary');summary.textContent=`${task.id||'task'}: ${task.stage}; ${task.task_state||'state unknown'}`;box.append(summary);const content=document.createElement('div');content.append(text(`PRs: ${task.prs?.map(pr=>`${pr.url} (${pr.state||'unknown'})`).join('; ')||'none'}\n`));content.append(text(`Focused: ${task.verification?.focused?.status||'unknown'}; Full: ${task.verification?.full?.status||'unknown'}; Verify: ${task.verification?.verify?.status||'unknown'}\n`));content.append(text(`Source accepted: ${task.source_accepted||'not recorded'}; Canonical journeys: ${task.canonical_journeys||'not recorded'}; Ready: ${task.ready_for_approval||'unknown'}\n`));content.append(text(`Evidence basis: ${task.verification?.full?.basis||'forge and typed lifecycle records; absent evidence stays unknown'}`));box.append(content);return box}
function verification(row){const box=document.createElement('div');for(const task of row.tasks||[])box.append(detail(task));if(!row.tasks?.length)box.textContent='No recorded Firstmate work';return box}
function changed(value){if(value?.class==='event')return `${time(value.at_epoch)} (event)`;if(value?.class==='detected')return `between ${time(value.from_epoch)} and ${time(value.to_epoch)} (detected)`;return 'unknown'}
function renderRow(row){const tr=document.createElement('tr'),issue=document.createElement('td'),link=document.createElement('a'),stage=document.createElement('td'),next=document.createElement('td'),prs=document.createElement('td'),verify=document.createElement('td'),when=document.createElement('td');link.href=row.url;link.textContent=row.number?`#${row.number} ${row.title}`:row.url;issue.append(link);const count=document.createElement('small');count.textContent=`${row.task_count||0} tasks / ${row.pr_count||0} PRs`;stage.append(text(row.stage+'\n'),count);next.textContent=row.next_step||'Next step not recorded';if(row.conflicts?.length){const conflict=document.createElement('div');conflict.className='conflict';conflict.textContent=row.conflicts.join('; ');next.append(conflict)}for(const task of row.tasks||[])for(const pr of task.prs||[]){const a=document.createElement('a');a.href=pr.url;a.textContent=`${pr.url} (${pr.state||'unknown'})`;prs.append(a,text('\n'))}if(!row.pr_count)prs.textContent='None';verify.append(verification(row));when.textContent=changed(row.changed);tr.append(issue,stage,next,prs,verify,when);return tr}
async function load(force=false){const url='/api/status?project='+encodeURIComponent(selected)+(force?'&refresh=1':'');try{const response=await fetch(url),data=await response.json();if(!response.ok)throw Error(data.error||'collection unavailable');projects=data.projects||[];el('project').replaceChildren();for(const name of projects){const option=document.createElement('option');option.value=name;option.textContent=name;option.selected=name===selected;el('project').append(option)}const p=data.projection;if(!p)throw Error(data.error||'status unavailable');const cat=p.catalog;el('status').className='muted';el('status').textContent=`${p.repository||'Repository unavailable'}; catalog ${cat.complete&&!cat.stale?'complete':'partial, stale, or unavailable'} (${cat.known} known); last checked ${time(p.last_checked_epoch)}. Supervisor ${p.supervisor.session_lock_present?'session lock held':'no session lock'}; watcher beat ${time(p.supervisor.watcher_beat_epoch)}. ${cat.error||p.snapshot.error||''}`;el('rows').replaceChildren(...p.rows.map(renderRow));el('unlinked').replaceChildren(...p.unlinked_tasks.map(task=>{const li=document.createElement('li');li.textContent=`${task.id}: ${task.stage}; ${task.next_step||'Next step not recorded'}`;li.append(verification({tasks:[task]}));return li}));const fingerprintKey='fm-issues-fingerprint:'+selected,prior=localStorage.getItem(fingerprintKey);el('changed').textContent=prior&&prior!==p.fingerprint?'Project status changed since you last looked.':'';localStorage.setItem(fingerprintKey,p.fingerprint);renderSummary(p.summary);renderRequests(p.summary_requests)}catch(error){el('status').className='error';el('status').textContent='Unavailable: '+error.message}}
function renderSummary(summary){const root=el('summary');root.replaceChildren();if(!summary)return;if(summary.state==='written'){root.textContent=`Manual summary by ${summary.author}, based on status as of ${time(summary.basis_observed_epoch)}:\n${summary.text}`;return}if(summary.state==='outdated'){const details=document.createElement('details'),title=document.createElement('summary'),body=document.createElement('p');title.textContent='Summary outdated; historical text retained';body.textContent=summary.text;details.append(title,body);root.append(details)}}
function renderRequests(requests){if(!requests.length)return;const states=requests.map(item=>`${item.id}: ${item.display_state||item.state}; supervisor ${item.supervisor_availability||'unknown'}${item.error?` (${item.error})`:''}`).join('; ');el('changed').textContent+=(el('changed').textContent?' ':'')+`Manual update requests: ${states}.`}
el('project').addEventListener('change',event=>{selected=event.target.value;localStorage.setItem('fm-issues-project',selected);load()});el('refresh').onclick=()=>load(true);el('request').onclick=async()=>{try{const response=await fetch('/api/summary-requests',{method:'POST',headers:{'Content-Type':'application/json','X-FM-Token':token},body:JSON.stringify({projects})}),result=await response.json();el('status').textContent=result.status==='pending'?`Manual update request ${result.request||Object.values(result.requests||{})[0]||'already queued'}; supervisor ${result.supervisor_availability||'unknown'}.`:result.error||result.status}catch(error){el('status').textContent='Request failed: '+error.message}};load();setInterval(()=>{if(!document.hidden)load()},15000);
</script></html>"""
    return page.replace("__TOKEN__", json.dumps(token)).replace("__PROJECT__", json.dumps(project)).encode()


def serve(home: Path, project: str, port: int | None) -> int:
    token = secrets.token_urlsafe(32)
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "FirstmateIssues/1"
        def log_message(self, fmt: str, *args: object) -> None:
            return
        def send(self, code: int, value: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)
        def do_GET(self) -> None:
            host = self.headers.get("Host", "")
            if host != f"127.0.0.1:{self.server.server_port}":
                self.send(403, b'{"error":"invalid host"}', "application/json")
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.send(200, render_page(token, project), "text/html; charset=utf-8")
                return
            if parsed.path != "/api/status":
                self.send(404, b"not found", "text/plain; charset=utf-8")
                return
            query = urllib.parse.parse_qs(parsed.query)
            selected = query.get("project", [project])[0]
            try:
                value = {"projects": sorted(projects(home)), "projection": make_projection(home, selected, query.get("refresh") == ["1"])}
                self.send(200, json.dumps(value).encode(), "application/json")
            except (ValueError, OSError) as exc:
                self.send(400, json.dumps({"projects": sorted(projects(home)), "error": str(exc)}).encode(), "application/json")
        def do_POST(self) -> None:
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.path != "/api/summary-requests" or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}" or self.headers.get("Origin") != expected_origin or not secrets.compare_digest(self.headers.get("X-FM-Token", ""), token):
                self.send(403, b'{"error":"request refused"}', "application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 4096 or length <= 0:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {"projects"} or not isinstance(body["projects"], list) or not body["projects"] or len(body["projects"]) > 100 or any(not isinstance(item, str) or item not in projects(home) for item in body["projects"]):
                    raise ValueError("invalid project selection")
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "summary", "request", *sum((["--project", item] for item in body["projects"]), [])], env={**os.environ, "FM_HOME": str(home)}, cwd=ROOT, capture_output=True, text=True, timeout=30)
                data = json.loads(result.stdout) if result.stdout else {"error": result.stderr[-500:]}
                self.send(200 if result.returncode == 0 else 503, json.dumps(data).encode(), "application/json")
            except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                self.send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port or 0), Handler)
    except OSError as exc:
        print(f"fm-issues: cannot bind loopback service: {exc}", file=sys.stderr)
        return 1
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_port}/"
    print(url, flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deterministic local project issue table")
    parser.add_argument("--home", default=os.environ.get("FM_HOME", str(ROOT)))
    parser.add_argument("--project")
    parser.add_argument("--terminal", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("summary", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    if args.summary:
        summary_args = args.summary[1:] if args.summary[0] == "summary" else args.summary
        return summary_command(home, summary_args)
    available = sorted(projects(home))
    selected = args.project or (available[0] if available else None)
    if selected is None:
        print("fm-issues: no registered projects", file=sys.stderr)
        return 1
    try:
        value = make_projection(home, selected, args.refresh)
    except (ValueError, OSError) as exc:
        print(f"fm-issues: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif args.terminal:
        print(f"Issues - {selected} ({value['repository'] or 'repository unavailable'})")
        print(f"Catalog: {'complete' if value['catalog']['complete'] and not value['catalog']['stale'] else 'partial/stale/unavailable'}; {value['catalog']['known']} known; last checked {pt(value['last_checked_epoch'])}")
        print("Issue | Exact title | Stage | Next step / blocker | PRs | Verification | Changed")
        for row in value["rows"]:
            title = row.get("title") or row["url"]
            tasks = row.get("tasks", [])
            prs = ", ".join(f"{pr['url']} ({pr.get('state', 'unknown')})" for task in tasks for pr in task.get("prs", [])) or "-"
            waiting = row.get("next_step") or "Next step not recorded"
            verification = "; ".join(f"{task.get('id')}: focused {task.get('verification', {}).get('focused', {}).get('status', 'unknown')}, full {task.get('verification', {}).get('full', {}).get('status', 'unknown')}, verify {task.get('verification', {}).get('verify', {}).get('status', 'unknown')}, source {task.get('source_accepted', 'not recorded')}, journeys {task.get('canonical_journeys', 'not recorded')}, ready {task.get('ready_for_approval', 'unknown')}" for task in tasks) or "No recorded Firstmate work"
            changed = row.get("changed", {})
            changed_text = pt(changed.get("at_epoch")) if changed.get("class") == "event" else (f"between {pt(changed.get('from_epoch'))} and {pt(changed.get('to_epoch'))} (detected)" if changed.get("class") == "detected" else "unknown")
            print(f"{row.get('number') or '-'} | {title} | {row['stage']} | {waiting} | {prs} | {verification} | {changed_text}")
        for task in value["unlinked_tasks"]:
            print(f"unlinked task {task.get('id')}: {task['stage']}")
    else:
        return serve(home, selected, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
