"""Pure issue-table semantics over owner-qualified task facts.

This module deliberately performs no filesystem, subprocess, clock, or forge
reads. Producers validate and normalize facts before calling these functions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

FINGERPRINT_SCHEMA = "fm-issues-fingerprint.v3"
STAGES = (
    "Unstarted", "Queued", "Investigating", "Implementing", "In review",
    "Revising", "Ready for approval", "Merging", "Completed",
    "Closed without delivery", "Unknown",
)
STAGE_ORDER = {
    "Queued": 3, "Investigating": 4, "Implementing": 5, "Revising": 6,
    "In review": 7, "Merging": 8, "Completed": 9,
    "Closed without delivery": 10,
}


def stage(task: dict[str, Any], linked_prs: list[dict[str, Any]], backlog: dict[str, Any] | None) -> tuple[str, str | None]:
    current = task.get("current_state", {}).get("state")
    hints = task.get("hints") or {}
    backlog = backlog or {}
    unresolved = backlog.get("unresolved_blocker_ids") or []
    waiting = None
    if backlog.get("hold_bucket") == "live" or hints.get("pending_decision") is True:
        waiting = "your decision"
    elif unresolved or hints.get("blocked_event") is True:
        waiting = "blocked"
    elif backlog.get("hold_bucket") in ("dated", "aged") or current == "paused":
        waiting = "paused"
    if backlog.get("state") == "queued":
        return "Queued", "prerequisite" if unresolved else waiting
    if linked_prs:
        if any(pr.get("state_known", pr.get("forge_checked") is True) and pr.get("state") == "merged" for pr in linked_prs):
            return ("Unknown" if backlog.get("state") == "in_flight" else "Completed"), waiting
        merge_requests = task.get("merge_requests") or []
        if any(item.get("url") == pr.get("url") and pr.get("state") == "open" for item in merge_requests for pr in linked_prs):
            return "Merging", waiting
        if not any(pr.get("state_known", pr.get("forge_checked") is True) and pr.get("state") == "open" for pr in linked_prs):
            return "Unknown", waiting
        if any(pr.get("revising") is True for pr in linked_prs):
            return "Revising", waiting
    if task.get("kind") == "scout" and backlog.get("state") == "in_flight":
        return "Investigating", waiting
    if task.get("kind") == "ship" and backlog.get("state") == "in_flight" and not linked_prs:
        return "Implementing", waiting
    if linked_prs:
        return "In review", waiting
    if current == "done" or backlog.get("state") == "done":
        return "Closed without delivery", waiting
    if current == "failed":
        return "Unknown", waiting or "failed"
    return "Unknown", waiting


def forge_pr_fact(url: str, contribution: dict[str, Any] | None, association: str) -> dict[str, Any]:
    """Normalize one contributions.jq row without re-reading forge payloads."""
    checked = isinstance(contribution, dict) and contribution.get("checked") is True
    forge = contribution.get("forge") if isinstance(contribution, dict) and isinstance(contribution.get("forge"), dict) else {}
    known = bool(forge)
    checks = forge.get("checks") if isinstance(forge.get("checks"), list) else None
    reviews = contribution.get("reviews", []) if isinstance(contribution, dict) and isinstance(contribution.get("reviews"), list) else []
    decision = forge.get("review_decision") if known else None
    requested = [review for review in reviews if isinstance(review, dict) and review.get("state") == "CHANGES_REQUESTED"]
    failed = _integer(contribution.get("failed_checks")) if isinstance(contribution, dict) else 0
    return {"url": url, "state": forge.get("state", "unknown") if known else "unknown",
            "head": forge.get("head") if known else None, "draft": forge.get("draft") if known else None,
            "review_decision": decision, "mergeable": forge.get("mergeable") if known else None,
            "checks": checks, "reviews": reviews,
            "outstanding_changes_requested": decision == "CHANGES_REQUESTED" and (not requested or any(review.get("freshness") == "current" for review in requested)),
            "revising": bool(known and forge.get("head") and any(review.get("freshness") == "STALE" for review in requested)),
            "checked_at": contribution.get("checked_at") if isinstance(contribution, dict) else None,
            "forge_checked": checked, "state_known": known,
            "evidence_freshness": "current" if checked else "stale" if known else "unknown",
            "missing_verdicts": contribution.get("missing_verdicts", 0) if isinstance(contribution, dict) else 0,
            "stale_verdicts": contribution.get("stale_verdicts", 0) if isinstance(contribution, dict) else 0,
            "pending_checks": contribution.get("pending_checks", 0) if isinstance(contribution, dict) else 0,
            "failed_checks": failed, "association": association}


def next_step(task: dict[str, Any]) -> str:
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
    prs = task.get("prs", [])
    if any(pr.get("review_decision") == "CHANGES_REQUESTED" for pr in prs):
        return "Changes requested; revise the current PR head"
    if any(pr.get("failed_checks", 0) for pr in prs):
        return "Checks failing on the current PR head"
    if any(pr.get("pending_checks", 0) for pr in prs):
        running = sum(pr.get("pending_checks", 0) for pr in prs)
        total = sum(len(pr.get("checks") or []) for pr in prs)
        return f"Waiting on CI ({running} of {total} checks running)"
    if any(pr.get("review_decision") == "REVIEW_REQUIRED" for pr in prs):
        return "Review required"
    if task.get("stage") == "Ready for approval":
        return "Ready for your approval"
    if task.get("stage") == "Queued":
        return "Queued"
    return "Next step not recorded"


def ready_state(task: dict[str, Any], configured_lanes: list[str]) -> str:
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
    if any(task.get("verification", {}).get(lane, {}).get("status") != "passed" for lane in configured_lanes):
        return "not ready"
    return "ready for approval"


def task_rank(task: dict[str, Any]) -> tuple[int, str]:
    if task.get("waiting") == "your decision":
        return 0, task.get("id") or ""
    if (task.get("conflicts") or task.get("waiting") in ("prerequisite", "blocked", "failed")
            or task.get("task_state") == "failed" or any(pr.get("failed_checks", 0) for pr in task.get("prs", []))):
        return 1, task.get("id") or ""
    if task.get("stage") == "Ready for approval":
        return 2, task.get("id") or ""
    return STAGE_ORDER.get(task.get("stage"), 1), task.get("id") or ""


def fingerprint_pr(pr: dict[str, Any]) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for check in pr.get("checks") if isinstance(pr.get("checks"), list) else []:
        name = check.get("name")
        if not isinstance(name, str):
            continue
        old = latest.get(name)
        key = (str(check.get("started_at") or ""), _integer(check.get("id")))
        old_key = (str(old.get("started_at") or ""), _integer(old.get("id"))) if old else None
        if old is None or key > old_key:
            latest[name] = check
    return {"url": pr.get("url"), "state": pr.get("state"), "head": pr.get("head"), "draft": pr.get("draft"),
            "review_decision": pr.get("review_decision"), "failed_checks": pr.get("failed_checks", 0),
            "checks": [{"name": name, "status": value.get("status"), "conclusion": value.get("conclusion")}
                       for name, value in sorted(latest.items())]}


def _integer(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def fingerprint_task(task: dict[str, Any]) -> dict[str, Any]:
    backlog = task.get("backlog") or {}
    return {"id": task.get("id"), "generation": task.get("generation"), "kind": task.get("kind"),
            "source_head": task.get("source_head"), "stage": task.get("stage"), "waiting": task.get("waiting"),
            "next_step": task.get("next_step"), "backlog_state": backlog.get("state"),
            "unresolved_blocker_ids": sorted(backlog.get("unresolved_blocker_ids") or []),
            "hold_reason": backlog.get("hold_reason"), "open_decisions": task.get("hints", {}).get("open_decisions", []),
            "conflicts": sorted(task.get("conflicts", [])),
            "prs": sorted((fingerprint_pr(pr) for pr in task.get("prs", [])), key=lambda pr: pr.get("url") or ""),
            "verification": {lane: task.get("verification", {}).get(lane, {}).get("status") for lane in ("focused", "full", "verify")}}


def semantic_fingerprint(rows: list[dict[str, Any]], unlinked: list[dict[str, Any]]) -> str:
    relevant = [{"url": row["url"], "state": row.get("forge_state"), "title": row.get("title"),
                 "stage": row.get("stage"), "conflicts": sorted(row.get("conflicts", [])),
                 "tasks": [fingerprint_task(task) for task in row.get("tasks", [])]} for row in rows]
    relevant.sort(key=lambda value: value["url"])
    payload = {"schema": FINGERPRINT_SCHEMA, "rows": relevant,
               "unlinked_tasks": [fingerprint_task(task) for task in unlinked]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def choose_change(event_facts: list[dict[str, Any]], detected_bracket: dict[str, int] | None) -> dict[str, Any]:
    """Prefer exact owner event time, then an honest bracket, else unknown."""
    events = [item for item in event_facts if item.get("class") == "event" and isinstance(item.get("at_epoch"), int)]
    if events:
        return {"class": "event", "at_epoch": max(events, key=lambda item: item["at_epoch"])["at_epoch"]}
    if detected_bracket and isinstance(detected_bracket.get("from_epoch"), int) and isinstance(detected_bracket.get("to_epoch"), int):
        if 0 <= detected_bracket["to_epoch"] - detected_bracket["from_epoch"] <= 86400:
            return {"class": "detected", **detected_bracket}
    return {"class": "unknown"}
