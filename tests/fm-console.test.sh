#!/usr/bin/env bash
# End-to-end tests for the local operator console tabs and answer delivery.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT

python3 - "$ROOT" "$TMP" <<'PY'
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

repo = pathlib.Path(sys.argv[1])
tmp = pathlib.Path(sys.argv[2])
root = tmp / "fake-root"
home = tmp / "home"
(root / "bin").mkdir(parents=True)
(home / "data").mkdir(parents=True)
(home / "state").mkdir()
(home / "projects" / "alpha").mkdir(parents=True)
(tmp / "fake-bin").mkdir()
(home / "data" / "projects.md").write_text("- alpha - Example project\n")
subprocess.run(["git", "init", "-q", str(home / "projects" / "alpha")], check=True)
subprocess.run(["git", "-C", str(home / "projects" / "alpha"), "config", "remote.origin.url", "git@github.com:example/alpha.git"], check=True)
issue_url = "https://github.com/example/alpha/issues/7"
pr_url = "https://github.com/example/alpha/pull/8"
snapshot = {
    "schema": "fm-fleet-snapshot.v1",
    "tasks": [{"id": "worker", "backlog": {"repo": "alpha", "state": "in_flight", "links": [issue_url], "pr_url": pr_url}, "current_state": {"state": "working"}}],
    "backlog": {"records": [{"id": "next", "title": "Waiting task", "repo": "alpha", "state": "queued", "blocked_by_ids": ["worker"], "unresolved_blocker_ids": ["worker"]}]},
    "secondmate_current": {"records": []},
}
snapshot_path = tmp / "snapshot.json"
snapshot_path.write_text(json.dumps(snapshot))
(root / "bin" / "fm-fleet-snapshot.sh").write_text('#!/bin/bash\ncat "$FM_CONSOLE_FIXTURE"\n')
(root / "bin" / "fm-fleet-snapshot.sh").chmod(0o755)
(root / "bin" / "fm-classify-lib.sh").write_text('scan_open_decisions() { cat "$FM_CONSOLE_DECISIONS_FILE"; }\n')
(root / "bin" / "fm-send.sh").write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$FM_CONSOLE_SEND_CAPTURE"\n')
(root / "bin" / "fm-send.sh").chmod(0o755)
fake_gh = tmp / "fake-bin" / "gh-axi"
fake_gh.write_text(r'''#!/usr/bin/env python3
import json, sys
path = next((arg for arg in sys.argv if arg.startswith("repos/")), "")
if "/issues/7" in path:
    out = {"number": 7, "title": "Exact issue title", "state": "open", "html_url": "https://github.com/example/alpha/issues/7"}
elif "/pulls/8/reviews" in path:
    out = [{"user": {"login": "reviewer"}, "state": "APPROVED"}]
elif "/pulls/8" in path:
    out = {"number": 8, "html_url": "https://github.com/example/alpha/pull/8", "state": "open", "draft": False, "merged": False, "head": {"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
elif "/check-runs" in path:
    out = {"check_runs": [{"status": "completed", "conclusion": "success"}]}
else:
    out = {}
print(json.dumps(out))
''')
fake_gh.chmod(0o755)
capture = tmp / "send.txt"
decisions_path = tmp / "decisions.tsv"
decisions_path.write_text("worker\tdecision-a\tneeds-decision\tShould we ship?\n")
env = {**os.environ, "PATH": f"{tmp / 'fake-bin'}:{os.environ['PATH']}", "FM_CONSOLE_FIXTURE": str(snapshot_path), "FM_CONSOLE_SEND_CAPTURE": str(capture), "FM_CONSOLE_DECISIONS_FILE": str(decisions_path), "BROWSER": "/usr/bin/true"}
proc = subprocess.Popen([sys.executable, str(repo / "bin" / "fm-console.py"), "--root", str(root), "--home", str(home), "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
try:
    line = proc.stdout.readline().strip()
    assert line.startswith("http://127.0.0.1:"), line
    port = int(line.rsplit(":", 1)[1].rstrip("/"))
    base = line.rstrip("/")
    page = urllib.request.urlopen(base + "/", timeout=3).read().decode()
    assert all(f">{tab}<" in page for tab in ("Status", "Open decisions", "Queue"))
    status = json.load(urllib.request.urlopen(base + "/api/status?project=alpha", timeout=3))
    assert status["rows"], status
    assert status["rows"][0]["title"] == "Exact issue title"
    assert status["rows"][0]["issue_state"] == "open"
    assert status["rows"][0]["tasks"][0]["stage"] == "ready"
    assert status["rows"][0]["prs"][0]["checks"] == "passed"
    decisions = json.load(urllib.request.urlopen(base + "/api/decisions", timeout=3))["decisions"]
    assert decisions == [{"task": "worker", "key": "decision-a", "verb": "needs-decision", "note": "Should we ship?"}]
    queue = json.load(urllib.request.urlopen(base + "/api/queue", timeout=3))["items"]
    assert queue[0]["unresolved_blocker_ids"] == ["worker"]
    snapshot_path.write_text("not JSON")
    stale = json.load(urllib.request.urlopen(base + "/api/status?project=alpha&refresh=1", timeout=3))
    assert stale["stale"] is True and stale["rows"][0]["title"] == "Exact issue title"
    body = json.dumps({"task": "worker", "key": "decision-a", "answer": "--resolve-key is answer text"}).encode()
    request = urllib.request.Request(base + "/api/answer", data=body, method="POST", headers={"Content-Type": "application/json", "Origin": base})
    try:
        urllib.request.urlopen(request, timeout=3)
        raise AssertionError("answer without page token was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    match = re.search(r"const token=(\"[^\"]+\")", page)
    assert match, "page token was not found"
    request.add_header("X-FM-Token", json.loads(match.group(1)))
    result = json.load(urllib.request.urlopen(request, timeout=3))
    assert result["ok"] is True, result
    assert capture.read_text().strip() == "worker --resolve-key decision-a -- --resolve-key is answer text"
    decisions_path.write_text("")
    try:
        urllib.request.urlopen(request, timeout=3)
        raise AssertionError("closed decision was answered a second time")
    except urllib.error.HTTPError as exc:
        assert json.load(exc)["ok"] is False
    assert len(capture.read_text().splitlines()) == 1
finally:
    proc.terminate()
    proc.wait(timeout=5)
PY

echo "ok - console tabs render current status, decisions, queue, and deliver keyed answers"
