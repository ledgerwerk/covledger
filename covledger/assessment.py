"""Join immutable CovLedger evidence into deterministic hotspot assessments."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .coverage_data import function_coverage
from .gaps import GapCandidate, gaps_for_run
from .identity import function_id
from .scoring import attention_band, priority_score, quality_score, rules_sha256
from .storage import list_run_ids, load_covledger_config, load_run, runs_root

GAP_ORDER = {"error-path": 0, "branch": 1, "line": 2}


def _fresh(root: Path, path: str, expected: str | None) -> str:
    current = (root / path).resolve()
    if not current.is_file():
        return "missing"
    digest = hashlib.sha256(current.read_bytes()).hexdigest()
    return "fresh" if expected and digest == expected else "changed"


def _quality_rows(run: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, item in sorted(run.get("quality", {}).get("files", {}).items()):
        for row in item.get("functions", []):
            row = dict(row)
            row["path"] = path
            row.setdefault("function_id", function_id(path, row["qualname"]))
            row.setdefault("facts", {})
            row["facts"] = dict(row["facts"]) | {
                "exact_findings": [finding.get("id") for finding in row.get("findings", [])],
            }
            rows.append(row)
    return rows


def _function_gaps(gaps: list[GapCandidate], row: dict[str, Any]) -> list[GapCandidate]:
    return [
        gap
        for gap in gaps
        if gap.path == row["path"] and gap.function and gap.function.get("qualname") == row["qualname"]
    ]


def _coverage_for_row(run: dict[str, Any], row: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    item = run.get("coverage", {}).get("files", {}).get(row["path"], {})
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


def _regression_scores(root: Path, run: dict[str, Any]) -> dict[str, int]:
    prior_ids = [run_id for run_id in list_run_ids(root) if run_id < run["run_id"]]
    if not prior_ids:
        return {}
    previous = load_run(root, prior_ids[-1])
    current_rows = {row["function_id"]: row for row in _quality_rows(run)}
    previous_rows = {row["function_id"]: row for row in _quality_rows(previous)}
    current_gaps = gaps_for_run(run, runs_root(root) / run["run_id"])
    previous_gaps = gaps_for_run(previous, runs_root(root) / previous["run_id"])
    current_by_function: dict[str, set[str]] = {}
    previous_by_function: dict[str, set[str]] = {}
    for gap in current_gaps:
        if gap.function:
            current_by_function.setdefault(function_id(gap.path, gap.function["qualname"]), set()).add(gap.kind)
    for gap in previous_gaps:
        if gap.function:
            previous_by_function.setdefault(function_id(gap.path, gap.function["qualname"]), set()).add(gap.kind)
    result: dict[str, int] = {}
    for function, row in current_rows.items():
        old = previous_rows.get(function)
        if old is None:
            continue
        signals: list[int] = []
        current_kinds = current_by_function.get(function, set())
        previous_kinds = previous_by_function.get(function, set())
        current_hash = run.get("coverage", {}).get("files", {}).get(row["path"], {}).get("source_sha256")
        previous_hash = previous.get("coverage", {}).get("files", {}).get(row["path"], {}).get("source_sha256")
        if current_hash and current_hash == previous_hash:
            if "error-path" in current_kinds - previous_kinds:
                signals.append(100)
            if "branch" in current_kinds - previous_kinds:
                signals.append(80)
        old_facts = old.get("facts", {})
        current_facts = row.get("facts", {})
        old_hazards = set(old.get("exact_findings", []))
        current_hazards = set(row.get("exact_findings", []))
        if current_hazards - old_hazards:
            signals.append(60)
        old_quality = old.get("quality_score", quality_score(old_facts)[2])
        current_quality = row.get("quality_score", quality_score(current_facts)[2])
        if current_quality - old_quality >= 20:
            signals.append(40)
        old_coverage = previous.get("coverage", {}).get("files", {}).get(row["path"], {})
        current_coverage = run.get("coverage", {}).get("files", {}).get(row["path"], {})
        if current_hash == previous_hash and old_coverage.get("statements") and current_coverage.get("statements"):
            old_percent = 100.0 * old_coverage.get("covered_lines", 0) / old_coverage["statements"]
            current_percent = 100.0 * current_coverage.get("covered_lines", 0) / current_coverage["statements"]
            if old_percent - current_percent >= 10:
                signals.append(20)
        if signals:
            result[function] = max(signals)
    return result


def build_assessment(root: Path, run: dict[str, Any], run_dir: Path | None = None) -> dict[str, Any]:
    run_dir = run_dir or runs_root(root) / run["run_id"]
    config = load_covledger_config(root)
    gaps = gaps_for_run(run, run_dir)
    regressions = _regression_scores(root, run)
    hotspots: list[dict[str, Any]] = []
    finding_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    for gap in gaps:
        gap_rows.append(gap.to_dict())
    for row in _quality_rows(run):
        function_gaps = _function_gaps(gaps, row)
        coverage = _coverage_for_row(run, row, run_dir)
        selected_gap = min(function_gaps, key=lambda item: (GAP_ORDER[item.kind], item.line)) if function_gaps else None
        facts = row["facts"]
        breakdown = priority_score(
            facts,
            gap_kind=selected_gap.kind if selected_gap else None,
            line_missing_ratio=(coverage["missing_lines"] / coverage["statements"]) if coverage["statements"] else 0.0,
            branch_missing_ratio=(coverage["missing_branches"] / coverage["branches"]) if coverage["branches"] else 0.0,
            regression=regressions.get(row["function_id"]),
        )
        score = breakdown.to_dict()
        score["band"] = attention_band(breakdown.priority, config)
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
            "gap_ids": [gap.id for gap in function_gaps],
            "score": score,
            "current_source": _fresh(
                root,
                row["path"],
                run.get("coverage", {}).get("files", {}).get(row["path"], {}).get("source_sha256"),
            ),
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
        "run_id": run["run_id"],
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


def assessment_for_run(root: Path, run: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
    run_id = run["run_id"]
    run_dir = runs_root(root) / run_id
    path = run_dir / "assessment.json"
    if path.is_file():
        from ledgercore import load_json_object

        return load_json_object(path, label="assessment artifact")
    assessment = build_assessment(root, run, run_dir)
    if persist:
        from ledgercore import atomic_create_text, dumps_json

        atomic_create_text(path, dumps_json(assessment))
    return assessment


__all__ = ["assessment_for_run", "build_assessment"]
