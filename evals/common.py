"""Shared eval-harness plumbing: dataset loading, the eval acting user, and the floors.

Floors live in evals/thresholds.toml (single source of truth, ADR-026) — the harness reads
them here and CI runs the same harness, so there is no second copy of the numbers anywhere.
"""
# Implemented in M4 (extracted from run_evals.py so the e2e/dedup suite modules can share it
# without importing the CLI module).

from __future__ import annotations

import json
import tomllib
from pathlib import Path

DATASET_DIR = Path(__file__).parent / "datasets"

# Suites that exercise ACTION agents (routing, e2e, dedup) act as this (seeded) user.
EVAL_USER = "demo.user@corp.com"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_floors() -> dict:
    with (Path(__file__).parent / "thresholds.toml").open("rb") as f:
        return tomllib.load(f)


FLOORS = load_floors()
