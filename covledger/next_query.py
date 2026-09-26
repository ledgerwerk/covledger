"""Select the next actionable hotspot from one fresh current analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .storage import load_fresh_current_analysis

GAP_ORDER = {"error-path": 0, "branch": 1, "line": 2}


def _gap_id(row: dict[str, Any]) -> str | None:
    return row.get("gap_id") or row.get("gap", {}).get("gap_id")


def _candidate(
    assessment: dict[str, Any],
    analysis: dict[str, Any],
    hotspot: dict[str, Any],
    gap: dict[str, Any],
) -> dict[str, Any]:
    gap_data = gap["gap"]
    finding_ids = hotspot.get("finding_ids", [])
    semantic_findings = hotspot.get("semantic_findings", [])
    return {
        "analysis_id": analysis["analysis_id"],
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
        "semantic": {
            "source": "current-analysis" if semantic_findings else "none",
            "score": hotspot.get("semantic_score"),
            "judgments": hotspot.get("semantic_judgments", {}),
            "finding_ids": semantic_findings,
        },
        "suggested_action": {
            "error-path": "inspect this uncovered error path",
            "branch": "inspect this uncovered branch",
            "line": "inspect this uncovered line",
        }[gap_data["kind"]],
        "why": [
            "uncovered behavior",
            f"priority score {hotspot['score']['priority']}",
        ]
        + (["deterministic findings"] if finding_ids else [])
        + (["semantic findings"] if semantic_findings else []),
        "current_source": hotspot["current_source"],
        "repository_context": {
            "work_mode": assessment["summary"]["work_mode"],
            "counts": assessment["summary"]["counts"],
        },
    }


def ranked_gap_candidates(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every actionable gap candidate in the deterministic next-query order.

    This enumerator is deliberately independent of semantic judgments so all
    workflows select targets from the same current deterministic evidence.
    """
    assessment = analysis.get("assessment")
    if not isinstance(assessment, dict):
        raise ValueError("current CovLedger analysis is missing its assessment")
    gaps_by_id = {_gap_id(row): row for row in assessment.get("gaps", []) if _gap_id(row)}
    candidates: list[dict[str, Any]] = []
    for hotspot in assessment.get("hotspots", []):
        if hotspot.get("current_source") != "fresh":
            continue
        gaps = [gaps_by_id[gap_id] for gap_id in hotspot.get("gap_ids", []) if gap_id in gaps_by_id]
        if not gaps:
            continue
        gap = min(gaps, key=lambda row: (GAP_ORDER[row["gap"]["kind"]], row["line"], row["path"]))
        candidates.append(_candidate(assessment, analysis, hotspot, gap))
    return sorted(
        candidates,
        key=lambda item: (
            -item["priority_score"],
            item["path"],
            item["line"],
            item["function_id"],
        ),
    )


def next_query(root: Path) -> dict[str, Any]:
    """Return a next candidate only from current, source/config-fresh analysis."""
    analysis = load_fresh_current_analysis(root)
    base = {"schema_version": 1, "analysis_id": analysis["analysis_id"]}
    if not analysis.get("suite", {}).get("passed", False):
        return {**base, "status": "blocked", "reason": "suite-failed"}
    if analysis.get("coverage", {}).get("status") != "available":
        return {**base, "status": "blocked", "reason": "coverage-unavailable"}

    assessment = analysis.get("assessment")
    if not isinstance(assessment, dict):
        raise ValueError("current CovLedger analysis is missing its assessment")
    candidates = ranked_gap_candidates(analysis)
    if not candidates:
        if assessment.get("summary", {}).get("functions_with_gaps"):
            return {**base, "status": "blocked", "reason": "stale-analysis"}
        return {**base, "status": "ok", "candidate": None, "repository_context": assessment.get("summary", {})}
    return {**base, "status": "ok", "candidate": candidates[0], "repository_context": assessment["summary"]}


__all__ = ["next_query", "ranked_gap_candidates"]
