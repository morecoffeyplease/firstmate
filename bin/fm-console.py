#!/usr/bin/env python3
"""Local, deterministic Firstmate operator console."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_ANSWER = 8192


def run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def registered_projects(home: Path) -> list[str]:
    path = home / "data" / "projects.md"
    names = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) > 1 and parts[0] == "-" and PROJECT_RE.fullmatch(parts[1]):
                names.append(parts[1])
    except OSError:
        pass
    return sorted(set(names))


def repository_identity(home: Path, project: str) -> str | None:
    clone = home / "projects" / project
    if not clone.is_dir() or clone.is_symlink():
        return None
    try:
        result = subprocess.run(["git", "-C", str(clone), "config", "--get", "remote.origin.url"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    origin = result.stdout.strip()
    match = re.fullmatch(r"git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", origin)
    if match:
        return match.group(1).lower()
    parsed = urllib.parse.urlsplit(origin)
    path = parsed.path.strip("/").removesuffix(".git")
    if parsed.hostname and parsed.hostname.lower() == "github.com" and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        return path.lower()
    return None


def url_repository(url: str) -> str:
    return "/".join(url.removeprefix("https://github.com/").split("/")[:2]).lower()


def snapshot(home: Path, root: Path) -> dict:
    env = {**os.environ, "FM_HOME": str(home)}
    env.pop("FM_ROOT_OVERRIDE", None)
    result = run([str(root / "bin" / "fm-fleet-snapshot.sh"), "--json"], cwd=root, env=env, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr[-1000:] or "fleet snapshot failed")
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get("schema") != "fm-fleet-snapshot.v1":
        raise RuntimeError("unsupported fleet snapshot schema")
    return value


def issue_urls(record: dict) -> list[str]:
    backlog = record.get("backlog") or {}
    values = backlog.get("links") or record.get("links") or []
    urls = []
    for value in values:
        if isinstance(value, str) and re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*", value):
            urls.append(value)
    return sorted(set(urls))


def pr_urls(record: dict) -> list[str]:
    backlog = record.get("backlog") or {}
    values = [backlog.get("pr_url"), (record.get("pr") or {}).get("url")]
    urls = []
    for value in values:
        if isinstance(value, str) and re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", value):
            urls.append(value)
    return sorted(set(urls))


def task_records(value: dict) -> list[dict]:
    records = []
    for task in value.get("tasks", []):
        if isinstance(task, dict):
            records.append(task)
    for row in (value.get("backlog") or {}).get("records", []):
        if isinstance(row, dict) and row.get("state") in ("queued", "in_flight", "done"):
            records.append(row)
    for home_record in (value.get("secondmate_current") or {}).get("records", []):
        if not isinstance(home_record, dict):
            continue
        for task in home_record.get("issue_tasks", []):
            if isinstance(task, dict):
                item = dict(task)
                item["id"] = f"{home_record.get('id', 'secondmate')}/{task.get('id', '')}"
                item["owner_home_id"] = home_record.get("id")
                records.append(item)
        for row in home_record.get("queued", []):
            if isinstance(row, dict):
                item = dict(row)
                item["id"] = f"{home_record.get('id', 'secondmate')}/{row.get('id', '')}"
                records.append(item)
    unique: dict[str, dict] = {}
    for record in records:
        task_id = record.get("id")
        if isinstance(task_id, str) and task_id:
            prior = unique.get(task_id)
            if prior is None:
                unique[task_id] = dict(record)
                continue
            prior_backlog = prior.get("backlog") if isinstance(prior.get("backlog"), dict) else {}
            incoming_backlog = record.get("backlog") if isinstance(record.get("backlog"), dict) else record
            prior["backlog"] = {**incoming_backlog, **prior_backlog}
            for field in ("current_state", "pr", "project", "kind", "hints", "generation", "spawn_gen"):
                if field not in prior and field in record:
                    prior[field] = record[field]
    return list(unique.values())


def github_json(root: Path, path: str, home: Path) -> object:
    env = {**os.environ, "FM_HOME": str(home)}
    result = run(["gh-axi", "api", path, "--full"], cwd=root, env=env, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr[-500:] or "GitHub read failed")
    data = json.loads(result.stdout)
    return data


def fetch_issue(root: Path, home: Path, url: str) -> dict:
    parts = url.removeprefix("https://github.com/").split("/")
    owner, repo, _, number = parts
    issue = github_json(root, f"repos/{owner}/{repo}/issues/{number}", home)
    if not isinstance(issue, dict):
        raise RuntimeError("GitHub returned an unexpected issue")
    if issue.get("number") != int(number) or str(issue.get("html_url", "")).lower() != url.lower():
        raise RuntimeError("GitHub returned a different issue identity")
    return issue


def fetch_pr(root: Path, home: Path, url: str) -> dict:
    parts = url.removeprefix("https://github.com/").split("/")
    owner, repo, _, number = parts
    pr = github_json(root, f"repos/{owner}/{repo}/pulls/{number}", home)
    if not isinstance(pr, dict):
        raise RuntimeError("GitHub returned an unexpected pull request")
    if pr.get("number") != int(number) or str(pr.get("html_url", "")).lower() != url.lower():
        raise RuntimeError("GitHub returned a different pull request identity")
    head = (pr.get("head") or {}).get("sha")
    pr["review_decision"] = "unknown"
    pr["checks"] = "unknown"
    if isinstance(head, str) and re.fullmatch(r"[A-Fa-f0-9]{40,64}", head):
        try:
            reviews = github_json(root, f"repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100", home)
            if isinstance(reviews, list):
                latest = {}
                for review in reviews:
                    if isinstance(review, dict) and isinstance(review.get("user"), dict):
                        latest[review["user"].get("login")] = review.get("state")
                if any(state == "CHANGES_REQUESTED" for state in latest.values()):
                    pr["review_decision"] = "changes requested"
                elif any(state == "APPROVED" for state in latest.values()):
                    pr["review_decision"] = "approved"
                else:
                    pr["review_decision"] = "review pending"
            checks = github_json(root, f"repos/{owner}/{repo}/commits/{head}/check-runs?per_page=100", home)
            runs = checks.get("check_runs") if isinstance(checks, dict) else None
            if isinstance(runs, list) and runs:
                conclusions = [item.get("conclusion") for item in runs if isinstance(item, dict)]
                if any(item in ("failure", "timed_out", "action_required", "cancelled") for item in conclusions):
                    pr["checks"] = "failed"
                elif all(item in ("success", "neutral", "skipped") for item in conclusions) and len(conclusions) == len(runs):
                    pr["checks"] = "passed"
                else:
                    pr["checks"] = "running"
            elif isinstance(runs, list):
                pr["checks"] = "none reported"
        except (RuntimeError, ValueError, subprocess.SubprocessError):
            pass
    return pr


def stage(record: dict, prs: list[dict]) -> str:
    open_prs = [pr for pr in prs if not (pr.get("merged") is True or pr.get("merged_at"))]
    if prs and not open_prs:
        return "merged"
    backlog = record.get("backlog") or record
    state = backlog.get("state")
    if state == "queued":
        return "queued"
    if not open_prs:
        return "in lane"
    if any(pr.get("draft") is True for pr in open_prs):
        return "draft PR"
    if all(pr.get("review_decision") == "approved" and pr.get("checks") == "passed" for pr in open_prs):
        return "ready"
    return "in review"


def status_data(home: Path, root: Path, stale: dict | None = None) -> dict:
    now = int(time.time())
    try:
        value = snapshot(home, root)
        records = task_records(value)
        projects = registered_projects(home)
        repositories = {project: repository_identity(home, project) for project in projects}
        rows: dict[str, dict] = {}
        failures = []
        for project, repo in repositories.items():
            if repo is None:
                failures.append(f"{project}: registered GitHub project origin is unavailable or invalid")
        issue_cache: dict[str, dict] = {}
        pr_cache: dict[str, dict] = {}
        for record in records:
            project = (record.get("backlog") or record).get("repo") or record.get("project")
            if project not in projects:
                continue
            for url in issue_urls(record):
                if repositories.get(project) is None or url_repository(url) != repositories[project]:
                    if url_repository(url) != repositories.get(project):
                        failures.append(f"{url}: issue repository does not match registered project {project}")
                    continue
                try:
                    if url not in issue_cache:
                        issue_cache[url] = fetch_issue(root, home, url)
                    issue = issue_cache[url]
                except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                    failures.append(f"{url}: {exc}")
                    issue = {"number": int(url.rsplit("/", 1)[1]), "title": "", "html_url": url, "state": "unknown"}
                prs = []
                for pr_url in pr_urls(record):
                    if url_repository(pr_url) != repositories.get(project):
                        failures.append(f"{pr_url}: pull request repository does not match registered project {project}")
                        continue
                    try:
                        if pr_url not in pr_cache:
                            pr_cache[pr_url] = fetch_pr(root, home, pr_url)
                        prs.append(pr_cache[pr_url])
                    except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                        failures.append(f"{pr_url}: {exc}")
                        prs.append({"html_url": pr_url, "state": "unknown", "draft": None})
                key = (project, url)
                row = rows.setdefault(key, {"project": project, "number": issue.get("number"), "title": issue.get("title"), "url": url, "issue_state": issue.get("state", "unknown"), "tasks": [], "prs": []})
                row["tasks"].append({"id": record.get("id"), "state": (record.get("current_state") or {}).get("state") or record.get("state") or "unknown", "stage": stage(record, prs)})
                for pr in prs:
                    summary = {"url": pr.get("html_url"), "state": pr.get("state", "unknown"), "draft": pr.get("draft"), "merged": pr.get("merged") is True or bool(pr.get("merged_at")), "review": pr.get("review_decision") or "unknown", "checks": pr.get("checks") or "unknown"}
                    if summary not in row["prs"]:
                        row["prs"].append(summary)
        result = {"generated_epoch": now, "projects": projects, "rows": list(rows.values()), "failures": failures, "stale": False, "unavailable": False}
        result["rows"].sort(key=lambda row: (row["project"], row["number"] or 0))
        return result
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        if stale:
            return {**stale, "stale": True, "error": str(exc), "attempted_epoch": now}
        return {"generated_epoch": now, "projects": registered_projects(home), "rows": [], "failures": [], "stale": False, "unavailable": True, "error": str(exc)}


def decisions(home: Path, root: Path) -> list[dict]:
    env = {**os.environ, "FM_HOME": str(home)}
    script = '. "$1/fm-classify-lib.sh"; scan_open_decisions "${FM_STATE_OVERRIDE:-${FM_HOME}/state}"'
    result = run(["/bin/bash", "-c", script, "fm-console-decisions", str(root / "bin")], cwd=root, env=env)
    if result.returncode:
        raise RuntimeError(result.stderr[-500:] or "open decision scan failed")
    out = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        task, key, verb, note = parts
        if ID_RE.fullmatch(task) and key and verb in ("needs-decision", "blocked"):
            out.append({"task": task, "key": key, "verb": verb, "note": note})
    return out


def queue_data(home: Path, root: Path) -> list[dict]:
    try:
        snap = snapshot(home, root)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"queue unavailable: {exc}") from exc
    out = []
    for row in (snap.get("backlog") or {}).get("records", []):
        if isinstance(row, dict) and row.get("state") in ("queued", "in_flight"):
            out.append({key: row.get(key) for key in ("id", "title", "state", "repo", "blocked_by_ids", "unresolved_blocker_ids", "blocked_reason", "captain_actionable")})
    out.sort(key=lambda row: (row.get("repo") or "", row.get("id") or ""))
    return out


def page(token: str, port: int) -> bytes:
    token_json = json.dumps(token)
    html_page = r"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Firstmate Console</title>
<style>
:root{font:16px system-ui,sans-serif;color-scheme:light dark}body{max-width:1200px;margin:2rem auto;padding:0 1rem}header{display:flex;align-items:center;gap:1rem;flex-wrap:wrap}h1{margin-right:auto}nav{display:flex;gap:.5rem;border-bottom:1px solid #888;padding:.5rem 0}button{font:inherit;padding:.55rem .8rem;border:1px solid #888;border-radius:.35rem;background:Canvas;color:CanvasText;cursor:pointer}button[aria-selected=true]{border-bottom:3px solid #3978db}.muted{color:GrayText}.error{color:#b42318}.tab{padding-top:1rem}table{border-collapse:collapse;width:100%;margin-top:1rem}th,td{text-align:left;vertical-align:top;padding:.65rem;border-bottom:1px solid #8885}a{color:LinkText}textarea{display:block;width:min(48rem,100%);min-height:5rem;margin:.5rem 0;font:inherit}#status{white-space:pre-wrap}label{display:block;margin:.4rem 0}
</style><header><h1>Firstmate Console</h1><button id="refresh">Refresh</button></header><p id="status" class="muted" role="status">Loading…</p><nav role="tablist" aria-label="Console sections"><button role="tab" aria-selected="true" aria-controls="status-tab" id="status-button">Status</button><button role="tab" aria-selected="false" aria-controls="decisions-tab" id="decisions-button">Open decisions</button><button role="tab" aria-selected="false" aria-controls="queue-tab" id="queue-button">Queue</button></nav>
<section class="tab" role="tabpanel" id="status-tab" aria-labelledby="status-button"><label>Project <select id="project"></select></label><div id="status-content"></div></section>
<section class="tab" role="tabpanel" id="decisions-tab" aria-labelledby="decisions-button" hidden><div id="decisions-content"></div></section>
<section class="tab" role="tabpanel" id="queue-tab" aria-labelledby="queue-button" hidden><div id="queue-content"></div></section>
<script>
const token=__TOKEN__,tabs=[...document.querySelectorAll('[role=tab]')],status=document.getElementById('status');
const el=id=>document.getElementById(id),node=value=>document.createTextNode(value==null?'':String(value));
function activate(tab){for(const item of tabs){const on=item===tab;item.setAttribute('aria-selected',on);el(item.getAttribute('aria-controls')).hidden=!on}}
tabs.forEach((tab,index)=>{tab.onclick=()=>activate(tab);tab.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[next].focus();activate(tabs[next])}});
function link(url,label){const a=document.createElement('a');a.href=url;a.target='_blank';a.textContent=label;a.rel='noopener noreferrer';return a}
function prCell(prs){const root=document.createElement('div');for(const pr of prs||[]){const state=pr.merged?'merged':pr.draft?'draft PR':pr.state;root.append(link(pr.url,`PR · ${state}`),node(` · review ${pr.review}; checks ${pr.checks}`),document.createElement('br'))}if(!prs?.length)root.textContent='No linked PR';return root}
function table(parent,headers,rows){const t=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');headers.forEach(h=>{const th=document.createElement('th');th.textContent=h;hr.append(th)});head.append(hr);const body=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(value=>{const td=document.createElement('td');if(value instanceof Node)td.append(value);else td.append(node(value));tr.append(td)});body.append(tr)});t.append(head,body);parent.replaceChildren(t)}
async function load(force=false){try{const project=el('project').value,params=new URLSearchParams();if(project)params.set('project',project);if(force)params.set('refresh','1');const query=params.toString()?'?'+params.toString():'';const [s,d,q]=await Promise.all([fetch('/api/status'+query).then(x=>x.json()),fetch('/api/decisions').then(x=>x.json()),fetch('/api/queue').then(x=>x.json())]);if(s.error&&s.unavailable)throw Error(s.error);const select=el('project'),prior=select.value;select.replaceChildren(...(s.projects||[]).map(name=>{const o=document.createElement('option');o.value=name;o.textContent=name;return o}));if((s.projects||[]).includes(prior))select.value=prior;const rows=(s.rows||[]).filter(row=>!select.value||row.project===select.value).map(row=>[row.project,link(row.url,`#${row.number} ${row.title||'(title unavailable)'}`),row.issue_state,row.tasks.map(task=>`${task.id}: ${task.stage} (${task.state})`).join('\n'),prCell(row.prs)]);if(rows.length)table(el('status-content'),['Project','Issue','Issue state','Lane stage','PR status'],rows);else el('status-content').textContent=select.value?'No admitted issues are linked to this project yet.':'No registered projects are available.';if(s.failures?.length){status.className='error';status.textContent=`Updated with unavailable source data at ${new Date(s.generated_epoch*1000).toLocaleString()}.\n${s.failures.join('\n')}`}else{status.className=s.stale?'error':'muted';status.textContent=`${s.stale?'Stale cached':'Updated'} ${new Date(s.generated_epoch*1000).toLocaleString()}${s.error?' · '+s.error:''}`}const dr=(d.decisions||[]).map(item=>{const form=document.createElement('form'),label=document.createElement('strong'),area=document.createElement('textarea'),send=document.createElement('button');label.textContent=`${item.task} · ${item.key} · ${item.verb}: ${item.note}`;area.name='answer';area.maxLength=8192;area.required=true;area.setAttribute('aria-label',`Answer ${item.key}`);send.textContent='Answer and send';form.append(label,area,send);form.onsubmit=async event=>{event.preventDefault();send.disabled=true;try{const result=await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json','X-FM-Token':token},body:JSON.stringify({task:item.task,key:item.key,answer:area.value})}).then(x=>x.json());if(!result.ok)throw Error(result.error||'answer was not delivered');await load()}catch(error){status.className='error';status.textContent='Answer failed: '+error.message;send.disabled=false}};return [form]});if(d.error)throw Error(d.error);table(el('decisions-content'),['Open decision'],dr);if(!(d.decisions||[]).length)el('decisions-content').textContent='No open decisions.';if(q.error){el('queue-content').textContent='Queue unavailable: '+q.error}else{const items=q.items||[];if(items.length)table(el('queue-content'),['Task','Project','State','Dependencies / blocker'],items.map(item=>[`${item.id||''} ${item.title||''}`,item.repo||'Unassigned',item.state,item.blocked_reason||item.unresolved_blocker_ids?.join(', ')||item.blocked_by_ids?.join(', ')||(item.state==='queued'?'Waiting for admission; no dependency blocker recorded':'Currently admitted')]));else el('queue-content').textContent='No queued or in-flight work.'}}catch(error){status.className='error';status.textContent='Unavailable: '+error.message}}
el('project').onchange=()=>load();el('refresh').onclick=()=>load(true);load();setInterval(()=>{if(!document.hidden)load()},30000);
</script></html>"""
    return html_page.replace("__TOKEN__", token_json).encode()


