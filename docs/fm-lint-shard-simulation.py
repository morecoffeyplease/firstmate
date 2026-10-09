#!/usr/bin/env python3
"""Reproduce the four-shard byte-LPT estimate from the pinned CI profile."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

ANALYZED_MERGE_SHA = "e8ab41d0a632bad4ce92967ccce9f26059d5ca28"
EXPECTED_ROOTS = 415
EXPECTED_BYTES = 13_375_129


@dataclass(frozen=True)
class Root:
    path: str
    blob_bytes: int
    wall_seconds: float
    max_rss_kib: int
    canonical_index: int


def canonical_key(path: str) -> tuple[int, str]:
    if path.startswith("bin/") and "/" not in path[4:]:
        return (0, path)
    if path.startswith("bin/backends/") and "/" not in path[len("bin/backends/") :]:
        return (1, path)
    if path.startswith("tests/") and "/" not in path[6:]:
        return (2, path)
    raise ValueError(f"noncanonical lint root: {path}")


def read_profile(path: Path) -> list[Root]:
    metadata: dict[str, str] = {}
    rows: list[tuple[str, int, float, int]] = []
    with path.open(newline="") as stream:
        for line in stream:
            if line.startswith("# "):
                key, value = line[2:].strip().split("=", 1)
                metadata[key] = value
                continue
            if line.startswith("path\t"):
                continue
            fields = next(csv.reader([line], delimiter="\t"))
            if len(fields) != 4:
                raise ValueError(f"expected four columns, got {len(fields)}")
            rows.append((fields[0], int(fields[1]), float(fields[2]), int(fields[3])))

    if metadata.get("analyzed_merge_sha") != ANALYZED_MERGE_SHA:
        raise ValueError("profile analyzed_merge_sha does not match this simulation")
    if len(rows) != EXPECTED_ROOTS:
        raise ValueError(f"expected {EXPECTED_ROOTS} measured roots, got {len(rows)}")
    if len({row[0] for row in rows}) != len(rows):
        raise ValueError("profile contains duplicate roots")
    if sum(row[1] for row in rows) != EXPECTED_BYTES:
        raise ValueError("profile byte total does not match the analyzed Git tree")

    ordered = sorted(rows, key=lambda row: canonical_key(row[0]))
    return [Root(*row, index) for index, row in enumerate(ordered)]


def simulate(roots: list[Root], shard_count: int = 4) -> list[list[Root]]:
    assignments: list[list[Root]] = [[] for _ in range(shard_count)]
    byte_loads = [0] * shard_count
    for root in sorted(roots, key=lambda item: (-item.blob_bytes, item.canonical_index)):
        shard = min(range(shard_count), key=lambda index: (byte_loads[index], index))
        assignments[shard].append(root)
        byte_loads[shard] += root.blob_bytes
    return assignments


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("fm-lint-shard-measurements.tsv"),
    )
    args = parser.parse_args()
    roots = read_profile(args.profile)
    assignments = simulate(roots)
    print(f"analyzed_merge_sha\t{ANALYZED_MERGE_SHA}")
    print(f"root_count\t{len(roots)}")
    print(f"direct_bytes\t{sum(root.blob_bytes for root in roots)}")
    aggregate_runner_minutes = 0.0
    for index, shard in enumerate(assignments, start=1):
        wall = sum(root.wall_seconds for root in shard)
        rss = max(root.max_rss_kib for root in shard)
        aggregate_runner_minutes += wall / 60
        print(
            f"shard_{index}\troots={len(shard)}\tbytes={sum(root.blob_bytes for root in shard)}"
            f"\tmeasured_wall_seconds={wall:.2f}\tmeasured_wall_minutes={wall / 60:.2f}"
            f"\tmax_single_root_rss_kib={rss}"
        )
    print(f"aggregate_runner_minutes\t{aggregate_runner_minutes:.2f}")


if __name__ == "__main__":
    main()
