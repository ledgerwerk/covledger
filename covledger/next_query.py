"""Select the next actionable hotspot from the shared assessment model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .assessment import assessment_for_run
from .storage import load_run

GAP_ORDER = {"error-path": 0, "branch": 1, "line": 2}


def _gap_id(row: dict[str, Any]) -> str | None:
    return row.get("gap_id") or row.get("gap", {}).get("gap_id")


def _candidate(assessment: dict[str, Any], hotspot: dict[str, Any], gap: dict[str, Any]) -> dict[str, Any]:
    gap_data = gap["gap"]
    finding_ids = hotspot.get("finding_ids", [])
    return {
        "function_id": hotspot["id"],
        "gap_id": _gap_id(gap),
        "path": hotspot["path"],
        "line": gap["line"],
        "gap": gap_data,
        "function": {
            "qualname": hotspot["qualname"],
            "line": hotspot["line"],
            "end_line": hotspot["end_line"],
        },
        "priority_score": hotspot["score"]["priority"],
        "band": hotspot["score"]["band"],
        "score_breakdown": hotspot["score"],
        "deterministic": {"facts": hotspot["facts"], "finding_ids": finding_ids},
        "semantic": {"source": "none", "score": None, "judgments": {}},
        "suggested_action": {
            "error-path": "inspect this uncovered error path",
            "branch": "inspect this uncovered branch",
            "line": "inspect this uncovered line",
        }[gap_data["kind"]],
        "why": [
            "uncovered behavior",
            f"priority score {hotspot['score']['priority']}",
        ]
        + (["deterministic findings"] if finding_ids else []),
        "current_source": hotspot["current_source"],
        "repository_context": {
            "work_mode": assessment["summary"]["work_mode"],
            "counts": assessment["summary"]["counts"],
        },
    }


def next_query(root: Path, selector: str = "latest") -> dict[str, Any]:
    run = load_run(root, selector)
    run_id = run["run_id"]
    base = {"schema_version": 2, "run_id": run_id}
    suite_passed = run.get("suite_passed", run.get("suite", {}).get("passed", False))
    if not suite_passed:
        return {**base, "status": "blocked", "reason": "suite-failed"}
    if run.get("coverage", {}).get("status") != "available":
        return {**base, "status": "blocked", "reason": "coverage-unavailable"}
    assessment = assessment_for_run(root, run)
    gaps_by_id = {_gap_id(row): row for row in assessment["gaps"] if _gap_id(row)}
    candidates: list[dict[str, Any]] = []
    for hotspot in assessment["hotspots"]:
        if hotspot["current_source"] != "fresh":
            continue
        gaps = [gaps_by_id[gap_id] for gap_id in hotspot.get("gap_ids", []) if gap_id in gaps_by_id]
        if not gaps:
            continue
        gap = min(gaps, key=lambda row: (GAP_ORDER[row["gap"]["kind"]], row["line"], row["path"]))
        candidates.append(_candidate(assessment, hotspot, gap))
    if not candidates:
        if assessment["summary"]["functions_with_gaps"]:
            return {**base, "status": "blocked", "reason": "stale-run"}
        return {**base, "status": "ok", "candidate": None, "repository_context": assessment["summary"]}
    selected = min(
        candidates,
        key=lambda item: (
            -item["priority_score"],
            item["path"],
            item["line"],
            item["function_id"],
        ),
    )
    return {**base, "status": "ok", "candidate": selected, "repository_context": assessment["summary"]}


__all__ = ["next_query"]