def serve(home: Path, root: Path, port: int | None) -> int:
    token = secrets.token_urlsafe(32)
    cached: dict | None = None
    cache_at = 0.0

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "FirstmateConsole/1"

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def send(self, code: int, value: object) -> None:
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self) -> bool:
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self) -> None:
            nonlocal cached, cache_at
            if not self.valid_host():
                self.send(403, {"error": "invalid host"})
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                body = page(token, self.server.server_port)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/status":
                query = urllib.parse.parse_qs(parsed.query)
                project = query.get("project", [""])[0]
                names = registered_projects(home)
                if project and project not in names:
                    self.send(400, {"error": "project is not registered"})
                    return
                if query.get("refresh") == ["1"] or cached is None or time.monotonic() - cache_at >= 20:
                    cached = status_data(home, root, cached)
                    cache_at = time.monotonic()
                value = dict(cached)
                value["projects"] = names
                value["rows"] = [row for row in value.get("rows", []) if row["project"] in names and (not project or row["project"] == project)]
                self.send(200, value)
                return
            if parsed.path == "/api/decisions":
                try:
                    self.send(200, {"decisions": decisions(home, root)})
                except (RuntimeError, OSError) as exc:
                    self.send(503, {"error": str(exc)})
                return
            if parsed.path == "/api/queue":
                try:
                    self.send(200, {"items": queue_data(home, root)})
                except RuntimeError as exc:
                    self.send(503, {"error": str(exc)})
                return
            self.send(404, {"error": "not found"})

        def do_POST(self) -> None:
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if (self.path != "/api/answer" or not self.valid_host() or self.headers.get("Origin") != expected_origin
                    or not secrets.compare_digest(self.headers.get("X-FM-Token", ""), token)):
                self.send(403, {"error": "request refused"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_ANSWER + 1024:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {"task", "key", "answer"}:
                    raise ValueError("invalid answer request")
                task, key, answer = body["task"], body["key"], body["answer"]
                if not isinstance(task, str) or not ID_RE.fullmatch(task) or not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", key):
                    raise ValueError("invalid decision identity")
                if not isinstance(answer, str) or not answer.strip() or len(answer.encode("utf-8")) > MAX_ANSWER or "\x00" in answer:
                    raise ValueError("answer must contain 1 to 8192 bytes")
                open_keys = {(item["task"], item["key"]) for item in decisions(home, root)}
                if (task, key) not in open_keys:
                    raise ValueError("decision is no longer open")
                result = run([str(root / "bin" / "fm-send.sh"), task, "--resolve-key", key, "--", answer], cwd=root, env={**os.environ, "FM_HOME": str(home)}, timeout=45)
                if result.returncode:
                    raise RuntimeError(result.stderr[-800:] or "fm-send could not deliver the answer")
                self.send(200, {"ok": True, "result": result.stdout[-500:]})
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                self.send(400, {"ok": False, "error": str(exc)})

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port or 0), Handler)
    except OSError as exc:
        print(f"fm-console: cannot bind loopback service: {exc}", file=sys.stderr)
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
    parser = argparse.ArgumentParser(description="local deterministic Firstmate operator console")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--home", default=os.environ.get("FM_HOME"))
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    home = Path(args.home).expanduser().resolve() if args.home else root
    return serve(home, root, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
