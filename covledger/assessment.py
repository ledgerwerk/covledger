"""Build the compact assessment for one in-memory CovLedger analysis."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .coverage_data import function_coverage
from .decisions import UserDecision, decisions_from_config
from .gaps import GapCandidate, gaps_for_analysis
from .identity import function_id
from .scoring import attention_band, priority_score, rules_sha256
from .storage import load_covledger_config, resolve_source

GAP_ORDER = {"error-path": 0, "branch": 1, "line": 2}


def _source_status(root: Path, path: str, expected: str | None) -> str:
    try:
        current = resolve_source(root, path)
        if not current.is_file():
            return "missing"
        digest = hashlib.sha256(current.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return "missing"
    return "fresh" if expected and digest == expected else "changed"


def _quality_rows(analysis: dict[str, Any], decisions: tuple[UserDecision, ...]) -> list[dict[str, Any]]:
    ignored_symbols = {item.symbol for item in decisions if item.action == "ignore"}
    ignored_rules: dict[str, set[str]] = {}
    for item in decisions:
        if item.action == "ignore-finding" and item.rule is not None:
            ignored_rules.setdefault(item.symbol, set()).add(item.rule)
    rows: list[dict[str, Any]] = []
    for path, item in sorted(analysis.get("quality", {}).get("files", {}).items()):
        for function in item.get("functions", []):
            symbol = f"{path}:{function['qualname']}"
            if symbol in ignored_symbols:
                continue
            row = dict(function)
            row["path"] = path
            row.setdefault("function_id", function_id(path, row["qualname"]))
            ignored = ignored_rules.get(symbol, set())
            row["findings"] = [finding for finding in row.get("findings", []) if finding.get("id") not in ignored]
            row["semantic_findings"] = [
                finding for finding in row.get("semantic_findings", []) if finding not in ignored
            ]
            row.setdefault("facts", {})
            row["facts"] = dict(row["facts"]) | {
                "exact_findings": [finding.get("id") for finding in row["findings"]],
            }
            rows.append(row)
    return rows


def _function_gaps(gaps: list[GapCandidate], row: dict[str, Any]) -> list[GapCandidate]:
    return [
        gap
        for gap in gaps
        if gap.path == row["path"] and gap.function and gap.function.get("qualname") == row["qualname"]
    ]


def _coverage_for_row(analysis: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    item = analysis.get("coverage", {}).get("files", {}).get(row["path"], {})
    function = SimpleNamespace(line=row["line"], end_line=row["end_line"])
    return function_coverage(function, item)


def _work_mode(counts: dict[str, int], config: dict[str, Any]) -> dict[str, Any]:
    priority = config.get("priority", {})
    campaign_high_count = int(priority.get("campaign_high_count", 3))
    critical = counts.get("critical", 0)
    high_or_critical = counts.get("high", 0) + critical
    if critical >= 1:
        return {"name": "campaign", "reasons": ["critical_count>=1"]}
    if high_or_critical >= campaign_high_count:
        return {"name": "campaign", "reasons": [f"high_or_critical_count>={campaign_high_count}"]}
    if high_or_critical in {1, 2}:
        return {"name": "targeted", "reasons": ["one_or_two_high_or_critical_hotspots"]}
    return {"name": "backlog", "reasons": ["fewer_than_one_high_or_critical_hotspot"]}


def build_assessment(root: Path, analysis: dict[str, Any]) -> dict[str, Any]:
    """Build prioritization entirely from current source and in-memory evidence."""
    config = load_covledger_config(root)
    decisions = decisions_from_config(config)
    gaps = gaps_for_analysis(analysis, root)
    hotspots: list[dict[str, Any]] = []
    finding_rows: list[dict[str, Any]] = []
    gap_rows = [gap.to_dict() for gap in gaps]
    scope_files = analysis.get("scope", {}).get("files", {})

    for row in _quality_rows(analysis, decisions):
        function_gaps = _function_gaps(gaps, row)
        coverage = _coverage_for_row(analysis, row)
        selected_gap = min(function_gaps, key=lambda item: (GAP_ORDER[item.kind], item.line)) if function_gaps else None
        facts = row["facts"]
        breakdown = priority_score(
            facts,
            gap_kind=selected_gap.kind if selected_gap else None,
            line_missing_ratio=(coverage["missing_lines"] / coverage["statements"]) if coverage["statements"] else 0.0,
            branch_missing_ratio=(coverage["missing_branches"] / coverage["branches"]) if coverage["branches"] else 0.0,
            regression=None,
        )
        score = breakdown.to_dict()
        score["band"] = attention_band(breakdown.priority, config)
        expected = scope_files.get(row["path"], {}).get("sha256") or row.get("source_sha256")
        hotspot = {
            "id": row["function_id"],
            "path": row["path"],
            "qualname": row["qualname"],
            "line": row["line"],
            "end_line": row["end_line"],
            "facts": facts,
            "coverage": coverage,
            "finding_ids": [
                finding.get("finding_id") for finding in row.get("findings", []) if finding.get("finding_id")
            ],
            "semantic_findings": row.get("semantic_findings", []),
            "semantic_judgments": row.get("semantic", {}).get("judgments", {}),
            "semantic_score": row.get("semantic_score"),
            "gap_ids": [gap.id for gap in function_gaps],
            "score": score,
            "current_source": _source_status(root, row["path"], expected),
        }
        hotspots.append(hotspot)
        for finding in row.get("findings", []):
            finding_rows.append({"function_id": row["function_id"], "path": row["path"], **finding})

    counts = {
        band: sum(1 for item in hotspots if item["score"]["band"] == band)
        for band in ("critical", "high", "medium", "low")
    }
    risk_map: dict[str, dict[str, int]] = {}
    for item in hotspots:
        quality_bucket = (
            "quality >= 70"
            if item["score"]["quality"] >= 70
            else "quality 45..69"
            if item["score"]["quality"] >= 45
            else "quality < 45"
        )
        exposure_bucket = "exposed" if item["gap_ids"] else "no gap"
        risk_map.setdefault(quality_bucket, {"no gap": 0, "exposed": 0})[exposure_bucket] += 1
    hotspots.sort(key=lambda item: (-item["score"]["priority"], item["path"], item["line"], item["qualname"]))
    return {
        "schema_version": 1,
        "analysis_id": analysis.get("analysis_id"),
        "scorer": {"name": "covledger-priority", "version": 1, "rules_sha256": rules_sha256()},
        "summary": {
            "functions": len(hotspots),
            "functions_with_findings": sum(bool(item["finding_ids"]) for item in hotspots),
            "functions_with_gaps": sum(bool(item["gap_ids"]) for item in hotspots),
            "counts": counts,
            "risk_map": risk_map,
            "work_mode": _work_mode(counts, config),
            "stale_current_files": sum(item["current_source"] != "fresh" for item in hotspots),
        },
        "hotspots": hotspots,
        "findings": finding_rows,
        "gaps": gap_rows,
    }


__all__ = ["build_assessment"]
