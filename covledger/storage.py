"""Immutable local run storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_DIR = ".covledger"
RUNS_DIR = "runs"


class RunNotFound(ValueError):
    pass


def state_root(root: Path) -> Path:
    return root / STATE_DIR


def runs_root(root: Path) -> Path:
    return state_root(root) / RUNS_DIR


def list_run_ids(root: Path) -> list[str]:
    directory = runs_root(root)
    if not directory.exists():
        return []
    runs = [path.name for path in directory.iterdir() if path.is_dir() and (path / "run.json").is_file()]
    return sorted(runs, reverse=True)


def resolve_run_id(root: Path, selector: str) -> str:
    if selector == "latest":
        runs = list_run_ids(root)
        if not runs:
            raise RunNotFound("no CovLedger runs found")
        return runs[0]
    path = runs_root(root) / selector / "run.json"
    if not path.is_file():
        raise RunNotFound(f"run not found: {selector}")
    return selector


def load_run(root: Path, selector: str) -> dict[str, Any]:
    run_id = resolve_run_id(root, selector)
    path = runs_root(root) / run_id / "run.json"
    return json.loads(path.read_text(encoding="utf-8"))


def publish_run(run_dir: Path, report: dict[str, Any]) -> None:
    target = run_dir / "run.json"
    if target.exists():
        raise FileExistsError(f"refusing to rewrite published run: {target}")
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    target.write_text(payload, encoding="utf-8")
