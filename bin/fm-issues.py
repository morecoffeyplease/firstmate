#!/usr/bin/env python3
"""Local deterministic project issue table and status-summary request owner."""

from __future__ import annotations

import argparse
import contextlib
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
from fm_lane_receipts import classify_finished_receipt, repository_identity, valid_receipt
from fm_issues_derive import (
    FINGERPRINT_SCHEMA as FP_SCHEMA,
    fingerprint_pr as derive_fingerprint_pr,
    fingerprint_task as derive_fingerprint_task,
    choose_change,
    forge_pr_fact,
    next_step as derive_next_step,
    owner_task_fact,
    ready_state as derive_ready_state,
    semantic_fingerprint,
    stage as derive_stage,
    transition_watermark,
    task_rank,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "fm-issues.v1"
MAX_CATALOG_AGE = 3600
MAX_PROJECTION_AGE = 60
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


@contextlib.contextmanager
def project_summary_lock(home: Path, project: str):
    root = home / "state" / "status-summary"
    if root.is_symlink():
        raise ValueError("status summary state is a symlink")
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f".{hashlib.sha256(project.encode()).hexdigest()}.lock"
    if lock_path.is_symlink():
        raise ValueError("project summary lock is a symlink")
    import fcntl
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def invalidate_projection_cache(home: Path, project: str) -> None:
    cache_dir = home / "state" / "issue-status"
    if cache_dir.is_symlink():
        return
    path = cache_dir / f"projection-{hashlib.sha256(project.encode()).hexdigest()}.json"
    if path.is_file() and not path.is_symlink():
        path.unlink(missing_ok=True)


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
        repository = "/".join(part.lower() for part in path.split("/"))
        catalog_state = home / "state" / "issue-catalog"
        if catalog_state.is_symlink():
            return None, "repository identity state directory is unsafe"
        catalog_state.mkdir(parents=True, exist_ok=True)
        pins = catalog_state / "identity-pins"
        if pins.is_symlink():
            return None, "repository identity pin directory is unsafe"
        pins.mkdir(parents=True, exist_ok=True)
        pin = pins / f"{hashlib.sha256(project.encode()).hexdigest()}.json"
        if pin.is_symlink():
            return None, "repository identity pin is unsafe"
        prior = read_json(pin, 4096)
        if not pin.exists():
            atomic_json(pin, {"schema": "fm-issue-repository-pin.v1", "project": project, "repository": repository})
        elif (not isinstance(prior, dict) or prior.get("schema") != "fm-issue-repository-pin.v1"
              or prior.get("project") != project or prior.get("repository") != repository):
            return None, "registered project origin conflicts with its pinned repository identity"
        return repository, None
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


def issue_inventory(summary: dict) -> tuple[list[dict], bool]:
    """Read a complete ordered batch inventory, or retain legacy bounded rows."""
    inventory = summary.get("issue_inventory")
    if not isinstance(inventory, dict) or inventory.get("complete") is not True:
        rows = summary.get("issue_tasks", [])
        return ([item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []), False
    total, size, count, pages = (inventory.get("total"), inventory.get("page_size"),
                                 inventory.get("page_count"), inventory.get("pages"))
    if (not isinstance(total, int) or total < 0 or not isinstance(size, int) or size <= 0
            or not isinstance(count, int) or not isinstance(pages, list) or len(pages) != count
            or inventory.get("next_offset") is not None):
        return [], False
    flattened = []
    for index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get("offset") != index * size:
            return [], False
        expected_next = (index + 1) * size if index + 1 < count else None
        if page.get("next_offset") != expected_next or not isinstance(page.get("tasks"), list):
            return [], False
        if len(page["tasks"]) > size or any(not isinstance(item, dict) for item in page["tasks"]):
            return [], False
        flattened.extend(page["tasks"])
    if len(flattened) != total:
        return [], False
    if any(not isinstance(item.get("id"), str) or not isinstance(item.get("current_state"), dict)
           or not isinstance(item.get("backlog"), dict) for item in flattened):
        return [], False
    if len({item["id"] for item in flattened}) != total:
        return [], False
    known_total = (summary.get("counts") or {}).get("issue_tasks")
    if isinstance(known_total, int) and known_total != total:
        return [], False
    return flattened, True


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
    return derive_stage(task, linked_prs, backlog)


def next_step(task: dict) -> str:
    return derive_next_step(task)


def ready_state(task: dict, configured: dict[str, list[str]]) -> str:
    return derive_ready_state(task, read_project_lanes_for_ready(configured))


def read_project_lanes_for_ready(configured: dict[str, list[str]]) -> list[str]:
    return [name for name in ("full", "verify") if name in configured]


def fingerprint_pr(pr: dict) -> dict:
    return derive_fingerprint_pr(pr)


def safe_integer(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def fingerprint_task(task: dict) -> dict:
    return derive_fingerprint_task(task)


def process_start_matches(pid: int, expected: str | None) -> bool:
    if not expected:
        return False
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return result.returncode == 0 and result.stdout.strip() == expected
    except (OSError, subprocess.SubprocessError):
        return False


def worktree_dirty(path: str | None) -> bool | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        result = subprocess.run(["git", "-C", path, "status", "--porcelain", "--untracked-files=all"],
                                capture_output=True, text=True, timeout=3)
        return result.stdout != "" if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def lane_status(home: Path, task: dict, project: str, configured: dict[str, list[str]] | None = None, current_head: str | None = None) -> dict[str, dict]:
    configured = configured if configured is not None else read_project_lanes(home).get(project, {})
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
    if current_head is None and isinstance(worktree, str) and worktree:
        try:
            current_head = subprocess.run(["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    current_dirty = task.get("source_dirty") if "source_dirty" in task else worktree_dirty(worktree)
    records: dict[str, list[dict]] = {lane: [] for lane in ("focused", "full", "verify")}
    malformed: dict[str, bool] = {lane: False for lane in records}
    receipt_errors: dict[str, list[tuple[int, str]]] = {lane: [] for lane in records}
    error_path = receipt_dir / ".errors"
    try:
        if error_path.is_symlink() or (error_path.exists() and (not error_path.is_file() or error_path.stat().st_size > 65536)):
            receipt_errors = {lane: [(0, "receipt error journal is unsafe")] for lane in records}
        elif error_path.exists():
            for line in error_path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^(\d+) task=([A-Za-z0-9._-]+) generation=([A-Za-z0-9._-]+) lane=(focused|full|verify) (.{1,300})$", line)
                if match:
                    if match.group(2) == task_id and match.group(3) == generation:
                        receipt_errors[match.group(4)].append((int(match.group(1)), match.group(5)))
                    continue
                legacy = re.match(r"^(\d+) (.{1,300})$", line)
                if legacy:
                    for lane in records:
                        receipt_errors[lane].append((int(legacy.group(1)), legacy.group(2)))
                else:
                    receipt_errors = {lane: [(0, "receipt error journal is malformed")] for lane in records}
                    break
    except (OSError, UnicodeError):
        receipt_errors = {lane: [(0, "receipt error journal cannot be read")] for lane in records}
    expected_repo, _repo_error = canonical_repo(home, project)
    for lane in records:
        for path in receipt_dir.glob(f"{lane}-*.json"):
            value = read_json(path, 2_000_000)
            if not isinstance(value, dict):
                malformed[lane] = True
                continue
            valid = valid_receipt(value, path, lane, task_id, generation, project, expected_repo)
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
        relevant_errors = [(stamp, message) for stamp, message in receipt_errors[lane] if not chosen or stamp >= chosen.get("started_epoch", 0)]
        if relevant_errors:
            result[lane] = {"status": "unknown", "basis": "a validated receipt write failed", "error": relevant_errors[-1][1]}
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
        else:
            state = {
                "passed-dirty": "passed on uncommitted or changed source",
                "stale": "stale (passed on an older head)",
            }.get(classify_finished_receipt(chosen, generation, current_head),
                  classify_finished_receipt(chosen, generation, current_head))
            if state == "passed" and current_dirty is True:
                state = "passed on uncommitted or changed source"
            elif state == "passed" and current_dirty is not False:
                state = "unknown"
        error = next((message for stamp, message in reversed(receipt_errors[lane]) if stamp >= chosen["started_epoch"]), None)
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
    try:
        validated = subprocess.run(
            ["/bin/bash", "-c", 'source "$1" && fm_issue_event_validate_file "$2" "$3"', "fm-event-validate", str(ROOT / "bin" / "fm-issue-events-lib.sh"), str(path), task_id],
            capture_output=True,
            timeout=5,
        )
        if validated.returncode != 0:
            return None
        events = [
            item for item in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
            if item.get("generation") == generation
        ]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    if not events:
        return None
    latest = dict(events[-1])
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
    configured_lanes = read_project_lanes(home).get(project, {})
    remote_issue_coverage = {"registered_homes": 0, "shown_home_summaries": 0, "unknown_home_summaries": 0, "stale_home_summaries": 0, "visible_task_records": 0, "omitted_task_records": 0, "omitted_scope_unknown": False, "complete": False}
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
            issue_tasks, page_inventory_complete = issue_inventory(secondmate)
            inventory_proven = (isinstance((secondmate.get("counts") or {}).get("issue_tasks"), int)
                                and (page_inventory_complete or "issue_inventory" not in secondmate))
            relevant_tasks = [item for item in issue_tasks if isinstance(item, dict) and (item.get("backlog") or {}).get("repo") == project]
            remote_issue_coverage["visible_task_records"] += len(relevant_tasks)
            omissions_by_project = secondmate.get("omitted_issue_tasks_by_project")
            if page_inventory_complete:
                pass
            elif isinstance(omissions_by_project, list):
                remote_issue_coverage["omitted_task_records"] += sum(item.get("count", 0) for item in omissions_by_project if isinstance(item, dict) and item.get("project") == project and isinstance(item.get("count"), int))
                unknown_scope_count = secondmate.get("omitted_issue_tasks_scope_unknown")
                if isinstance(unknown_scope_count, int) and unknown_scope_count > 0:
                    remote_issue_coverage["omitted_scope_unknown"] = True
                    remote_issue_coverage["complete"] = False
            else:
                unscoped_omitted = sum(item.get("count", 0) for item in secondmate.get("omitted", []) if item.get("surface") == "issue_tasks" and isinstance(item.get("count"), int))
                if unscoped_omitted:
                    remote_issue_coverage["omitted_scope_unknown"] = True
                    remote_issue_coverage["complete"] = False
            if not valid_summary or not inventory_proven:
                remote_issue_coverage["unknown_home_summaries"] += 1
                remote_issue_coverage["complete"] = False
            if remote_issue_coverage["omitted_task_records"] > 0:
                remote_issue_coverage["complete"] = False
            if summary_freshness != "fresh":
                remote_issue_coverage["stale_home_summaries"] += 1
                remote_issue_coverage["complete"] = False
        contribution_rows = (snap.get("contributions") or {}).get("rows", [])
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
                linked.append(forge_pr_fact(pr_url, contribution, "task-linked; issue-specific PR relation not recorded"))
            task_id = task.get("id")
            generation = task.get("spawn_gen")
            event = task_event_fact(home, task_id, generation)
            event_history = event.get("history", []) if isinstance(event, dict) else []
            latest_event = {key: value for key, value in event.items() if key != "history"} if isinstance(event, dict) else None
            source_head = worktree_head(task)
            verification = lane_status(home, task, project, configured_lanes, source_head)
            task_row = {"id": task_id, "generation": generation, "kind": task.get("kind"), "task_state": task.get("current_state", {}).get("state", "unknown"), "activity_source": task.get("current_state", {}).get("source", "unknown"), "activity_detail": task.get("current_state", {}).get("detail"), "current_state": task.get("current_state", {}), "hints": task.get("hints", {}), "source_head": source_head, "backlog": backlog, "issues": task_links(task, repo), "prs": linked, "verification": verification, "event": latest_event, "event_history": event_history, "merge_requests": [{"url": item.get("fields", {}).get("url"), "at_epoch": item.get("at_epoch")} for item in event_history if item.get("kind") == "merge-requested"]}
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
                unresolved = sorted(set(row.get("unresolved_blocker_ids") or []))
                candidates.append({"id": row.get("id"), "generation": None, "kind": row.get("kind"), "task_state": "unknown" if orphan else "queued", "backlog": row, "issues": canonical_issue_urls(row.get("links", []), repo), "prs": [], "stage": "Unknown" if orphan else "Queued", "waiting": "prerequisite" if unresolved and not orphan else None, "next_step": "Next step not recorded" if orphan else (f"Queued behind {', '.join(unresolved)}" if unresolved else "Queued"), "conflicts": ["backlog is in flight but task metadata is missing"] if orphan else [], "verification": {lane: {"status": "not instrumented"} for lane in ("focused", "full", "verify")}, "ready_for_approval": "not ready", "source_accepted": "not recorded", "canonical_journeys": "not recorded", "event": None})
        secondmates = remote_records
        for secondmate in secondmates:
            home_id = secondmate.get("id") or "registered-home"
            remote_tasks, _pages_complete = issue_inventory(secondmate)
            for remote in remote_tasks:
                backlog = remote.get("backlog") if isinstance(remote.get("backlog"), dict) else {}
                issue_urls = canonical_issue_urls(backlog.get("links", []), repo)
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
                    linked_prs.append(forge_pr_fact(pr_url, contribution, "task-linked; issue-specific PR relation not recorded"))
                current_state = remote.get("current_state") if isinstance(remote.get("current_state"), dict) else {"state": "unknown", "source": "remote summary unavailable"}
                summary_valid = (secondmate.get("provenance") or {}).get("summary_valid") is True
                summary_fresh = (secondmate.get("freshness") or {}).get("status") == "fresh"
                decisions = [item for item in secondmate.get("decisions_open", []) if isinstance(item, dict) and item.get("id") == source_id and item.get("verb") in ("needs-decision", "captain-hold")]
                remote_event = remote.get("event") if isinstance(remote.get("event"), dict) else None
                remote_events = remote.get("event_history") if isinstance(remote.get("event_history"), list) else []
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
                    "hints": {"open_decisions": decisions, "pending_decision": any(item.get("verb") in ("needs-decision", "captain-hold") for item in decisions), "blocked_event": (remote.get("hints") or {}).get("blocked_event") is True},
                    "source_head": (remote.get("source") or {}).get("head"),
                    "source_dirty": (remote.get("source") or {}).get("dirty"),
                    "backlog": backlog,
                    "issues": issue_urls,
                    "prs": linked_prs,
                    "verification": (remote.get("verification") if isinstance(remote.get("verification"), dict)
                                     else {lane: {"status": "unknown", "basis": "owner lane evidence is not included in the validated home summary"} for lane in ("focused", "full", "verify")}),
                    "event": remote_event,
                    "event_history": remote_events,
                    "merge_requests": [],
                    "evidence_freshness": "fresh" if summary_valid and summary_fresh else "stale" if summary_valid else "unknown",
                    "source_accepted": "not recorded",
                    "canonical_journeys": "not recorded",
                    "conflicts": ([] if summary_valid else ["remote home summary is not valid for complete current-state evidence"])
                        + (["task PR URL has an unsupported identity"] if raw_remote_pr_url and not pr_url else []),
                }
                task_row["stage"], task_row["waiting"] = stage(task_row, linked_prs, backlog)
                task_row["next_step"] = next_step(task_row)
                task_row["ready_for_approval"] = "unknown" if task_row["evidence_freshness"] != "fresh" else ready_state(task_row, configured_lanes)
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
        chosen_task = min(linked, key=task_rank) if linked else None
        chosen = chosen_task["stage"] if chosen_task else ("Unstarted" if issue["state"] == "open" and remote_issue_coverage["complete"] else ("Unknown" if issue["state"] == "open" else "Closed without delivery"))
        conflicts = [conflict for task in linked for conflict in task.get("conflicts", [])]
        if issue["state"] == "closed" and any(task.get("task_state") in ("working", "busy", "paused", "running") for task in linked):
            conflicts.append("forge issue is closed while local task remains active")
        rows.append({**issue, "forge_state": issue["state"], "tasks": linked, "task_count": len(linked), "pr_count": sum(len(task.get("prs", [])) for task in linked), "conflicts": conflicts, "freshness": "conflicting" if conflicts else ("stale" if cat.get("stale") else "current"), "stage": chosen, "next_step": chosen_task.get("next_step") if chosen_task else ("No recorded Firstmate work" if issue["state"] == "open" else "Closed; delivery not recorded"), "changed": {"class": "unknown"}})
    unlinked = sorted((task for task in candidates if not task["issues"]), key=lambda task: task.get("id") or "")
    state_cache = home / "state" / "issue-status" / f"{hashlib.sha256(project.encode()).hexdigest()}.json"
    previous = read_json(state_cache)
    prior_rows = previous.get("rows", {}) if isinstance(previous, dict) and previous.get("schema") == FP_SCHEMA and isinstance(previous.get("rows"), dict) else {}
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
            task.update(owner_task_fact(task, task.get("owner_home_id", "main-home"),
                                        task.get("task_id", task.get("id", "")),
                                        read_project_lanes_for_ready(configured_lanes)))
    for task in unlinked:
        task.update(owner_task_fact(task, task.get("owner_home_id", "main-home"),
                                    task.get("task_id", task.get("id", "")),
                                    read_project_lanes_for_ready(configured_lanes)))
    for row in rows:
        linked = row.get("tasks", [])
        if not linked:
            continue
        chosen_task = min(linked, key=task_rank)
        row["stage"] = chosen_task.get("stage", "Unknown")
        row["next_step"] = chosen_task.get("next_step")
    fingerprint = semantic_fingerprint(rows, unlinked)
    previous_fp = previous.get("fingerprint") if isinstance(previous, dict) and previous.get("schema") == FP_SCHEMA else None
    previous_seen = previous.get("observed_epoch") if isinstance(previous, dict) and previous.get("schema") == FP_SCHEMA else None
    changed = {"class": "unknown"}
    project_event_facts = []
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
                event_facts = [
                    {"class": event.get("class"), "at_epoch": event.get("at_epoch"),
                     "from_epoch": event.get("fields", {}).get("from_epoch"),
                     "to_epoch": event.get("fields", {}).get("to_epoch")}
                    for event in changed_events
                ]
                project_event_facts.extend(event_facts)
                detected = [
                    event for event in changed_events
                    if event.get("class") == "detected"
                    and isinstance(event.get("fields", {}).get("from_epoch"), int)
                    and isinstance(event.get("fields", {}).get("to_epoch"), int)
                ]
                newest = max(detected, key=lambda event: event["fields"]["to_epoch"], default=None)
                bracket = (
                    {"from_epoch": newest["fields"]["from_epoch"], "to_epoch": newest["fields"]["to_epoch"]}
                    if newest else {"from_epoch": int(old.get("observed_epoch", 0)), "to_epoch": now}
                )
                row_changed = choose_change(event_facts, bracket)
        row["changed"] = row_changed
        next_rows[row["url"]] = {"fingerprint": row_fingerprint, "observed_epoch": now, "title": row.get("title"), "state": row.get("forge_state"), "changed": row_changed, "events": {task.get("id"): task.get("event") for task in row.get("tasks", []) if task.get("event")}, "tasks": [fingerprint_task(task) for task in row.get("tasks", [])]}
    changed = {"class": "unknown"}
    if previous_fp and previous_fp != fingerprint and previous_seen:
        detected_project = [item for item in project_event_facts
                            if item.get("class") == "detected"
                            and isinstance(item.get("from_epoch"), int)
                            and isinstance(item.get("to_epoch"), int)]
        newest_detected = max(detected_project, key=lambda item: item["to_epoch"], default=None)
        project_bracket = ({"from_epoch": newest_detected["from_epoch"], "to_epoch": newest_detected["to_epoch"]}
                           if newest_detected else {"from_epoch": int(previous_seen), "to_epoch": now})
        changed = choose_change(project_event_facts, project_bracket)
    elif previous_fp == fingerprint:
        changed = previous.get("changed", changed)
    if previous_fp != fingerprint or prior_rows != next_rows:
        atomic_json(state_cache, {"schema": FP_SCHEMA, "fingerprint": fingerprint, "observed_epoch": now, "changed": changed, "rows": next_rows})
    watermark = transition_watermark(rows, unlinked)
    summary = summary_record_for_projection(home, project, fingerprint, watermark, now, changed)
    beat = home / "state" / ".last-watcher-beat"
    try:
        beat_epoch = int(beat.stat().st_mtime) if beat.is_file() and not beat.is_symlink() else None
    except OSError:
        beat_epoch = None
    session_lock = home / "state" / ".lock"
    supervisor = {"watcher_beat_epoch": beat_epoch, "watcher_age_seconds": now - beat_epoch if beat_epoch else None, "session_lock_present": session_lock.exists()}
    return {"schema": SCHEMA, "project": project, "repository": repo, "generated_epoch": now, "last_checked_epoch": cat.get("checked_epoch"), "catalog": {key: cat.get(key) for key in ("complete", "partial", "known", "observed_known", "total", "error", "stale", "checked_epoch", "last_attempt_epoch", "throttled")}, "snapshot": {"collected_epoch": checked.get("collected_epoch"), "error": checked.get("error"), "stale": checked.get("stale", False)}, "remote_issue_coverage": remote_issue_coverage, "supervisor": supervisor, "fingerprint_schema": FP_SCHEMA, "fingerprint": fingerprint, "transition_watermark": watermark, "project_changed": changed, "rows": rows, "unlinked_tasks": unlinked, "summary_requests": list_summary_requests(home, project), "summary": summary}


def summary_record_for_projection(home: Path, project: str, fingerprint: str, watermark: str, now: int, changed: dict) -> dict | None:
    summary_path = home / "state" / "status-summary" / "summaries" / f"{hashlib.sha256(project.encode()).hexdigest()}.json"
    with project_summary_lock(home, project):
        summary_file = read_json(summary_path, 1_000_000)
        summaries = summary_file.get("summaries", []) if isinstance(summary_file, dict) and isinstance(summary_file.get("summaries"), list) else []
        summary = summaries[-1] if summaries else None
        if not isinstance(summary, dict) or summary.get("schema") != "fm-status-summary.v1":
            return None
        summary = dict(summary)
        outdated = (
            summary.get("basis_fingerprint") != fingerprint
            or summary.get("basis_transition_watermark") != watermark
        )
        if summary.get("state") != "outdated" and outdated:
            summary["state"] = "outdated"
            summary["invalidated_epoch"] = now
            summary["invalidated_change"] = changed
            summaries[-1] = summary
            atomic_json(summary_path, {**summary_file, "summaries": summaries[-20:]})
        else:
            summary["state"] = "outdated" if summary.get("state") == "outdated" else "written"
        return summary


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
            cache_dir = state / "issue-status"
            if cache_dir.is_symlink():
                raise ValueError("issue status cache directory is a symlink")
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / f"projection-{hashlib.sha256(project.encode()).hexdigest()}.json"
            if cache_path.is_symlink():
                raise ValueError("issue status projection cache is a symlink")
            cached = read_json(cache_path)
            now = utc_now()
            if (not refresh and isinstance(cached, dict)
                    and cached.get("schema") == "fm-issue-projection-cache.v1"
                    and isinstance(cached.get("cached_epoch"), int)
                    and now - cached["cached_epoch"] < MAX_PROJECTION_AGE
                    and isinstance(cached.get("projection"), dict)):
                projection = dict(cached["projection"])
                projection["projection_cached_epoch"] = cached["cached_epoch"]
                return projection
            projection = _make_projection(home, project, refresh)
            atomic_json(cache_path, {"schema": "fm-issue-projection-cache.v1", "cached_epoch": now, "projection": projection})
            projection["projection_cached_epoch"] = now
            return projection
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
            reasons = value.get("result_reasons", {})
            routes = value.get("routes", {})
            correlations = value.get("correlations", {})
            value["project_results"] = [
                {"project": name, "state": results.get(name, "unknown"),
                 "reason": reasons.get(name) if isinstance(reasons, dict) else None,
                 "route": (routes.get(name) or {}).get("route") if isinstance(routes, dict) and isinstance(routes.get(name), dict) else routes.get(name) if isinstance(routes, dict) else None,
                 "correlation": correlations.get(name) if isinstance(correlations, dict) else None}
                for name in value.get("projects", []) if project is None or name == project
            ]
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


def summary_route_candidates(home: Path, selected: list[str]) -> dict[str, dict]:
    registry = home / "data" / "secondmates.md"
    if not registry.exists() and not registry.is_symlink():
        return {name: {"route": "main-home", "target": None, "state": "pending"} for name in selected}
    script = '''
source "$1/fm-secondmate-registry-lib.sh" || exit 2
source "$1/fm-backend.sh" || exit 2
source "$1/fm-repo-concurrency-lib.sh" || exit 2
home=$2
registry=$3
shift 3
if ! secondmate_registry_validate_bindings "$registry" secondmate_registry_path_key; then
  printf 'ERROR\\tsecondmate registry validation failed\\n'
  exit 0
fi
for project in "$@"; do
  identity=$(fm_repo_scope_canonical_origin_identity "$home/projects/$project" 2>/dev/null || true)
  matches=()
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in "- "*) ;; *) continue ;; esac
    secondmate_registry_parse_line "$line" || continue
    id=$SECONDMATE_REGISTRY_ID
    project_names=",${SECONDMATE_REGISTRY_PROJECTS//[[:space:]]/},"
    case "$project_names" in *,"$project",*) ;; *) continue ;; esac
    if [ "$SECONDMATE_REGISTRY_REMOTE" -eq 1 ] || [ -n "$SECONDMATE_REGISTRY_REPO_IDENTITIES" ]; then
      [ -n "$identity" ] || continue
      identity_rows=",${SECONDMATE_REGISTRY_REPO_IDENTITIES//[[:space:]]/},"
      case "$identity_rows" in *,"$project=sha256:$identity",*) ;; *) continue ;; esac
    fi
    meta="$home/state/$id.meta"
    [ -f "$meta" ] && [ ! -L "$meta" ] || continue
    [ "$(fm_meta_get "$meta" kind 2>/dev/null || true)" = secondmate ] || continue
    matches+=("$id")
  done < "$registry"
  if [ "${#matches[@]}" -eq 1 ]; then
    printf '%s\\tsecondmate\\t%s\\t\\n' "$project" "${matches[0]}"
  elif [ "${#matches[@]}" -gt 1 ]; then
    printf '%s\\tunavailable\\t\\tproject maps to multiple registered secondmate routes\\n' "$project"
  else
    printf '%s\\tmain-home\\t\\t\\n' "$project"
  fi
done
'''
    try:
        result = subprocess.run(
            ["/bin/bash", "-c", script, "fm-summary-routes", str(ROOT / "bin"), str(home), str(registry), *selected],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {name: {"route": "unavailable", "target": None, "state": "unavailable", "reason": str(exc)[:300]} for name in selected}
    if result.returncode != 0:
        reason = result.stderr[-300:] or "secondmate registry could not be validated"
        return {name: {"route": "unavailable", "target": None, "state": "unavailable", "reason": reason} for name in selected}
    routes = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t", 3)
        if len(fields) == 4 and fields[0] in selected:
            route, target, reason = fields[1:]
            routes[fields[0]] = {"route": route, "target": target or None, "state": "unavailable" if route == "unavailable" else "pending", **({"reason": reason} if reason else {})}
    return {name: routes.get(name, {"route": "unavailable", "target": None, "state": "unavailable", "reason": "project route was not returned by registry validation"}) for name in selected}


def summary_pending_correlation(home: Path, target: str, request_id: str, project: str) -> str | None:
    marker = f"request={request_id} project={project}"
    script = '''
source "$1/fm-pending-reply-lib.sh" || exit 2
state=$2
task=$3
marker=$4
dir=$(fm_pending_reply_dir "$state")
[ -d "$dir" ] && [ ! -L "$dir" ] || exit 0
found=
for record in "$dir"/*; do
  [ -f "$record" ] && [ ! -L "$record" ] || continue
  [ "$(fm_pending_reply_get "$record" task_id)" = "$task" ] || continue
  summary=$(fm_pending_reply_get "$record" request_summary)
  case "$summary" in *"$marker"*) found=$(fm_pending_reply_get "$record" corr_id) ;; esac
done
case "$found" in [a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9][a-f0-9]) printf '%s\\n' "$found" ;; *) exit 0 ;; esac
'''
    try:
        result = subprocess.run(
            ["/bin/bash", "-c", script, "fm-summary-correlation", str(ROOT / "bin"), str(home / "state"), target, marker],
            capture_output=True,
            text=True,
            timeout=5,
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 and re.fullmatch(r"[a-f0-9]{16}", value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def summary_command(home: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fm-status-summary")
    sub = parser.add_subparsers(dest="command", required=True)
    request = sub.add_parser("request")
    request.add_argument("--project", action="append", required=True)
    route = sub.add_parser("route")
    route.add_argument("request_id")
    route.add_argument("project")
    route.add_argument("--target", required=True)
    route.add_argument("--correlation", required=True)
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("request_id")
    put = sub.add_parser("put")
    put.add_argument("request_id")
    put.add_argument("project")
    put.add_argument("--basis-fingerprint", required=True)
    put.add_argument("--basis-transition-watermark", required=True)
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
    if args.command in ("put", "resolve", "route", "dispatch") and not re.fullmatch(r"[0-9]{1,12}-[a-f0-9]{12}", args.request_id):
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
        routes = summary_route_candidates(home, selected)
        initial_results = {name: ("unavailable" if routes[name].get("state") == "unavailable" else "pending")
                           for name in selected}
        initial_reasons = {name: routes[name].get("reason") for name in selected
                           if routes[name].get("reason")}
        request_state = "pending" if "pending" in initial_results.values() else "unavailable"
        record = {"schema": "fm-status-summary-request.v1", "id": ident, "requested_epoch": now, "requested_at": iso_utc(now), "projects": selected, "basis_fingerprints": {}, "state": request_state, "results": initial_results, "result_reasons": initial_reasons, "routes": routes, "correlations": {}, "expires_epoch": now + 1800, "supervisor_availability": supervisor_availability(home)}
        atomic_json(reqdir / f"{ident}.json", record)
        for name in selected:
            invalidate_projection_cache(home, name)
        note = [str(ROOT / "bin" / "fm-inbox.sh"), "note", f"Request Manual Update id={ident} projects={','.join(selected)}"]
        env = os.environ.copy()
        env["FM_HOME"] = str(home)
        env.pop("FM_STATE_OVERRIDE", None)
        env.pop("FM_DATA_OVERRIDE", None)
        sent = subprocess.run(note, env=env, cwd=ROOT, capture_output=True, text=True, timeout=10)
        if sent.returncode:
            record["error"] = sent.stderr[-500:] or "inbox notification failed"
            record["results"] = {name: "failed" for name in selected}
            record["result_reasons"] = {name: record["error"] for name in selected}
            record["state"] = "failed"
            atomic_json(reqdir / f"{ident}.json", record)
            for name in selected:
                invalidate_projection_cache(home, name)
            print(json.dumps({"status": "failed", "request": ident, "error": record["error"]}))
            return 1
        print(json.dumps({"status": record["state"], "request": ident, "projects": selected, "attached": attached, "supervisor_availability": availability, "results": record["results"]}))
        return 0
    if args.command == "route":
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", args.target) or not re.fullmatch(r"[a-f0-9]{16}", args.correlation):
            print("error: invalid route target or correlation", file=sys.stderr)
            return 2
        path = reqdir / f"{args.request_id}.json"
        record = read_json(path)
        if not isinstance(record, dict) or record.get("schema") != "fm-status-summary-request.v1" or args.project not in record.get("projects", []):
            print("error: unknown request or project", file=sys.stderr)
            return 2
        if int(record.get("expires_epoch", 0)) <= utc_now() or record.get("results", {}).get(args.project) != "pending":
            print(json.dumps({"status": record.get("results", {}).get(args.project, "expired"), "project": args.project}))
            return 1
        verified_correlation = summary_pending_correlation(home, args.target, args.request_id, args.project)
        if verified_correlation != args.correlation:
            print("error: correlation does not match the durable pending-reply record", file=sys.stderr)
            return 2
        prior = record.setdefault("correlations", {}).get(args.project)
        if prior and prior != args.correlation:
            print("error: project already has a different pending correlation", file=sys.stderr)
            return 2
        record.setdefault("routes", {})[args.project] = {"route": "secondmate", "target": args.target, "state": "pending"}
        record["correlations"][args.project] = args.correlation
        atomic_json(path, record)
        invalidate_projection_cache(home, args.project)
        print(json.dumps({"status": "pending", "project": args.project, "correlation": args.correlation}))
        return 0
    if args.command == "dispatch":
        path = reqdir / f"{args.request_id}.json"
        record = read_json(path)
        if not isinstance(record, dict) or record.get("schema") != "fm-status-summary-request.v1":
            print("error: unknown summary request", file=sys.stderr)
            return 2
        if int(record.get("expires_epoch", 0)) <= utc_now():
            record["results"] = {name: ("expired" if state == "pending" else state)
                                 for name, state in record.get("results", {}).items()}
            record["state"] = "expired"
            atomic_json(path, record)
            print(json.dumps({"status": "expired", "request": args.request_id}))
            return 1
        selected = [name for name in record.get("projects", [])
                    if record.get("results", {}).get(name) == "pending"]
        routes = summary_route_candidates(home, selected)
        for name in selected:
            route = routes.get(name, {})
            if route.get("route") == "main-home":
                record.setdefault("routes", {})[name] = {"route": "main-home", "target": None, "state": "pending"}
                continue
            if route.get("route") != "secondmate" or not route.get("target"):
                reason = route.get("reason", "registered project route is unavailable")
                record.setdefault("routes", {})[name] = {"route": "unavailable", "target": None, "state": "unavailable", "reason": reason}
                record.setdefault("results", {})[name] = "unavailable"
                record.setdefault("result_reasons", {})[name] = reason
                continue
            target = route["target"]
            marker = f"request={args.request_id} project={name}"
            existing = summary_pending_correlation(home, target, args.request_id, name)
            message = (f"{marker} Request Manual Update. Read the current structured issue projection when composition begins, "
                       "then provide a concise written project summary through the correlated parent status channel.")
            env = os.environ.copy()
            env["FM_HOME"] = str(home)
            env.pop("FM_STATE_OVERRIDE", None)
            env.pop("FM_DATA_OVERRIDE", None)
            if existing:
                env["FM_PENDING_REPLY_EXISTING_CORR"] = existing
            try:
                sent = subprocess.run([str(ROOT / "bin" / "fm-send.sh"), target, message],
                                      env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)
                correlation = summary_pending_correlation(home, target, args.request_id, name)
                if correlation:
                    prior = record.setdefault("correlations", {}).get(name)
                    if prior and prior != correlation:
                        raise ValueError("request already owns a different pending-reply correlation")
                    record.setdefault("correlations", {})[name] = correlation
                    record.setdefault("routes", {})[name] = {"route": "secondmate", "target": target, "state": "pending"}
                    if sent.returncode != 0:
                        record.setdefault("result_reasons", {})[name] = "send outcome is uncertain; durable correlation retained for recovery"
                else:
                    record.setdefault("routes", {})[name] = {"route": "secondmate", "target": target, "state": "pending"}
                    record.setdefault("result_reasons", {})[name] = (sent.stderr[-500:] or "send returned without a durable correlated reply")
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                record.setdefault("routes", {})[name] = {"route": "secondmate", "target": target, "state": "pending"}
                record.setdefault("result_reasons", {})[name] = str(exc)[:500]
        values = record.get("results", {}).values()
        record["state"] = ("pending" if "pending" in values else "failed" if "failed" in values
                            else "unavailable" if "unavailable" in values else "outdated" if "outdated" in values
                            else "written")
        atomic_json(path, record)
        for name in selected:
            invalidate_projection_cache(home, name)
        print(json.dumps({"status": record["state"], "request": args.request_id,
                          "routes": {name: record.get("routes", {}).get(name) for name in selected},
                          "results": {name: record.get("results", {}).get(name) for name in selected}}))
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
        if (not re.fullmatch(r"[a-f0-9]{64}", args.basis_fingerprint)
                or not re.fullmatch(r"[a-f0-9]{64}", args.basis_transition_watermark)
                or args.basis_observed_at < 0 or not args.author.strip() or len(args.author) > 160):
            print("error: invalid summary basis or author", file=sys.stderr)
            return 2
        text = Path(args.text_file).read_text(encoding="utf-8")
        if not text.strip() or len(text.encode()) > 16_384:
            print("error: summary must be nonempty and at most 16384 bytes", file=sys.stderr)
            return 2
        current = make_projection(home, args.project)
        with project_summary_lock(home, args.project):
            outdated = (current["fingerprint"] != args.basis_fingerprint
                        or current["transition_watermark"] != args.basis_transition_watermark)
            written_epoch = utc_now()
            summary = {"schema": "fm-status-summary.v1", "request": args.request_id, "project": args.project, "author": args.author, "basis_fingerprint": args.basis_fingerprint, "basis_transition_watermark": args.basis_transition_watermark, "basis_observed_epoch": args.basis_observed_at, "written_epoch": written_epoch, "text": text, "state": "outdated" if outdated else "written", "current_fingerprint": current["fingerprint"], "current_transition_watermark": current["transition_watermark"], "invalidated_epoch": written_epoch if outdated else None, "invalidated_change": current.get("project_changed") if outdated else None, "evidence": {"basis_fingerprint": args.basis_fingerprint, "basis_transition_watermark": args.basis_transition_watermark, "basis_observed_epoch": args.basis_observed_at, "comparison_fingerprint": current["fingerprint"], "comparison_transition_watermark": current["transition_watermark"], "comparison_observed_epoch": current["generated_epoch"], "repository": current["repository"], "catalog_checked_epoch": current["last_checked_epoch"], "snapshot_collected_epoch": current["snapshot"]["collected_epoch"]}}
            summary_path = sumdir / f"{hashlib.sha256(args.project.encode()).hexdigest()}.json"
            prior = read_json(summary_path, 1_000_000)
            history = prior.get("summaries", []) if isinstance(prior, dict) and prior.get("schema") == "fm-status-summaries.v1" and isinstance(prior.get("summaries"), list) else []
            history.append(summary)
            atomic_json(summary_path, {"schema": "fm-status-summaries.v1", "project": args.project, "summaries": history[-20:]})
            record.setdefault("results", {})[args.project] = summary["state"]
            values = list(record["results"].values())
            record["state"] = ("pending" if "pending" in values else
                                "failed" if "failed" in values else
                                "unavailable" if "unavailable" in values else
                                "outdated" if "outdated" in values else "written")
            atomic_json(path, record)
        invalidate_projection_cache(home, args.project)
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
        invalidate_projection_cache(home, args.project)
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
async function load(force=false){const url='/api/status?project='+encodeURIComponent(selected)+(force?'&refresh=1':'');try{const response=await fetch(url),data=await response.json();if(!response.ok)throw Error(data.error||'collection unavailable');projects=data.projects||[];el('project').replaceChildren();for(const name of projects){const option=document.createElement('option');option.value=name;option.textContent=name;option.selected=name===selected;el('project').append(option)}const p=data.projection;if(!p)throw Error(data.error||'status unavailable');const cat=p.catalog,observed=cat.observed_known??0,coverage=cat.complete&&!cat.stale?`complete (${observed} observed of ${cat.total} total)`:`partial or unavailable (${cat.known} cached; ${observed} observed; total unknown)`,remote=p.remote_issue_coverage||{},omitted=remote.omitted_scope_unknown?'omitted count unknown':`${remote.omitted_task_records||0} omitted`,remoteCoverage=`Remote summaries ${remote.shown_home_summaries||0}/${remote.registered_homes||0}; ${remote.visible_task_records||0} selected-project issue task records visible, ${omitted}, ${remote.unknown_home_summaries||0} unknown, ${remote.stale_home_summaries||0} stale${remote.complete?'':' (partial)'}`;el('status').className='muted';el('status').textContent=`${p.repository||'Repository unavailable'}; catalog ${coverage}; last checked ${time(p.last_checked_epoch)}. ${remoteCoverage}. Supervisor ${p.supervisor.session_lock_present?'session lock held':'no session lock'}; watcher beat ${time(p.supervisor.watcher_beat_epoch)}. ${cat.error||p.snapshot.error||''}`;el('rows').replaceChildren(...p.rows.map(renderRow));el('unlinked').replaceChildren(...p.unlinked_tasks.map(task=>{const li=document.createElement('li');li.textContent=`${task.id}: ${task.stage}; ${task.next_step||'Next step not recorded'}`;li.append(verification({tasks:[task]}));return li}));const fingerprintKey='fm-issues-fingerprint:'+selected,prior=localStorage.getItem(fingerprintKey);el('changed').textContent=prior&&prior!==p.fingerprint?'Project status changed since you last looked.':'';localStorage.setItem(fingerprintKey,p.fingerprint);renderSummary(p.summary);renderRequests(p.summary_requests)}catch(error){el('status').className='error';el('status').textContent='Unavailable: '+error.message}}
function renderSummary(summary){const root=el('summary');root.replaceChildren();if(!summary)return;const evidence=summary.evidence||{},basis=`Basis ${summary.basis_fingerprint||'unknown'} observed ${time(summary.basis_observed_epoch)}; repository ${evidence.repository||'unknown'}; snapshot ${time(evidence.snapshot_collected_epoch)}; catalog ${time(evidence.catalog_checked_epoch)}`;if(summary.state==='written'){root.textContent=`AI-written summary by ${summary.author}, based on status as of ${time(summary.basis_observed_epoch)}, written ${time(summary.written_epoch)}:\n${basis}\n${summary.text}`;return}if(summary.state==='outdated'){const details=document.createElement('details'),title=document.createElement('summary'),body=document.createElement('p'),basisLine=document.createElement('small');title.textContent=`AI-written summary outdated since ${time(summary.invalidated_epoch)}; historical text retained`;basisLine.textContent=basis;body.textContent=summary.text;details.append(title,basisLine,body);root.append(details)}}
function renderRequests(requests){if(!requests.length)return;const states=requests.map(item=>{const projects=(item.project_results||[]).map(result=>`${result.project} ${result.state}${result.route?` via ${result.route}`:''}${result.correlation?` (${result.correlation})`:''}${result.reason?`: ${result.reason}`:''}`).join(', ');return `${item.id}: ${item.display_state||item.state}; supervisor ${item.supervisor_availability||'unknown'}${projects?`; ${projects}`:''}${item.error?` (${item.error})`:''}`}).join('; ');el('changed').textContent+=(el('changed').textContent?' ':'')+`Manual update requests: ${states}.`}
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
    parser.add_argument("--lane-evidence-task")
    parser.add_argument("--lane-evidence-project")
    parser.add_argument("--lane-evidence-generation")
    parser.add_argument("--lane-evidence-head")
    parser.add_argument("--lane-evidence-dirty", choices=("true", "false", "unknown"), default="unknown")
    parser.add_argument("summary", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    if args.lane_evidence_task:
        if (not args.lane_evidence_project or not args.lane_evidence_generation
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.lane_evidence_task)):
            parser.error("lane evidence requires a task, registered project name, and generation")
        value = lane_status(home, {"id": args.lane_evidence_task,
                                   "spawn_gen": args.lane_evidence_generation,
                                   "source_dirty": (True if args.lane_evidence_dirty == "true" else False if args.lane_evidence_dirty == "false" else None)},
                            args.lane_evidence_project, current_head=args.lane_evidence_head)
        print(json.dumps(value, sort_keys=True))
        return 0
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
