"""Normalize Coverage.py output and create source-scoped evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from ledgercore import ensure_inside_base, relative_to_base

from .analysis_scope import AnalysisScope
from .model import CoverageFile


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _safe_project_path(root: Path, raw_path: str) -> Path | None:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return ensure_inside_base(root.resolve(), candidate.resolve(), field_name="coverage source path")
    except Exception:
        return None


def _backend() -> dict[str, Any]:
    try:
        import coverage

        version = getattr(coverage, "__version__", None)
    except ImportError:
        version = None
    return {"name": "coverage.py", "version": version, "branch": True}


def unavailable_coverage(error: BaseException) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "status": "unavailable",
        "backend": _backend(),
        "error": {"type": type(error).__name__, "message": str(error)},
        "totals": None,
        "files": {},
    }


def normalize_coverage(
    raw_json: Path,
    *,
    project_root: Path,
    run_dir: Path,
    allowed_paths: frozenset[str] | set[str] | None = None,
    analysis_scope: AnalysisScope | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    scope_artifact = {
        "schema_version": 2,
        "analysis": analysis_scope.to_dict() if analysis_scope is not None else {},
        "files": {},
    }
    try:
        raw = json.loads(raw_json.read_text(encoding="utf-8"))
    except Exception as exc:
        return unavailable_coverage(exc), scope_artifact
    files: dict[str, dict[str, Any]] = {}
    scope_files: dict[str, dict[str, Any]] = {}
    source_root = run_dir / "sources"

    for raw_path, item in sorted(raw.get("files", {}).items()):
        summary = item.get("summary", {})
        source_path = _safe_project_path(project_root, raw_path)
        if source_path is not None:
            display_path = relative_to_base(project_root.resolve(), source_path)
        else:
            display_path = Path(raw_path).as_posix()
        if allowed_paths is not None and display_path not in allowed_paths:
            continue
        source_hash = _sha256(source_path) if source_path and source_path.is_file() else None
        if source_path and source_path.is_file() and source_hash:
            snapshot = source_root / display_path
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, snapshot)
            scope_files[display_path] = {
                "language": "python",
                "sha256": source_hash,
                "snapshot": f"sources/{display_path}",
            }
        evidence = CoverageFile(
            path=display_path,
            source_sha256=source_hash,
            statements=int(summary.get("num_statements", 0)),
            covered_lines=int(summary.get("covered_lines", 0)),
            executed_lines=tuple(int(line) for line in item.get("executed_lines", [])),
            missing_lines=tuple(int(line) for line in item.get("missing_lines", [])),
            branches=int(summary.get("num_branches", 0)),
            covered_branches=int(summary.get("covered_branches", 0)),
            executed_branches=tuple(tuple(map(int, branch)) for branch in item.get("executed_branches", [])),
            missing_branches=tuple(tuple(map(int, branch)) for branch in item.get("missing_branches", [])),
        )
        files[display_path] = evidence.to_dict()

    retained = list(files.values())
    statements = sum(int(item["statements"]) for item in retained)
    covered_lines = sum(int(item["covered_lines"]) for item in retained)
    branches = sum(int(item["branches"]) for item in retained)
    covered_branches = sum(int(item["covered_branches"]) for item in retained)
    executed_lines = tuple(int(line) for line in raw.get("executed_lines", []))
    executed_branches = tuple(tuple(map(int, branch)) for branch in raw.get("executed_branches", []))
    coverage = {
        "schema_version": 2,
        "status": "available",
        "backend": _backend()
        | {
            "version": raw.get("meta", {}).get("version"),
            "branch": bool(raw.get("meta", {}).get("branch_coverage", False)),
        },
        "totals": {
            "statements": statements,
            "covered_lines": covered_lines,
            "executed_lines": list(executed_lines),
            "missing_lines": max(statements - covered_lines, 0),
            "line_percent": 100.0 if statements == 0 else 100.0 * covered_lines / statements,
            "branches": branches,
            "covered_branches": covered_branches,
            "executed_branches": [list(branch) for branch in executed_branches],
            "missing_branches": max(branches - covered_branches, 0),
            "branch_percent": 100.0 if branches == 0 else 100.0 * covered_branches / branches,
        },
        "files": files,
    }
    scope_artifact["files"] = scope_files
    return coverage, scope_artifact


def function_coverage(function: Any, coverage_file: dict[str, Any]) -> dict[str, Any]:
    """Intersect positive and missing file obligations with one function region."""
    start, end = int(function.line), int(function.end_line)
    executed = {int(line) for line in coverage_file.get("executed_lines", []) if start <= int(line) <= end}
    missing = {int(line) for line in coverage_file.get("missing_lines", []) if start <= int(line) <= end}
    executed_branches = {
        tuple(map(int, branch))
        for branch in coverage_file.get("executed_branches", [])
        if start <= int(branch[0]) <= end
    }
    missing_branches = {
        tuple(map(int, branch))
        for branch in coverage_file.get("missing_branches", [])
        if start <= int(branch[0]) <= end
    }
    statements = len(executed | missing)
    branches = len(executed_branches | missing_branches)
    return {
        "statements": statements,
        "covered_lines": len(executed),
        "missing_lines": len(missing),
        "line_percent": 100.0 if not statements else 100.0 * len(executed) / statements,
        "branches": branches,
        "covered_branches": len(executed_branches),
        "missing_branches": len(missing_branches),
        "branch_percent": 100.0 if not branches else 100.0 * len(executed_branches) / branches,
    }
