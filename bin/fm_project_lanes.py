"""Single typed reader for config/project-lanes.json."""

from __future__ import annotations

import json
from pathlib import Path


def read_project_lanes(home: Path) -> dict[str, dict[str, list[str]]]:
    path = home / "config" / "project-lanes.json"
    if not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise ValueError("config/project-lanes.json must be a regular file no larger than 1 MB")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("invalid config/project-lanes.json") from exc
    if not isinstance(raw, dict):
        raise ValueError("config/project-lanes.json must be an object keyed by registered project")
    result: dict[str, dict[str, list[str]]] = {}
    for project, lanes in raw.items():
        if not isinstance(project, str) or not project or any(char.isspace() for char in project):
            raise ValueError("project lane keys must be nonempty project names")
        if not isinstance(lanes, dict) or set(lanes) - {"full", "verify"}:
            raise ValueError(f"project {project} may configure only full and verify lanes")
        result[project] = {}
        for lane, command in lanes.items():
            if not isinstance(command, list) or not command or any(not isinstance(arg, str) or "\x00" in arg for arg in command):
                raise ValueError(f"{project}.{lane} must be a nonempty argument array")
            result[project][lane] = command
    return result
