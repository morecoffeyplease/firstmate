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


def repository_identity(origin: object) -> str | None:
    if not isinstance(origin, str) or len(origin) > 2048 or any(char in origin for char in "\r\n\x00"):
        return None
    parsed = urllib.parse.urlparse(origin)
    if origin.startswith("git@") and ":" in origin:
        host = origin.split("@", 1)[1].split(":", 1)[0]
        path = origin.split(":", 1)[1]
    else:
        host = parsed.hostname
        path = parsed.path.lstrip("/")
        if parsed.scheme not in ("https", "ssh", "git") or parsed.username not in (None, "git"):
            return None
    path = path.removesuffix(".git").strip("/")
    if host != "github.com" or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", path):
        return None
    return path


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


def catalog(home: Path, project: str, repo: str, refresh: bool, known_issue_numbers: list[int] | None = None) -> dict:
    digest = hashlib.sha256(repo.lower().encode()).hexdigest()
    path = home / "state" / "issue-catalog" / f"{digest}.json"
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("issue catalog cache path is a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
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
    command = [str(ROOT / "bin" / "fm-contributions.sh"), "catalog", repo, *[str(number) for number in sorted(set(known_issue_numbers or []))]]
    env = os.environ.copy()
    env["FM_HOME"] = str(home)
    env.pop("FM_ROOT_OVERRIDE", None)
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env, cwd=ROOT, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr[-500:] or f"catalog reader exited {result.returncode}")
        parsed = json.loads(result.stdout)
        if not isinstance(parsed, dict) or parsed.get("schema") != "fm-issue-catalog.v1" or parsed.get("repository") != repo or not isinstance(parsed.get("complete"), bool) or not isinstance(parsed.get("issues"), list):
            raise RuntimeError("catalog reader returned an invalid schema")
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
            canonical_urls = canonical_issue_urls([url], repo)
            if not canonical_urls or canonical_urls[0].rsplit("/", 1)[1] != str(number):
                continue
            url = canonical_urls[0]
            updated = item.get("updated_at")
            try:
                updated_epoch = int(datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp())
            except (TypeError, ValueError):
                updated_epoch = None
            issues.append({"number": number, "title": title, "url": url, "state": state, "updated_at": updated, "updated_epoch": updated_epoch})
        issues.sort(key=lambda row: row["number"])
        identity_checks = []
        for check in parsed.get("identity_checks", []):
            if not isinstance(check, dict) or not isinstance(check.get("url"), str) or check.get("status") not in ("checked", "not-visible-to-login", "unknown"):
                continue
            identity_checks.append({"url": check["url"], "status": check["status"], "error": check.get("error") if isinstance(check.get("error"), str) else None})
        checked_epoch = now
        try:
            checked_epoch = int(datetime.fromisoformat(parsed["observed_at"].replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            pass
        if parsed.get("complete") is True:
            value = {"schema": "fm-issue-catalog.v1", "repo": repo, "checked_epoch": checked_epoch, "complete": True, "partial": False, "observed_known": len(issues), "known": len(issues), "total": len(issues), "issues": issues, "identity_checks": identity_checks, "error": None, "stale": False}
        else:
            previous_issues = previous.get("issues", []) if isinstance(previous, dict) and previous.get("repo") == repo else []
            by_url = {
                item["url"]: item for item in previous_issues
                if isinstance(item, dict) and isinstance(item.get("url"), str)
                and isinstance(item.get("number"), int) and isinstance(item.get("title"), str)
                and item.get("state") in ("open", "closed")
            }
            by_url.update({item["url"]: item for item in issues})
            value = {"schema": "fm-issue-catalog.v1", "repo": repo, "checked_epoch": checked_epoch, "complete": False, "partial": True, "observed_known": len(issues), "known": len(by_url), "total": None, "issues": sorted(by_url.values(), key=lambda row: row["number"]), "identity_checks": identity_checks, "error": parsed.get("error") or "pagination was interrupted; total repository issue count is unknown", "stale": bool(previous_issues)}
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
        value = {"schema": "fm-issue-catalog.v1", "repo": repo, "checked_epoch": None, "complete": False, "partial": False, "observed_known": 0, "known": 0, "total": None, "issues": [], "identity_checks": [], "error": str(exc), "stale": False, "last_attempt_epoch": now}
        atomic_json(path, value)
        return value


def task_links(task: dict, repo: str | None) -> list[str]:
    backlog = task.get("backlog") or {}
    links = backlog.get("links") or []
    return canonical_issue_urls(links, repo)


def canonical_issue_urls(links: list, repo: str | None) -> list[str]:
    if not isinstance(repo, str) or not repo or not isinstance(links, list):
        return []
    expected = repo.casefold()
    canonical = f"https://github.com/{repo}/issues/"
    result = set()
    for link in links:
        if not isinstance(link, str):
            continue
        try:
            parsed = urllib.parse.urlparse(link)
            parts = parsed.path.strip("/").split("/")
            if (
                parsed.scheme != "https" or parsed.netloc.casefold() != "github.com"
                or parsed.query or parsed.fragment or len(parts) != 4
                or f"{parts[0]}/{parts[1]}".casefold() != expected
                or parts[2] != "issues" or not re.fullmatch(r"[1-9][0-9]*", parts[3])
            ):
                continue
            result.add(canonical + parts[3])
        except (TypeError, ValueError):
            continue
    return sorted(result, key=lambda value: int(value.rsplit("/", 1)[1]))


def canonical_pr_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlparse(value)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.scheme != "https" or parsed.netloc.casefold() != "github.com"
            or parsed.query or parsed.fragment or len(parts) != 4
            or parts[2] != "pull" or not re.fullmatch(r"[1-9][0-9]*", parts[3])
            or not re.fullmatch(r"[A-Za-z0-9-]+", parts[0])
            or not re.fullmatch(r"[A-Za-z0-9._-]+", parts[1])
        ):
            return None
        return f"https://github.com/{parts[0]}/{parts[1]}/pull/{parts[3]}"
    except (TypeError, ValueError):
        return None


def stage(task: dict, linked_prs: list[dict], backlog: dict | None) -> tuple[str, str | None]:
    current = task.get("current_state", {}).get("state")
    hints = task.get("hints") or {}
    unresolved = (backlog or {}).get("unresolved_blocker_ids") or []
    waiting = None
    if (backlog and backlog.get("hold_bucket") == "live") or hints.get("pending_decision") is True:
        waiting = "your decision"
    elif unresolved or hints.get("blocked_event") is True:
        waiting = "blocked"
    elif (backlog and backlog.get("hold_bucket") in ("dated", "aged")) or current == "paused":
        waiting = "paused"
    if backlog and backlog.get("state") == "queued":
        return "Queued", "prerequisite" if unresolved else waiting
    if linked_prs:
        if any(pr.get("forge_checked") is True and pr.get("state") == "merged" for pr in linked_prs):
            return ("Unknown", waiting) if backlog and backlog.get("state") == "in_flight" else ("Completed", waiting)
        merge_requests = task.get("merge_requests") or []
        merge_requested = any(
            item.get("url") == pr.get("url") and pr.get("state") == "open"
            for item in merge_requests for pr in linked_prs
        )
        if merge_requested:
            return "Merging", waiting
        if not any(pr.get("forge_checked") is True and pr.get("state") == "open" for pr in linked_prs):
            return "Unknown", waiting
        if any(pr.get("revising") is True for pr in linked_prs):
            return "Revising", waiting
    if task.get("kind") == "scout" and backlog and backlog.get("state") == "in_flight":
        return "Investigating", waiting
    if task.get("kind") == "ship" and backlog and backlog.get("state") == "in_flight" and not linked_prs:
        return "Implementing", waiting
    if linked_prs:
        return "In review", waiting
    if current == "done" or (backlog and backlog.get("state") == "done"):
        return "Closed without delivery", waiting
    if current == "failed":
        return "Unknown", waiting or "failed"
    return "Unknown", waiting


def next_step(task: dict) -> str:
    waiting = task.get("waiting")
    backlog = task.get("backlog") or {}
    if waiting == "your decision":
        return f"Waiting on your decision: {backlog.get('hold_reason') or task.get('id')}"
    if waiting == "paused":
        return f"Paused/deferred: {backlog.get('hold_reason') or 'task is paused'}"
    if waiting == "prerequisite":
        return f"Queued behind {', '.join(backlog.get('unresolved_blocker_ids') or [])}"
    if waiting == "blocked":
        return f"Blocked by {backlog.get('blocked_by') or 'untyped blocker'}"
    if task.get("task_state") == "failed":
        return "Failed; next step not recorded"
    if any(pr.get("review_decision") == "CHANGES_REQUESTED" for pr in task.get("prs", [])):
        return "Changes requested; revise the current PR head"
    if any(pr.get("failed_checks", 0) for pr in task.get("prs", [])):
        return "Checks failing on the current PR head"
    if any(pr.get("pending_checks", 0) for pr in task.get("prs", [])):
        running = sum(pr.get("pending_checks", 0) for pr in task.get("prs", []))
        total = sum(len(pr.get("checks") or []) for pr in task.get("prs", []))
        return f"Waiting on CI ({running} of {total} checks running)"
    if any(pr.get("review_decision") == "REVIEW_REQUIRED" for pr in task.get("prs", [])):
        return "Review required"
    if task.get("stage") == "Ready for approval":
        return "Ready for your approval"
    if task.get("stage") == "Queued":
        return "Queued"
    return "Next step not recorded"


def ready_state(task: dict, configured: dict[str, list[str]]) -> str:
    if task.get("waiting") or task.get("stage") in ("Queued", "Unknown", "Implementing", "Investigating"):
        return "not ready"
    if task.get("conflicts") or task.get("hints", {}).get("open_decisions"):
        return "not ready"
    backlog = task.get("backlog") or {}
    if backlog.get("unresolved_blocker_ids") or backlog.get("hold_bucket") in ("live", "blocked", "dated", "aged"):
        return "not ready"
    prs = task.get("prs", [])
    if not prs:
        return "not ready"
    for pr in prs:
        if pr.get("forge_checked") is not True or pr.get("state") in (None, "unknown") or pr.get("draft") is None or pr.get("checks") is None:
            return "unknown"
        if pr.get("state") != "open" or pr.get("draft") or pr.get("outstanding_changes_requested") or pr.get("missing_verdicts", 0) or pr.get("stale_verdicts", 0):
            return "not ready"
        checks = pr.get("checks", [])
        if not checks or any(check.get("status") != "completed" or check.get("conclusion") not in ("success", "skipped", "neutral") for check in checks):
            return "not ready"
        if not pr.get("head") or not task.get("source_head") or pr.get("head") != task.get("source_head"):
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
        candidate_key = (str(check.get("started_at") or ""), safe_integer(check.get("id")))
        prior_key = (str(prior.get("started_at") or ""), safe_integer(prior.get("id"))) if prior else None
        if prior is None or candidate_key > prior_key:
            latest[name] = check
    return {"url": pr.get("url"), "state": pr.get("state"), "head": pr.get("head"), "draft": pr.get("draft"), "review_decision": pr.get("review_decision"), "failed_checks": pr.get("failed_checks", 0), "checks": [{"name": name, "status": value.get("status"), "conclusion": value.get("conclusion")} for name, value in sorted(latest.items())]}


def safe_integer(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def fingerprint_task(task: dict) -> dict:
    event = task.get("event")
    event_value = None
    if event:
        fields = event.get("fields", {})
        momentary = event.get("kind") == "status-seen" and fields.get("state") in ("working", "busy", "running")
        if not momentary:
            event_value = {"class": event.get("class"), "kind": event.get("kind"), "fields": {key: value for key, value in fields.items() if key not in ("from_epoch", "to_epoch")}}
    return {
        "id": task.get("id"),
        "generation": task.get("generation"),
        "kind": task.get("kind"),
        "source_head": task.get("source_head"),
        "stage": task.get("stage"),
        "waiting": task.get("waiting"),
        "next_step": task.get("next_step"),
        "backlog_state": (task.get("backlog") or {}).get("state"),
        "blocked_by": (task.get("backlog") or {}).get("blocked_by"),
        "unresolved_blocker_ids": sorted((task.get("backlog") or {}).get("unresolved_blocker_ids") or []),
        "hold_reason": (task.get("backlog") or {}).get("hold_reason"),
        "open_decisions": task.get("hints", {}).get("open_decisions", []),
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
    records: dict[str, list[dict]] = {lane: [] for lane in ("focused", "full", "verify")}
    malformed: dict[str, bool] = {lane: False for lane in records}
    expected_repo, _repo_error = canonical_repo(home, project)
    for lane in records:
        for path in receipt_dir.glob(f"{lane}-*.json"):
            value = read_json(path, 2_000_000)
            if not isinstance(value, dict):
                malformed[lane] = True
                continue
            valid = (
                value.get("schema") == "fm-lane-receipt.v1"
                and value.get("phase") in ("start", "finish")
                and value.get("lane") == lane
                and isinstance(value.get("id"), str) and path.stem == f"{lane}-{value.get('id')}"
                and value.get("task") == task_id and value.get("generation") == generation
                and value.get("project") == project and value.get("repository")
                and repository_identity(value.get("repository")) == expected_repo
                and isinstance(value.get("argv"), list) and value["argv"]
                and all(isinstance(arg, str) and "\x00" not in arg for arg in value["argv"])
                and isinstance(value.get("started_epoch"), int) and value["started_epoch"] >= 0
                and isinstance(value.get("started_order_ns"), int) and value["started_order_ns"] >= 0
                and isinstance(value.get("pid"), int) and value["pid"] > 0
                and isinstance(value.get("process_start"), str) and bool(value["process_start"])
                and isinstance(value.get("head_before"), str) and bool(value["head_before"])
                and isinstance(value.get("dirty_before"), bool)
            )
            if value.get("phase") == "finish":
                valid = valid and isinstance(value.get("ended_epoch"), int) and value["ended_epoch"] >= value["started_epoch"]
                valid = valid and isinstance(value.get("head_after"), str) and bool(value["head_after"])
                valid = valid and isinstance(value.get("dirty_after"), bool)
                valid = valid and (value.get("exit_code") is None or (isinstance(value.get("exit_code"), int) and 0 <= value["exit_code"] <= 255))
                valid = valid and (value.get("signal") is None or (isinstance(value.get("signal"), int) and 1 <= value["signal"] <= 64))
                valid = valid and (value.get("received_signal") is None or (isinstance(value.get("received_signal"), int) and 1 <= value["received_signal"] <= 64))
            if not valid:
                malformed[lane] = True
                continue
            records[lane].append(value)
    focused_order = -1
    for lane in ("focused", "full", "verify"):
        matches = records[lane]
        matches.sort(key=lambda item: item["started_order_ns"], reverse=True)
        chosen = matches[0] if matches else None
        if malformed[lane]:
            result[lane] = {"status": "unknown", "basis": "a malformed or mismatched receipt prevents qualification"}
            continue
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
        elif chosen.get("signal") is not None or chosen.get("received_signal") is not None:
            state = "canceled"
        elif chosen.get("exit_code") != 0:
            state = "failed"
        elif chosen.get("dirty_before") is True or chosen.get("dirty_after") is True or chosen.get("head_before") != chosen.get("head_after"):
            state = "passed on uncommitted or changed source"
        elif chosen.get("dirty_before") is not False or chosen.get("dirty_after") is not False or not current_head:
            state = "unknown"
        elif chosen.get("head_after") == current_head:
            state = "passed"
        else:
            state = "stale (passed on an older head)"
        error_path = receipt_dir / ".errors"
        error = None
        try:
            if error_path.is_symlink() or (error_path.exists() and (not error_path.is_file() or error_path.stat().st_size > 65536)):
                error = "receipt error journal is unsafe"
            elif error_path.exists():
                for line in error_path.read_text(encoding="utf-8").splitlines():
                    match = re.match(r"^(\d+) (.{1,300})$", line)
                    if not match:
                        error = "receipt error journal is malformed"
                        break
                    if int(match.group(1)) >= chosen["started_epoch"]:
                        error = match.group(2)
        except (OSError, UnicodeError):
            error = "receipt error journal cannot be read"
        if error:
            state = "unknown"
        order = chosen["started_order_ns"]
        if lane in ("full", "verify") and chosen.get("argv") != configured.get(lane):
            if order > focused_order:
                result["focused"] = {"status": f"reclassified focused run; outcome {state}", "receipt": chosen.get("id"), "started_epoch": chosen.get("started_epoch"), "ended_epoch": chosen.get("ended_epoch"), "argv": chosen.get("argv"), "artifact": chosen.get("artifact_record"), "log": chosen.get("log", "not retained"), "basis": "configured full/verify argv did not match; this run receives no configured-lane credit"}
                focused_order = order
            continue
        detail = {"status": state, "receipt": chosen.get("id"), "started_epoch": chosen.get("started_epoch"), "ended_epoch": chosen.get("ended_epoch"), "head_after": chosen.get("head_after"), "argv": chosen.get("argv"), "artifact": chosen.get("artifact_record"), "log": chosen.get("log", "not retained"), "error": error, "basis": "receipts from wrapped runs; unwrapped runs are invisible"}
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
    events = []
    allowed = {
        "started": {"kind"}, "done": {"transition"}, "reopened": {"transition"},
        "held": {"source"}, "answered": {"source"}, "released": {"source"},
        "reconciled": {"source"}, "blocked-by": {"blocker"}, "unblocked": {"blocker"},
        "pr-bound": {"url", "head"}, "merge-requested": {"url", "authority"},
        "decision-resolved": {"key"},
        "status-seen": {"state", "key", "from_epoch", "to_epoch"},
    }
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if (
                not isinstance(item, dict) or item.get("schema") != "fm-task-event.v1"
                or item.get("task") != task_id or not isinstance(item.get("generation"), str)
                or item.get("class") not in ("event", "detected")
                or not isinstance(item.get("at_epoch"), int) or item["at_epoch"] < 0
                or not isinstance(item.get("kind"), str) or item["kind"] not in allowed
                or not isinstance(item.get("fields"), dict)
                or set(item["fields"]) - allowed[item["kind"]]
                or any(not isinstance(value, (str, int, type(None))) for value in item["fields"].values())
            ):
                return None
            fields = item["fields"]
            required = {"started": {"kind"}, "done": {"transition"}, "reopened": {"transition"},
                        "held": {"source"}, "answered": {"source"}, "released": {"source"},
                        "reconciled": {"source"}, "blocked-by": {"blocker"}, "unblocked": {"blocker"},
                        "pr-bound": {"url", "head"}, "merge-requested": {"url", "authority"},
                        "decision-resolved": {"key"}, "status-seen": {"state", "to_epoch"}}
            if not required[item["kind"]].issubset(fields):
                return None
            if item["kind"] == "started" and fields["kind"] not in ("ship", "scout"):
                return None
            if item["kind"] in ("done", "reopened") and fields["transition"] not in ("close", "retain"):
                return None
            if item["kind"] == "merge-requested" and fields["authority"] not in ("yolo", "away-grant", "attended"):
                return None
            if item["kind"] == "decision-resolved" and (not isinstance(fields["key"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", fields["key"])):
                return None
            if item.get("generation") != generation:
                continue
            events.append(item)
            latest = item
    except (OSError, ValueError, TypeError):
        return None
    if latest is not None:
        latest = dict(latest)
        latest["history"] = events
    return latest


def worktree_head(task: dict) -> str | None:
    path = (task.get("paths") or {}).get("worktree", {}).get("path")
    if not isinstance(path, str) or not path:
        return None
    try:
        result = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3)
        value = result.stdout.strip()
        return value if result.returncode == 0 and re.fullmatch(r"[a-fA-F0-9]{40,64}", value) else None
    except (OSError, subprocess.SubprocessError):
        return None


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
    remote_issue_coverage = {"registered_homes": 0, "shown_home_summaries": 0, "unknown_home_summaries": 0, "stale_home_summaries": 0, "visible_task_records": 0, "omitted_task_records": 0, "complete": False}
    if isinstance(snap, dict):
        secondmate_current = snap.get("secondmate_current") or {}
        remote_records = secondmate_current.get("records", []) if isinstance(secondmate_current, dict) else []
        if not isinstance(remote_records, list):
            remote_records = []
        registered_homes = secondmate_current.get("total") if isinstance(secondmate_current, dict) else None
        if isinstance(registered_homes, int) and registered_homes >= 0:
            remote_issue_coverage["registered_homes"] = registered_homes
        else:
            remote_issue_coverage["registered_homes"] = len(remote_records)
        remote_issue_coverage["shown_home_summaries"] = len(remote_records)
        remote_issue_coverage["complete"] = isinstance(registered_homes, int) and registered_homes == len(remote_records) and not bool(secondmate_current.get("truncated", False)) if isinstance(secondmate_current, dict) else False
        for secondmate in remote_records:
            valid_summary = (secondmate.get("provenance") or {}).get("summary_valid") is True
            summary_freshness = (secondmate.get("freshness") or {}).get("status")
            inventory_proven = isinstance((secondmate.get("counts") or {}).get("issue_tasks"), int)
            issue_tasks = secondmate.get("issue_tasks", []) if isinstance(secondmate.get("issue_tasks", []), list) else []
            remote_issue_coverage["visible_task_records"] += len(issue_tasks)
            remote_issue_coverage["omitted_task_records"] += sum(item.get("count", 0) for item in secondmate.get("omitted", []) if item.get("surface") == "issue_tasks" and isinstance(item.get("count"), int))
            if not valid_summary or not inventory_proven:
                remote_issue_coverage["unknown_home_summaries"] += 1
                remote_issue_coverage["complete"] = False
            if summary_freshness != "fresh":
                remote_issue_coverage["stale_home_summaries"] += 1
                remote_issue_coverage["complete"] = False
        contribution_rows = (snap.get("contributions") or {}).get("rows", [])
        configured_lanes = read_project_lanes(home).get(project, {})
        for task in snap.get("tasks", []):
            repo_name = (task.get("backlog") or {}).get("repo") or task.get("project")
            if repo_name != project or task.get("kind") == "secondmate":
                continue
            raw_pr_url = (task.get("pr") or {}).get("url")
            pr_url = canonical_pr_url(raw_pr_url)
            backlog = task.get("backlog")
            linked = []
            if pr_url:
                contribution = next((item for item in contribution_rows if item.get("url") == pr_url), None)
                forge = contribution.get("forge") if isinstance(contribution, dict) else None
                forge = forge if isinstance(forge, dict) else {}
                forge_checked = isinstance(contribution, dict) and contribution.get("checked") is True
                checks = forge.get("checks") if forge_checked and isinstance(forge.get("checks"), list) else None
                failed_checks = sum(1 for check in checks or [] if check.get("status") == "completed" and check.get("conclusion") not in (None, "success", "skipped", "neutral"))
                reviews = contribution.get("reviews", []) if forge_checked and isinstance(contribution, dict) else []
                decision = forge.get("review_decision") if forge_checked else None
                requested_reviews = [review for review in reviews if review.get("state") == "CHANGES_REQUESTED"]
                outstanding = decision == "CHANGES_REQUESTED" and (not requested_reviews or any(review.get("freshness") == "current" for review in requested_reviews))
                revising = bool(forge_checked and forge.get("head") and any(review.get("freshness") == "STALE" for review in requested_reviews))
                linked.append({"url": pr_url, "state": forge.get("state", "unknown") if forge_checked else "unknown", "head": forge.get("head") if forge_checked else None, "draft": forge.get("draft") if forge_checked else None, "review_decision": decision, "mergeable": forge.get("mergeable") if forge_checked else None, "checks": checks, "reviews": reviews, "outstanding_changes_requested": outstanding, "revising": revising, "checked_at": contribution.get("checked_at") if isinstance(contribution, dict) else None, "forge_checked": forge_checked, "missing_verdicts": contribution.get("missing_verdicts", 0) if isinstance(contribution, dict) else 0, "stale_verdicts": contribution.get("stale_verdicts", 0) if isinstance(contribution, dict) else 0, "pending_checks": contribution.get("pending_checks", 0) if isinstance(contribution, dict) else 0, "failed_checks": failed_checks, "association": "task-linked; issue-specific PR relation not recorded"})
            task_id = task.get("id")
            generation = task.get("spawn_gen")
            event = task_event_fact(home, task_id, generation)
            event_history = event.get("history", []) if isinstance(event, dict) else []
            latest_event = {key: value for key, value in event.items() if key != "history"} if isinstance(event, dict) else None
            task_row = {"id": task_id, "generation": generation, "kind": task.get("kind"), "task_state": task.get("current_state", {}).get("state", "unknown"), "activity_source": task.get("current_state", {}).get("source", "unknown"), "activity_detail": task.get("current_state", {}).get("detail"), "current_state": task.get("current_state", {}), "hints": task.get("hints", {}), "source_head": worktree_head(task), "backlog": backlog, "issues": task_links(task, repo), "prs": linked, "verification": lane_status(home, task, project), "event": latest_event, "merge_requests": [{"url": item.get("fields", {}).get("url"), "at_epoch": item.get("at_epoch")} for item in event_history if item.get("kind") == "merge-requested"]}
            if backlog and backlog.get("state") == "in_flight" and any(pr.get("state") == "merged" for pr in linked):
                task_row["conflicts"] = ["forge PR is merged while local task remains in flight"]
            if raw_pr_url and not pr_url:
                task_row.setdefault("conflicts", []).append("task PR URL has an unsupported identity")
            task_row["stage"], task_row["waiting"] = stage(task_row, linked, backlog)
            task_row["next_step"] = next_step(task_row)
            task_row["ready_for_approval"] = ready_state(task_row, configured_lanes)
            if task_row["ready_for_approval"] == "ready for approval":
                task_row["stage"] = "Ready for approval"
                task_row["next_step"] = "Ready for your approval"
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
        secondmates = remote_records
        for secondmate in secondmates:
            home_id = secondmate.get("id") or "registered-home"
            for remote in secondmate.get("issue_tasks", []):
                backlog = remote.get("backlog") if isinstance(remote.get("backlog"), dict) else {}
                issue_urls = canonical_issue_urls(backlog.get("links", []), repo)
                if not issue_urls:
                    continue
                source_id = remote.get("id")
                if not isinstance(source_id, str) or not source_id:
                    continue
                task_id = f"{home_id}/{source_id}"
                linked_prs = []
                raw_remote_pr_url = backlog.get("pr_url")
                pr_url = canonical_pr_url(raw_remote_pr_url)
                if pr_url:
                    remote_rows = ((secondmate.get("contributions") or {}).get("rows", []))
                    contribution = next((item for item in remote_rows if isinstance(item, dict) and item.get("url") == pr_url), None)
                    forge = contribution.get("forge") if isinstance(contribution, dict) and contribution.get("checked") is True else None
                    forge = forge if isinstance(forge, dict) else {}
                    forge_checked = isinstance(contribution, dict) and contribution.get("checked") is True
                    checks = forge.get("checks") if forge_checked and isinstance(forge.get("checks"), list) else None
                    failed_checks = sum(1 for check in checks or [] if check.get("status") == "completed" and check.get("conclusion") not in (None, "success", "skipped", "neutral"))
                    reviews = contribution.get("reviews", []) if forge_checked and isinstance(contribution, dict) else []
                    decision = forge.get("review_decision") if forge_checked else None
                    requested_reviews = [review for review in reviews if review.get("state") == "CHANGES_REQUESTED"]
                    linked_prs.append({"url": pr_url, "head": forge.get("head") if forge_checked else None, "state": forge.get("state", "unknown") if forge_checked else "unknown", "draft": forge.get("draft") if forge_checked else None, "review_decision": decision, "mergeable": forge.get("mergeable") if forge_checked else None, "checks": checks, "reviews": reviews, "outstanding_changes_requested": decision == "CHANGES_REQUESTED" and (not requested_reviews or any(review.get("freshness") == "current" for review in requested_reviews)), "revising": bool(forge_checked and forge.get("head") and any(review.get("freshness") == "STALE" for review in requested_reviews)), "forge_checked": forge_checked, "missing_verdicts": contribution.get("missing_verdicts", 0) if isinstance(contribution, dict) else 0, "stale_verdicts": contribution.get("stale_verdicts", 0) if isinstance(contribution, dict) else 0, "pending_checks": contribution.get("pending_checks", 0) if isinstance(contribution, dict) else 0, "failed_checks": failed_checks, "association": "task-linked; issue-specific PR relation not recorded"})
                current_state = remote.get("current_state") if isinstance(remote.get("current_state"), dict) else {"state": "unknown", "source": "remote summary unavailable"}
                summary_valid = (secondmate.get("provenance") or {}).get("summary_valid") is True
                summary_fresh = (secondmate.get("freshness") or {}).get("status") == "fresh"
                if not summary_valid or not summary_fresh:
                    current_state = {**current_state, "reported_state": current_state.get("state"), "state": "unknown", "source": "untrusted partial home summary"}
                task_row = {
                    "id": task_id,
                    "task_id": source_id,
                    "owner_home_id": home_id,
                    "generation": remote.get("generation"),
                    "kind": remote.get("kind"),
                    "task_state": current_state.get("state", "unknown"),
                    "activity_source": current_state.get("source", "unknown"),
                    "activity_detail": current_state.get("detail"),
                    "current_state": current_state,
                    "hints": {},
                    "source_head": None,
                    "backlog": backlog,
                    "issues": issue_urls,
                    "prs": linked_prs,
                    "verification": {lane: {"status": "unknown", "basis": "remote lane receipts are not included in the validated home summary"} for lane in ("focused", "full", "verify")},
                    "event": None,
                    "merge_requests": [],
                    "source_accepted": "not recorded",
                    "canonical_journeys": "not recorded",
                    "conflicts": ([] if summary_valid and summary_fresh else ["remote home summary is stale or not valid for complete current-state evidence"])
                        + (["task PR URL has an unsupported identity"] if raw_remote_pr_url and not pr_url else []),
                }
                task_row["stage"], task_row["waiting"] = stage(task_row, linked_prs, backlog)
                task_row["next_step"] = next_step(task_row)
                task_row["ready_for_approval"] = "unknown"
                candidates.append(task_row)
    cat = {"schema": "fm-issue-catalog.v1", "repo": None, "checked_epoch": None, "complete": False, "known": 0, "observed_known": 0, "total": None, "issues": [], "error": repo_error, "stale": False}
    if repo:
        issue_prefix = f"https://github.com/{repo}/issues/"
        known_numbers = [int(url[len(issue_prefix):]) for task in candidates for url in task.get("issues", []) if url.startswith(issue_prefix)]
        cat = catalog(home, project, repo, refresh, known_numbers)
    identity_checks = {item.get("url"): item for item in cat.get("identity_checks", []) if isinstance(item, dict)}
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
            identity = identity_checks.get(url, {})
            visibility = "not visible to this login (forge returned 403/404)" if identity.get("status") == "not-visible-to-login" else "known identity observation unknown" if identity.get("status") == "unknown" else "known identity not present in observed catalog coverage"
            rows.append({"url": url, "number": None, "title": None, "forge_state": "unknown", "visibility": visibility, "identity_check": identity, "tasks": linked, "stage": "Unknown", "changed": {"class": "unknown"}})
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
    prior_rows = previous.get("rows", {}) if isinstance(previous, dict) and isinstance(previous.get("rows"), dict) else {}
    prior_prs = {}
    for old_row in prior_rows.values():
        for old_task in old_row.get("tasks", []) if isinstance(old_row, dict) else []:
            for old_pr in old_task.get("prs", []) if isinstance(old_task, dict) else []:
                prior_prs[(old_task.get("id"), old_pr.get("url"))] = old_pr
    for row in rows:
        for task in row.get("tasks", []):
            for pr in task.get("prs", []):
                old_pr = prior_prs.get((task.get("id"), pr.get("url")))
                if old_pr and old_pr.get("head") and pr.get("head") and old_pr.get("head") != pr.get("head"):
                    prior_failed = any(check.get("conclusion") not in ("success", "skipped", "neutral", None) for check in old_pr.get("checks", []))
                    prior_requested = old_pr.get("review_decision") == "CHANGES_REQUESTED"
                    if prior_failed or prior_requested:
                        pr["revising"] = True
                        pr["prior_head_failure"] = prior_failed
                        pr["prior_head_changes_requested"] = prior_requested
            configured_lanes = read_project_lanes(home).get(project, {})
            task["stage"], task["waiting"] = stage(task, task.get("prs", []), task.get("backlog"))
            task["ready_for_approval"] = ready_state(task, configured_lanes)
            if task["ready_for_approval"] == "ready for approval":
                task["stage"] = "Ready for approval"
            task["next_step"] = next_step(task)
    for row in rows:
        linked = row.get("tasks", [])
        if not linked:
            continue
        def rank(task: dict) -> tuple[int, int]:
            if task.get("waiting") == "your decision": return (0, 0)
            if task.get("conflicts") or task.get("waiting") in ("blocked", "failed") or task.get("task_state") == "failed": return (1, 0)
            if task.get("stage") == "Ready for approval": return (2, 0)
            return ({"Investigating": (3, 0), "Implementing": (4, 0), "Revising": (5, 0), "In review": (6, 0), "Merging": (7, 0), "Queued": (8, 0), "Completed": (9, 0), "Closed without delivery": (10, 0)}.get(task.get("stage"), (1, 0)))
        chosen_task = min(linked, key=rank)
        row["stage"] = chosen_task.get("stage", "Unknown")
        row["next_step"] = chosen_task.get("next_step")
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
                old_tasks = {task.get("id"): task for task in old.get("tasks", []) if isinstance(task, dict)}
                event_fields = {
                    "started": {"stage", "backlog_state"}, "done": {"stage", "backlog_state"},
                    "reopened": {"stage", "backlog_state"}, "held": {"waiting", "hold_reason", "open_decisions"},
                    "answered": {"waiting", "hold_reason", "open_decisions"}, "released": {"waiting", "hold_reason", "open_decisions"},
                    "reconciled": {"waiting", "hold_reason", "open_decisions"}, "blocked-by": {"blocked_by", "unresolved_blocker_ids"},
                    "unblocked": {"blocked_by", "unresolved_blocker_ids"}, "pr-bound": {"prs"},
                    "merge-requested": {"stage", "prs"}, "decision-resolved": {"waiting", "open_decisions"},
                    "status-seen": {"waiting", "open_decisions", "stage"},
                }
                changed_events = []
                for task in row.get("tasks", []):
                    event = task.get("event")
                    prior_event = old_events.get(task.get("id"))
                    prior_task = old_tasks.get(task.get("id"), {})
                    current_task = fingerprint_task(task)
                    changed_fields = {key for key in current_task if prior_task.get(key) != current_task.get(key)}
                    if event and prior_event != event and event_fields.get(event.get("kind"), set()) & changed_fields:
                        changed_events.append(event)
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
        next_rows[row["url"]] = {"fingerprint": row_fingerprint, "observed_epoch": now, "title": row.get("title"), "state": row.get("forge_state"), "changed": row_changed, "events": {task.get("id"): task.get("event") for task in row.get("tasks", []) if task.get("event")}, "tasks": [fingerprint_task(task) for task in row.get("tasks", [])]}
    if previous_fp != fingerprint or prior_rows != next_rows:
        atomic_json(state_cache, {"schema": FP_SCHEMA, "fingerprint": fingerprint, "observed_epoch": now, "changed": changed, "rows": next_rows})
    summary_path = home / "state" / "status-summary" / "summaries" / f"{hashlib.sha256(project.encode()).hexdigest()}.json"
    summary_file = read_json(summary_path, 1_000_000)
    summaries = summary_file.get("summaries", []) if isinstance(summary_file, dict) and isinstance(summary_file.get("summaries"), list) else []
    summary = summaries[-1] if summaries else None
    if isinstance(summary, dict) and summary.get("schema") == "fm-status-summary.v1":
        summary = dict(summary)
        if summary.get("state") != "outdated" and summary.get("basis_fingerprint") != fingerprint:
            summary["state"] = "outdated"
            summary["invalidated_epoch"] = now
            summary["invalidated_change"] = changed
            summaries[-1] = summary
            atomic_json(summary_path, {**summary_file, "summaries": summaries[-20:]})
        else:
            summary["state"] = "outdated" if summary.get("state") == "outdated" else "written"
    else:
        summary = None
    beat = home / "state" / ".last-watcher-beat"
    try:
        beat_epoch = int(beat.stat().st_mtime) if beat.is_file() and not beat.is_symlink() else None
    except OSError:
        beat_epoch = None
    session_lock = home / "state" / ".lock"
    supervisor = {"watcher_beat_epoch": beat_epoch, "watcher_age_seconds": now - beat_epoch if beat_epoch else None, "session_lock_present": session_lock.exists()}
    return {"schema": SCHEMA, "project": project, "repository": repo, "generated_epoch": now, "last_checked_epoch": cat.get("checked_epoch"), "catalog": {key: cat.get(key) for key in ("complete", "partial", "known", "observed_known", "total", "error", "stale", "checked_epoch", "last_attempt_epoch", "throttled")}, "snapshot": {"collected_epoch": checked.get("collected_epoch"), "error": checked.get("error"), "stale": checked.get("stale", False)}, "remote_issue_coverage": remote_issue_coverage, "supervisor": supervisor, "fingerprint_schema": FP_SCHEMA, "fingerprint": fingerprint, "project_changed": changed, "rows": rows, "unlinked_tasks": unlinked, "summary_requests": list_summary_requests(home, project), "summary": summary}


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
            results = dict(value.get("results", {}))
            if int(value.get("expires_epoch", 0)) <= utc_now():
                results = {name: ("expired" if result == "pending" else result) for name, result in results.items()}
            value["results"] = results
            states = list(results.values())
            value["state"] = ("pending" if "pending" in states else
                              "failed" if "failed" in states else
                              "unavailable" if "unavailable" in states else
                              "expired" if "expired" in states else
                              "outdated" if "outdated" in states else "written")
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
    resolve = sub.add_parser("resolve")
    resolve.add_argument("request_id")
    resolve.add_argument("project")
    resolve.add_argument("result", choices=("failed", "unavailable"))
    resolve.add_argument("--reason", required=True)
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.command in ("put", "resolve") and not re.fullmatch(r"[0-9]{1,12}-[a-f0-9]{12}", args.request_id):
        print("error: invalid summary request identity", file=sys.stderr)
        return 2
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
            if now >= int(item.get("expires_epoch", 0)):
                continue
            for name, result in item.get("results", {}).items():
                if result == "pending":
                    existing_by_project.setdefault(name, item)
        attached = {name: existing_by_project[name]["id"] for name in selected if name in existing_by_project}
        selected = [name for name in selected if name not in existing_by_project]
        availability = supervisor_availability(home)
        if not selected:
            print(json.dumps({"status": "pending", "requests": attached, "deduplicated": True, "supervisor_availability": availability}))
            return 0
        ident = f"{now}-{secrets.token_hex(6)}"
        record = {"schema": "fm-status-summary-request.v1", "id": ident, "requested_epoch": now, "requested_at": iso_utc(now), "projects": selected, "basis_fingerprints": {name: projections[name]["fingerprint"] for name in selected}, "state": "pending", "results": {name: "pending" for name in selected}, "expires_epoch": now + 1800, "supervisor_availability": supervisor_availability(home), "route": "main-home inbox; use marked fm-send and pending-reply correlation for registered project homes"}
        atomic_json(reqdir / f"{ident}.json", record)
        note = [str(ROOT / "bin" / "fm-inbox.sh"), "note", f"Request Manual Update id={ident} projects={','.join(selected)}"]
        env = os.environ.copy()
        env["FM_HOME"] = str(home)
        sent = subprocess.run(note, env=env, cwd=ROOT, capture_output=True, text=True, timeout=10)
        if sent.returncode:
            record["error"] = sent.stderr[-500:] or "inbox notification failed"
            record["results"] = {name: "failed" for name in selected}
            record["result_reasons"] = {name: record["error"] for name in selected}
            record["state"] = "failed"
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
        if record.get("results", {}).get(args.project) != "pending":
            print(json.dumps({"status": record.get("results", {}).get(args.project, record.get("state", "unavailable")), "project": args.project}))
            return 1
        if not re.fullmatch(r"[a-f0-9]{64}", args.basis_fingerprint) or args.basis_observed_at < 0 or not args.author.strip() or len(args.author) > 160:
            print("error: invalid summary basis or author", file=sys.stderr)
            return 2
        text = Path(args.text_file).read_text(encoding="utf-8")
        if not text.strip() or len(text.encode()) > 16_384:
            print("error: summary must be nonempty and at most 16384 bytes", file=sys.stderr)
            return 2
        current = make_projection(home, args.project)
        outdated = current["fingerprint"] != args.basis_fingerprint
        written_epoch = utc_now()
        summary = {"schema": "fm-status-summary.v1", "request": args.request_id, "project": args.project, "author": args.author, "basis_fingerprint": args.basis_fingerprint, "basis_observed_epoch": args.basis_observed_at, "written_epoch": written_epoch, "text": text, "state": "outdated" if outdated else "written", "current_fingerprint": current["fingerprint"], "invalidated_epoch": written_epoch if outdated else None, "invalidated_change": current.get("project_changed") if outdated else None, "evidence": {"basis_fingerprint": args.basis_fingerprint, "basis_observed_epoch": args.basis_observed_at, "comparison_fingerprint": current["fingerprint"], "comparison_observed_epoch": current["generated_epoch"], "repository": current["repository"], "catalog_checked_epoch": current["last_checked_epoch"], "snapshot_collected_epoch": current["snapshot"]["collected_epoch"]}}
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
    if args.command == "resolve":
        path = reqdir / f"{args.request_id}.json"
        record = read_json(path)
        if not isinstance(record, dict) or record.get("schema") != "fm-status-summary-request.v1" or args.project not in record.get("projects", []):
            print("error: unknown request or project", file=sys.stderr)
            return 2
        if int(record.get("expires_epoch", 0)) <= utc_now():
            record["results"] = {name: ("expired" if value == "pending" else value) for name, value in record.get("results", {}).items()}
            record["state"] = "expired"
            atomic_json(path, record)
            print(json.dumps({"status": "expired", "project": args.project}))
            return 1
        if record.get("results", {}).get(args.project) != "pending":
            print(json.dumps({"status": record.get("results", {}).get(args.project), "project": args.project}))
            return 1
        record.setdefault("results", {})[args.project] = args.result
        record.setdefault("result_reasons", {})[args.project] = args.reason[:1000]
        values = record["results"].values()
        record["state"] = "pending" if "pending" in values else "failed" if "failed" in values else "unavailable" if "unavailable" in values else "outdated" if "outdated" in values else "written"
        atomic_json(path, record)
        print(json.dumps({"status": args.result, "project": args.project}))
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
function detail(task){const box=document.createElement('details'),summary=document.createElement('summary');summary.textContent=`${task.id||'task'}: ${task.stage}; activity ${task.task_state||'unknown'} (${task.activity_source||'source unknown'})`;box.append(summary);const content=document.createElement('div');content.append(text(`Lifecycle: ${task.event?.kind||'not recorded'} (${task.event?.class||'unknown'}); generation ${task.generation||'unknown'}\n`));content.append(text(`PRs: ${task.prs?.map(pr=>`${pr.url} (${pr.state||'unknown'})`).join('; ')||'none'}\n`));content.append(text(`Focused: ${task.verification?.focused?.status||'unknown'}; Full: ${task.verification?.full?.status||'unknown'}; Verify: ${task.verification?.verify?.status||'unknown'}\n`));content.append(text(`Source accepted: ${task.source_accepted||'not recorded'}; Canonical journeys: ${task.canonical_journeys||'not recorded'}; Ready: ${task.ready_for_approval||'unknown'}\n`));content.append(text(`Evidence basis: ${task.verification?.full?.basis||'forge and typed lifecycle records; absent evidence stays unknown'}`));box.append(content);return box}
function verification(row){const box=document.createElement('div');for(const task of row.tasks||[])box.append(detail(task));if(!row.tasks?.length)box.textContent='No recorded Firstmate work';return box}
function changed(value){if(value?.class==='event')return `${time(value.at_epoch)} (event)`;if(value?.class==='detected')return `between ${time(value.from_epoch)} and ${time(value.to_epoch)} (detected)`;return 'unknown'}
function renderRow(row){const tr=document.createElement('tr'),issue=document.createElement('td'),link=document.createElement('a'),stage=document.createElement('td'),next=document.createElement('td'),prs=document.createElement('td'),verify=document.createElement('td'),when=document.createElement('td');link.href=row.url;link.textContent=row.number?`#${row.number} ${row.title}`:row.url;issue.append(link);const count=document.createElement('small');count.textContent=`${row.task_count||0} tasks / ${row.pr_count||0} PRs`;stage.append(text(row.stage+'\n'),count);next.textContent=row.next_step||'Next step not recorded';if(row.conflicts?.length){const conflict=document.createElement('div');conflict.className='conflict';conflict.textContent=row.conflicts.join('; ');next.append(conflict)}for(const task of row.tasks||[])for(const pr of task.prs||[]){const a=document.createElement('a');a.href=pr.url;a.textContent=`${pr.url} (${pr.state||'unknown'})`;prs.append(a,text('\n'))}if(!row.pr_count)prs.textContent='None';verify.append(verification(row));when.textContent=changed(row.changed);tr.append(issue,stage,next,prs,verify,when);return tr}
async function load(force=false){const url='/api/status?project='+encodeURIComponent(selected)+(force?'&refresh=1':'');try{const response=await fetch(url),data=await response.json();if(!response.ok)throw Error(data.error||'collection unavailable');projects=data.projects||[];el('project').replaceChildren();for(const name of projects){const option=document.createElement('option');option.value=name;option.textContent=name;option.selected=name===selected;el('project').append(option)}const p=data.projection;if(!p)throw Error(data.error||'status unavailable');const cat=p.catalog,observed=cat.observed_known??0,coverage=cat.complete&&!cat.stale?`complete (${observed} observed of ${cat.total} total)`:`partial or unavailable (${cat.known} cached; ${observed} observed; total unknown)`,remote=p.remote_issue_coverage||{},remoteCoverage=`Remote summaries ${remote.shown_home_summaries||0}/${remote.registered_homes||0}; ${remote.visible_task_records||0} task records visible, ${remote.omitted_task_records||0} omitted, ${remote.unknown_home_summaries||0} unknown, ${remote.stale_home_summaries||0} stale${remote.complete?'':' (partial)'}`;el('status').className='muted';el('status').textContent=`${p.repository||'Repository unavailable'}; catalog ${coverage}; last checked ${time(p.last_checked_epoch)}. ${remoteCoverage}. Supervisor ${p.supervisor.session_lock_present?'session lock held':'no session lock'}; watcher beat ${time(p.supervisor.watcher_beat_epoch)}. ${cat.error||p.snapshot.error||''}`;el('rows').replaceChildren(...p.rows.map(renderRow));el('unlinked').replaceChildren(...p.unlinked_tasks.map(task=>{const li=document.createElement('li');li.textContent=`${task.id}: ${task.stage}; ${task.next_step||'Next step not recorded'}`;li.append(verification({tasks:[task]}));return li}));const fingerprintKey='fm-issues-fingerprint:'+selected,prior=localStorage.getItem(fingerprintKey);el('changed').textContent=prior&&prior!==p.fingerprint?'Project status changed since you last looked.':'';localStorage.setItem(fingerprintKey,p.fingerprint);renderSummary(p.summary);renderRequests(p.summary_requests)}catch(error){el('status').className='error';el('status').textContent='Unavailable: '+error.message}}
function renderSummary(summary){const root=el('summary');root.replaceChildren();if(!summary)return;const evidence=summary.evidence||{},basis=`Basis ${summary.basis_fingerprint||'unknown'} observed ${time(summary.basis_observed_epoch)}; repository ${evidence.repository||'unknown'}; snapshot ${time(evidence.snapshot_collected_epoch)}; catalog ${time(evidence.catalog_checked_epoch)}`;if(summary.state==='written'){root.textContent=`Written summary by ${summary.author}, based on status as of ${time(summary.basis_observed_epoch)}, written ${time(summary.written_epoch)}:\n${basis}\n${summary.text}`;return}if(summary.state==='outdated'){const details=document.createElement('details'),title=document.createElement('summary'),body=document.createElement('p'),basisLine=document.createElement('small');title.textContent=`Summary outdated since ${time(summary.invalidated_epoch)}; historical text retained`;basisLine.textContent=basis;body.textContent=summary.text;details.append(title,basisLine,body);root.append(details)}}
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
        catalog = value['catalog']
        if catalog['complete'] and not catalog['stale']:
            coverage = f"complete ({catalog.get('observed_known', 0)} observed of {catalog.get('total')} total)"
        else:
            coverage = f"partial or unavailable ({catalog.get('known', 0)} cached; {catalog.get('observed_known', 0)} observed; total unknown)"
        print(f"Catalog: {coverage}; last checked {pt(value['last_checked_epoch'])}")
        remote = value["remote_issue_coverage"]
        print(f"Remote current tasks: {remote['visible_task_records']} visible, {remote['omitted_task_records']} omitted; {remote['shown_home_summaries']} of {remote['registered_homes']} home summaries shown, {remote['unknown_home_summaries']} unknown, {remote['stale_home_summaries']} stale; {'complete' if remote['complete'] else 'partial'}")
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
