"""Normalize Coverage.py JSON into CovLedger's small public evidence contract."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .model import CoverageFile


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _safe_project_path(root: Path, raw_path: str) -> Path | None:
    candidate = (root / raw_path).resolve()
    root = root.resolve()
    if candidate == root or root in candidate.parents:
        return candidate
    return None


def normalize_coverage(raw_json: Path, *, project_root: Path, run_dir: Path) -> dict[str, Any]:
    raw = json.loads(raw_json.read_text(encoding="utf-8"))
    files: dict[str, dict[str, Any]] = {}
    source_root = run_dir / "sources"

    for raw_path, item in sorted(raw.get("files", {}).items()):
        summary = item.get("summary", {})
        source_path = _safe_project_path(project_root, raw_path)
        if source_path is not None:
            display_path = source_path.relative_to(project_root.resolve()).as_posix()
        else:
            display_path = Path(raw_path).as_posix()
        source_hash = _sha256(source_path) if source_path and source_path.is_file() else None
        if source_path and source_path.is_file():
            snapshot = source_root / display_path
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, snapshot)

        evidence = CoverageFile(
            path=display_path,
            source_sha256=source_hash,
            statements=int(summary.get("num_statements", 0)),
            covered_lines=int(summary.get("covered_lines", 0)),
            missing_lines=tuple(int(line) for line in item.get("missing_lines", [])),
            branches=int(summary.get("num_branches", 0)),
            covered_branches=int(summary.get("covered_branches", 0)),
            missing_branches=tuple(tuple(map(int, branch)) for branch in item.get("missing_branches", [])),
        )
        files[display_path] = evidence.to_dict()

    totals = raw.get("totals", {})
    statements = int(totals.get("num_statements", 0))
    covered_lines = int(totals.get("covered_lines", 0))
    branches = int(totals.get("num_branches", 0))
    covered_branches = int(totals.get("covered_branches", 0))
    return {
        "backend": "coverage.py",
        "backend_version": raw.get("meta", {}).get("version"),
        "branch_coverage": bool(raw.get("meta", {}).get("branch_coverage", False)),
        "totals": {
            "statements": statements,
            "covered_lines": covered_lines,
            "missing_lines": max(statements - covered_lines, 0),
            "line_percent": 100.0 if statements == 0 else 100.0 * covered_lines / statements,
            "branches": branches,
            "covered_branches": covered_branches,
            "missing_branches": max(branches - covered_branches, 0),
            "branch_percent": 100.0 if branches == 0 else 100.0 * covered_branches / branches,
        },
        "files": files,
    }
