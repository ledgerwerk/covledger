"""Bounded semantic enrichment for CovLedger's deterministic next-gap workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .identity import function_id
from .inferlingo_rules import derive_proofs
from .next_query import ranked_gap_candidates
from .quality import (
    DEFAULT_THRESHOLD,
    SemanticCacheStatus,
    semantic_cache_status,
    semantic_judgments,
)
from .source import FunctionFacts, extract_functions, read_verified_source, source_sha256
from .storage import (
    StaleAnalysisError,
    load_covledger_config,
    load_current_analysis,
    load_fresh_current_analysis,
    resolve_source,
)

AGENT_GUIDANCE = {
    "objective": "Reduce or resolve the selected uncovered behavior while preserving existing behavior.",
    "recommended_sequence": [
        "Inspect the primary gap and the function's existing tests.",
        "Prefer the narrowest behavior-focused test that exercises the gap.",
        "Change production code only when the path is incorrect, unreachable, or needs a small testability refactor.",
        "Run a focused pytest target if practical.",
        "Run the full suite again through `covledger run -- pytest -q`.",
        "Use the next CovLedger analysis to verify that the selected gap changed.",
    ],
}


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    candidate: dict[str, Any]
    hotspot: dict[str, Any]
    facts: FunctionFacts
    file_source: str
    file_sha256: str
    gaps: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _PreflightTarget:
    target: _ResolvedTarget
    cache: SemanticCacheStatus


def _mode_options(
    *,
    max_requests: int | None,
    plan: bool,
    cache_only: bool,
    refresh: bool,
) -> None:
    if max_requests is not None and (isinstance(max_requests, bool) or max_requests <= 0):
        raise ValueError("--max-requests must be a positive integer")
    if plan and refresh:
        raise ValueError("--plan and --refresh cannot be used together")
    if cache_only and refresh:
        raise ValueError("--cache-only and --refresh cannot be used together")


def _suite_info(analysis: dict[str, Any]) -> dict[str, Any]:
    suite = analysis.get("suite", {})
    return {
        "status": "passed" if suite.get("passed", False) else "failed",
        "passed": bool(suite.get("passed", False)),
        "exit_code": suite.get("exit_code"),
        "pytest_args": suite.get("pytest_args"),
    }


def _coverage_info(analysis: dict[str, Any]) -> dict[str, Any]:
    coverage = analysis.get("coverage", {})
    result = {
        "status": coverage.get("status", "unavailable"),
        "backend": coverage.get("backend"),
        "totals": coverage.get("totals"),
        "file_count": len(coverage.get("files", {})),
    }
    if "error" in coverage:
        result["error"] = coverage["error"]
    return result


def _base_response(
    analysis: dict[str, Any],
    *,
    mode: str,
    status: str,
    candidates: list[dict[str, Any]] | None = None,
    targets: list[dict[str, Any]] | None = None,
    cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidates = candidates or []
    targets = targets or []
    return {
        "schema_version": 1,
        "command": "analyze",
        "analysis_id": analysis.get("analysis_id"),
        "status": status,
        "mode": mode,
        "selection": {
            "strategy": "current-next-priority",
            "definition": "highest deterministic priority actionable current gap",
            "candidate_count": len(candidates),
            "selected_function_count": len(targets),
            "selected_gap_count": sum(len(target.get("gaps", [])) for target in targets),
        },
        "cost": cost
        or {
            "max_api_requests": 0,
            "required_api_requests": 0,
            "api_requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "usage": None,
        },
        "suite": _suite_info(analysis),
        "coverage": _coverage_info(analysis),
        "targets": targets,
    }


def _expected_file_hash(analysis: dict[str, Any], path: str) -> str:
    expected = analysis.get("scope", {}).get("files", {}).get(path, {}).get("sha256")
    if not expected:
        expected = analysis.get("coverage", {}).get("files", {}).get(path, {}).get("source_sha256")
    if not isinstance(expected, str) or not expected:
        raise StaleAnalysisError([path])
    return expected


def _resolve_targets(
    root: Path,
    analysis: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[_ResolvedTarget]:
    assessment = analysis.get("assessment")
    if not isinstance(assessment, dict):
        raise ValueError("current CovLedger analysis is missing its assessment")
    hotspots = {item.get("id"): item for item in assessment.get("hotspots", [])}
    gaps_by_id = {
        row.get("gap_id") or row.get("gap", {}).get("gap_id"): row
        for row in assessment.get("gaps", [])
        if row.get("gap_id") or row.get("gap", {}).get("gap_id")
    }
    resolved_files: dict[str, tuple[str, str, dict[str, FunctionFacts]]] = {}
    targets: list[_ResolvedTarget] = []

    for candidate in candidates:
        function_key = candidate["function_id"]
        hotspot = hotspots.get(function_key)
        if not isinstance(hotspot, dict):
            raise StaleAnalysisError([f"missing hotspot {function_key}"])
        path = candidate["path"]
        expected_hash = _expected_file_hash(analysis, path)
        if path not in resolved_files:
            live_source = read_verified_source(root, path, expected_hash)
            source_path = resolve_source(root, path)
            functions = extract_functions(source_path, root=root)
            # Verify the exact file again after extraction so function facts and
            # the returned source text cannot silently straddle a checkout edit.
            if read_verified_source(root, path, expected_hash) != live_source:
                raise StaleAnalysisError([path])
            by_id: dict[str, FunctionFacts] = {}
            for function in functions:
                stable_id = function_id(function.path, function.qualname)
                if stable_id in by_id:
                    raise StaleAnalysisError([f"ambiguous live function {stable_id}"])
                by_id[stable_id] = function
            resolved_files[path] = (live_source, expected_hash, by_id)

        live_source, file_hash, by_id = resolved_files[path]
        facts = by_id.get(function_key)
        if facts is None:
            raise StaleAnalysisError([f"{path}: missing live function {function_key}"])
        associated_ids = set(hotspot.get("gap_ids", []))
        function_gaps = tuple(gaps_by_id[gap_id] for gap_id in associated_ids if gap_id in gaps_by_id)
        if not function_gaps:
            raise StaleAnalysisError([f"{path}: missing current gaps for {function_key}"])
        targets.append(_ResolvedTarget(candidate, hotspot, facts, live_source, file_hash, function_gaps))
    return targets


def _semantic_threshold(root: Path) -> float:
    quality = load_covledger_config(root).get("quality", {})
    value = (
        float(quality.get("semantic_threshold", DEFAULT_THRESHOLD)) if isinstance(quality, dict) else DEFAULT_THRESHOLD
    )
    if not 0.0 <= value <= 1.0:
        raise ValueError("semantic threshold must be between 0 and 1")
    return value


def _gap_view(gap: dict[str, Any]) -> dict[str, Any]:
    details = gap.get("gap", {})
    return {
        "gap_id": gap.get("gap_id") or details.get("gap_id"),
        "kind": details.get("kind"),
        "line": gap.get("line"),
        "label": details.get("label"),
        "from_line": details.get("from_line"),
        "to_line": details.get("to_line"),
    }


def _gap_excerpts(source: str, gaps: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    lines = source.splitlines()
    excerpts: list[dict[str, Any]] = []
    for gap in sorted(gaps, key=lambda row: (row.get("line", 0), row.get("gap_id", ""))):
        center = int(gap.get("line", 1))
        start = max(1, center - 2)
        end = min(len(lines), max(center + 2, int(gap.get("gap", {}).get("to_line") or center)))
        excerpts.append(
            {
                "gap_id": gap.get("gap_id"),
                "from_line": start,
                "to_line": end,
                "text": "\n".join(lines[start - 1 : end]),
            }
        )
    return excerpts


def _semantic_view(
    result: dict[str, Any] | None,
    *,
    status: str,
    threshold: float,
) -> dict[str, Any]:
    if result is None:
        return {
            "status": status,
            "threshold": threshold,
            "findings": None,
            "judgments": None,
            "model": None,
            "request_id": None,
            "usage": None,
        }
    judgments = result.get("judgments")
    findings = None
    if isinstance(judgments, dict):
        findings = [rule for rule, probability in judgments.items() if float(probability) >= threshold]
    return {
        "status": status,
        "threshold": threshold,
        "findings": findings,
        "judgments": judgments,
        "model": result.get("model"),
        "request_id": result.get("request_id"),
        "usage": result.get("usage"),
        "cache_key": result.get("cache_key"),
    }


def _target_view(
    target: _ResolvedTarget,
    *,
    rank: int,
    semantic_result: dict[str, Any] | None,
    semantic_status: str,
    assessment: dict[str, Any],
    threshold: float,
) -> dict[str, Any]:
    candidate = target.candidate
    hotspot = target.hotspot
    gaps = [_gap_view(gap) for gap in target.gaps]
    primary_id = candidate.get("gap_id")
    primary = next((gap for gap in gaps if gap.get("gap_id") == primary_id), None)
    if primary is None:
        raise StaleAnalysisError([f"{candidate['path']}: missing primary gap {primary_id}"])
    findings = [
        dict(row) for row in assessment.get("findings", []) if row.get("function_id") == candidate["function_id"]
    ]
    return {
        "rank": rank,
        "function_id": candidate["function_id"],
        "path": candidate["path"],
        "qualname": target.facts.qualname,
        "line": target.facts.line,
        "end_line": target.facts.end_line,
        "coverage": dict(hotspot.get("coverage", {})),
        "priority": {
            "score": candidate["priority_score"],
            "band": candidate["band"],
            "breakdown": dict(hotspot.get("score", candidate.get("score_breakdown", {}))),
        },
        "primary_gap": primary,
        "gaps": gaps,
        "deterministic": {
            "facts": dict(hotspot.get("facts", {})),
            "findings": findings,
        },
        "semantic": _semantic_view(semantic_result, status=semantic_status, threshold=threshold),
        "proofs": derive_proofs(assessment, hotspot),
        "source": {
            "sha256": source_sha256(target.facts.source),
            "file_sha256": target.file_sha256,
            "function_text": target.facts.source,
            "gap_excerpt": _gap_excerpts(target.file_source, target.gaps),
        },
        "agent": dict(AGENT_GUIDANCE),
    }


def _aggregate_usage(results: list[dict[str, Any]]) -> dict[str, int | float] | None:
    usages: list[dict[str, Any]] = []
    for result in results:
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        usages.append(usage)
    if not usages:
        return None
    common_keys = set(usages[0])
    for usage in usages[1:]:
        common_keys.intersection_update(usage)
    totals: dict[str, int | float] = {}
    for key in sorted(common_keys):
        values = [usage[key] for usage in usages]
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            totals[key] = sum(values)
    return totals or None


def analyze_current(
    root: Path,
    *,
    all_functions: bool = False,
    max_requests: int | None = None,
    plan: bool = False,
    cache_only: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    """Select fresh current gaps and semantically enrich only within a hard budget."""
    _mode_options(
        max_requests=max_requests,
        plan=plan,
        cache_only=cache_only,
        refresh=refresh,
    )
    root = root.resolve()
    mode = "all" if all_functions else "single"
    try:
        analysis = load_fresh_current_analysis(root)
    except StaleAnalysisError as exc:
        stale_analysis = load_current_analysis(root)
        stale_response = _base_response(stale_analysis, mode=mode, status="stale-analysis")
        stale_response["stale"] = {"changed": list(exc.changed)}
        stale_response["next_step"] = "covledger run -- pytest -q"
        return stale_response
    if not analysis.get("suite", {}).get("passed", False):
        return _base_response(analysis, mode=mode, status="suite-failed")
    if analysis.get("coverage", {}).get("status") != "available":
        return _base_response(analysis, mode=mode, status="coverage-unavailable")

    assessment = analysis.get("assessment")
    if not isinstance(assessment, dict):
        raise ValueError("current CovLedger analysis is missing its assessment")
    ranked = ranked_gap_candidates(analysis)
    if not ranked:
        if assessment.get("summary", {}).get("functions_with_gaps"):
            stale_response = _base_response(analysis, mode=mode, status="stale-analysis", candidates=ranked)
            stale_response["stale"] = {"reason": "no-fresh-gap-candidate"}
            stale_response["next_step"] = "covledger run -- pytest -q"
            return stale_response
        return _base_response(analysis, mode=mode, status="no-target", candidates=ranked)

    selected = ranked if all_functions else ranked[:1]
    # Defensively deduplicate stable identities while preserving the established rank.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in selected:
        function_key = candidate["function_id"]
        if function_key not in seen:
            seen.add(function_key)
            unique.append(candidate)
    resolved = _resolve_targets(root, analysis, unique)
    preflight = [_PreflightTarget(target, semantic_cache_status(target.facts, root=root)) for target in resolved]
    threshold = _semantic_threshold(root)
    cache_hits = sum(item.cache.cached and not refresh for item in preflight)
    cache_misses = sum(not item.cache.cached for item in preflight)
    required_requests = len(preflight) if refresh else sum(not item.cache.cached for item in preflight)
    if all_functions:
        effective_budget = max_requests
        budget_sufficient = required_requests == 0 or (max_requests is not None and required_requests <= max_requests)
    else:
        effective_budget = 1
        budget_sufficient = required_requests <= 1

    insufficient_explicit_budget = all_functions and max_requests is not None and required_requests > max_requests
    missing_batch_budget = (
        all_functions and max_requests is None and required_requests > 0 and not plan and not cache_only
    )
    budget_blocked = insufficient_explicit_budget or missing_batch_budget

    cost: dict[str, Any] = {
        "max_api_requests": effective_budget,
        "required_api_requests": required_requests,
        "api_requests": 0,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "usage": None,
        "budget_sufficient": budget_sufficient,
    }

    if plan:
        semantic_rows = [(item.cache.result, "cached" if item.cache.cached else "not-run-plan") for item in preflight]
        final_status = "budget-blocked" if insufficient_explicit_budget else "planned"
    elif cache_only:
        semantic_rows = [
            (item.cache.result if item.cache.cached else None, "cached" if item.cache.cached else "not-run-cache-miss")
            for item in preflight
        ]
        final_status = "cache-only"
    elif budget_blocked:
        semantic_rows = [
            (
                item.cache.result if item.cache.cached and not refresh else None,
                "cached" if item.cache.cached and not refresh else "not-run-budget",
            )
            for item in preflight
        ]
        final_status = "budget-blocked"
        if refresh:
            cost["cache_hits"] = 0
    else:
        semantic_rows: list[tuple[dict[str, Any] | None, str]] = []
        fresh_results: list[dict[str, Any]] = []
        for item in preflight:
            if item.cache.cached and not refresh:
                assert item.cache.result is not None
                semantic_rows.append((item.cache.result, "cached"))
                continue
            if cost["api_requests"] >= (effective_budget if effective_budget is not None else 0):
                raise RuntimeError("semantic request budget exhausted after preflight")
            result = semantic_judgments(item.target.facts, root=root, refresh=refresh)
            cost["api_requests"] += 1
            fresh_results.append(result)
            semantic_rows.append((result, "fresh"))
        cost["usage"] = _aggregate_usage(fresh_results)
        final_status = "ok"

    targets = [
        _target_view(
            item.target,
            rank=index,
            semantic_result=semantic_rows[index - 1][0],
            semantic_status=semantic_rows[index - 1][1],
            assessment=assessment,
            threshold=threshold,
        )
        for index, item in enumerate(preflight, 1)
    ]
    if final_status == "budget-blocked" and refresh:
        cost["cache_hits"] = 0
    return _base_response(
        analysis,
        mode=mode,
        status=final_status,
        candidates=ranked,
        targets=targets,
        cost=cost,
    )


__all__ = ["AGENT_GUIDANCE", "analyze_current"]
