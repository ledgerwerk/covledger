"""Deterministic selection of the next uncovered behavior to inspect."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .gaps import GapCandidate, gaps_for_run
from .quality import read_semantic_cache
from .storage import load_run, resolve_run_id, runs_root

GAP_ORDER = {"error-path": 0, "branch": 1, "line": 2}
HIGH_SIGNAL = {"bare-except", "broad-except", "dynamic-code-execution", "mutable-default"}
STRUCTURAL = {"deep-nesting", "many-decisions", "long-function", "many-return-paths", "long-parameter-list"}


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _quality_row(run: dict[str, Any], candidate: GapCandidate) -> dict[str, Any] | None:
    quality_file = run.get("quality", {}).get("files", {}).get(candidate.path, {})
    functions = quality_file.get("functions", [])
    if candidate.function is None:
        return None
    for row in functions:
        if row.get("qualname") == candidate.function.get("qualname"):
            return row
    return None


def _fresh(root: Path, run: dict[str, Any], candidate: GapCandidate) -> str:
    current = (root / candidate.path).resolve()
    if not current.is_file():
        return "missing"
    expected = run.get("coverage", {}).get("files", {}).get(candidate.path, {}).get("source_sha256")
    return "fresh" if expected and _sha256(current) == expected else "changed"


def _candidate_payload(root: Path, run: dict[str, Any], candidate: GapCandidate) -> dict[str, Any]:
    row = _quality_row(run, candidate)
    facts = row.get("facts", {}) if row else {}
    finding_rows = row.get("findings", []) if row else []
    findings = [item.get("id") for item in finding_rows if item.get("id")]
    semantic_payload = None
    if row and row.get("semantic_cache_key"):
        semantic_payload = read_semantic_cache(root, row["semantic_cache_key"])
    judgments = semantic_payload.get("judgments", {}) if semantic_payload else {}
    high_signal = bool(set(findings) & HIGH_SIGNAL)
    structural_count = len(set(findings) & STRUCTURAL)
    strongest_semantic = max((float(value) for value in judgments.values()), default=0.0)
    payload: dict[str, Any] = {
        "path": candidate.path,
        "line": candidate.line,
        "gap": {
            "kind": candidate.kind,
            "label": candidate.label,
            **({"from_line": candidate.from_line} if candidate.from_line is not None else {}),
            **({"to_line": candidate.to_line} if candidate.to_line is not None else {}),
        },
        "function": candidate.function,
        "deterministic": {"facts": facts, "findings": sorted(set(findings))},
        "semantic": {"source": "cache" if semantic_payload else "none", "judgments": judgments},
        "suggested_action": {
            "error-path": "inspect this uncovered error path",
            "branch": "inspect this uncovered branch",
            "line": "inspect this uncovered line",
        }[candidate.kind],
        "why": ["uncovered behavior"] + (["high-risk function"] if high_signal or structural_count else []),
        "current_source": _fresh(root, run, candidate),
        "_rank": (
            GAP_ORDER[candidate.kind],
            0 if high_signal else 1,
            -structural_count,
            -strongest_semantic,
            candidate.path,
            candidate.line,
        ),
    }
    return payload


def next_query(root: Path, selector: str = "latest") -> dict[str, Any]:
    run_id = resolve_run_id(root, selector)
    run = load_run(root, run_id)
    base = {"schema_version": 1, "run_id": run_id}
    suite_passed = run.get("suite_passed", run.get("suite", {}).get("passed", False))
    if not suite_passed:
        return {**base, "status": "blocked", "reason": "suite-failed"}
    if run.get("coverage", {}).get("status") != "available":
        return {**base, "status": "blocked", "reason": "coverage-unavailable"}

    run_dir = runs_root(root) / run_id
    candidates = gaps_for_run(run, run_dir)
    payloads = [_candidate_payload(root, run, candidate) for candidate in candidates]
    fresh = [item for item in payloads if item["current_source"] == "fresh"]
    if not fresh:
        if payloads:
            return {**base, "status": "blocked", "reason": "stale-run"}
        return {**base, "status": "ok", "candidate": None}
    selected = min(fresh, key=lambda item: item.pop("_rank"))
    return {**base, "status": "ok", "candidate": selected}


__all__ = ["next_query"]
