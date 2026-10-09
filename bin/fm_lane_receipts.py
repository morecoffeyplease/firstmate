"""Shared structural validation for local validation-lane receipts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA = "fm-lane-receipt.v1"
LANES = {"focused", "full", "verify"}


def repository_identity(origin: object) -> str | None:
    if not isinstance(origin, str) or len(origin) > 2048 or any(char in origin for char in "\r\n\x00"):
        return None
    parsed = urlparse(origin)
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
    return path.lower()


def valid_receipt(value: Any, path: Path, lane: str, task: str, generation: str,
                  project: str, expected_repository: str | None) -> bool:
    if not isinstance(value, dict):
        return False
    phase = value.get("phase")
    valid = (
        value.get("schema") == SCHEMA and phase in ("start", "finish")
        and lane in LANES and value.get("lane") == lane
        and isinstance(value.get("id"), str) and path.stem == f"{lane}-{value.get('id')}"
        and value.get("task") == task and value.get("generation") == generation
        and value.get("project") == project and value.get("repository")
        and repository_identity(value.get("repository")) == expected_repository
        and isinstance(value.get("argv"), list) and value["argv"]
        and all(isinstance(arg, str) and "\x00" not in arg for arg in value["argv"])
        and isinstance(value.get("started_epoch"), int) and value["started_epoch"] >= 0
        and isinstance(value.get("started_order_ns"), int) and value["started_order_ns"] >= 0
        and isinstance(value.get("pid"), int) and value["pid"] > 0
        and isinstance(value.get("process_start"), str) and bool(value["process_start"])
        and isinstance(value.get("head_before"), str) and bool(value["head_before"])
        and isinstance(value.get("dirty_before"), bool)
    )
    if phase == "finish":
        valid = valid and isinstance(value.get("ended_epoch"), int) and value["ended_epoch"] >= value.get("started_epoch", 0)
        valid = valid and isinstance(value.get("head_after"), str) and bool(value["head_after"])
        valid = valid and isinstance(value.get("dirty_after"), bool)
        valid = valid and (value.get("exit_code") is None or (isinstance(value.get("exit_code"), int) and 0 <= value["exit_code"] <= 255))
        valid = valid and (value.get("signal") is None or (isinstance(value.get("signal"), int) and 1 <= value["signal"] <= 64))
        valid = valid and (value.get("received_signal") is None or (isinstance(value.get("received_signal"), int) and 1 <= value["received_signal"] <= 64))
    return bool(valid)
