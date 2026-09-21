"""Deterministic, inspectable CovLedger scoring policy."""

from __future__ import annotations

import hashlib
import importlib.resources
import tomllib
from typing import Any

from .assessment_models import ScoreBreakdown


def rules_text() -> str:
    return importlib.resources.files("covledger").joinpath("scoring_rules.toml").read_text(encoding="utf-8")


def rules() -> dict[str, Any]:
    return tomllib.loads(rules_text())


def rules_sha256() -> str:
    return hashlib.sha256(rules_text().encode("utf-8")).hexdigest()


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _excess(value: float, start: float, severe: float) -> float:
    if severe <= start:
        raise ValueError("scoring severe threshold must be greater than start")
    return _clamp((value - start) / (severe - start), 0.0, 1.0)


def hazard_score(facts: dict[str, Any], config: dict[str, Any] | None = None) -> int:
    weights = (config or rules())["hazard"]
    product = 1.0
    for finding, weight in weights.items():
        if (
            finding == "bare-except"
            and facts.get("bare_except")
            or finding == "broad-except"
            and facts.get("broad_except")
        ):
            product *= 1.0 - float(weight)
        elif finding == "dynamic-code-execution" and facts.get("eval_exec_calls"):
            product *= 1.0 - float(weight)
        elif finding == "mutable-default" and facts.get("mutable_default"):
            product *= 1.0 - float(weight)
    return round(100.0 * (1.0 - product))


def structural_score(facts: dict[str, Any], config: dict[str, Any] | None = None) -> int:
    source = config or rules()
    values = {
        "lines": facts.get("line_count", 0),
        "decisions": facts.get("decision_count", 0),
        "nesting": facts.get("max_nesting", 0),
        "returns": facts.get("return_count", 0),
        "parameters": facts.get("parameter_count", 0),
    }
    total = 0.0
    for metric, value in values.items():
        setting = source["structure"][metric]
        total += float(setting["weight"]) * _excess(float(value), float(setting["start"]), float(setting["severe"]))
    return round(100.0 * total)


def quality_score(facts: dict[str, Any], config: dict[str, Any] | None = None) -> tuple[int, int, int]:
    source = config or rules()
    hazard = hazard_score(facts, source)
    structural = structural_score(facts, source)
    total = (
        float(source["quality"]["hazard_weight"]) * hazard + float(source["quality"]["structure_weight"]) * structural
    )
    findings = set(facts.get("exact_findings", []))
    if findings & {"bare-except", "broad-except"} and structural >= 50:
        total += 8
    if facts.get("line_count", 0) >= 100 and facts.get("decision_count", 0) >= 20:
        total += 6
    return hazard, structural, round(_clamp(total))


def attention_band(score: int, config: dict[str, Any] | None = None) -> str:
    policy = (config or {}).get("priority", {})
    critical = int(policy.get("critical", 85))
    high = int(policy.get("high", 70))
    medium = int(policy.get("medium", 45))
    if score >= critical:
        return "critical"
    if score >= high:
        return "high"
    if score >= medium:
        return "medium"
    return "low"


def priority_score(
    facts: dict[str, Any],
    *,
    gap_kind: str | None = None,
    line_missing_ratio: float = 0.0,
    branch_missing_ratio: float = 0.0,
    regression: int | None = None,
    config: dict[str, Any] | None = None,
) -> ScoreBreakdown:
    source = config or rules()
    hazard, structural, quality = quality_score(facts, source)
    peak = float(source["exposure"].get(f"gap_{gap_kind}", 0.0)) if gap_kind else 0.0
    exposure = round(
        _clamp(
            100.0
            * (
                float(source["exposure"]["gap_weight"]) * peak
                + float(source["exposure"]["line_ratio_weight"]) * line_missing_ratio
                + float(source["exposure"]["branch_ratio_weight"]) * branch_missing_ratio
            ),
        )
    )
    bonuses: list[dict[str, Any]] = []
    if gap_kind == "error-path" and (facts.get("bare_except", False) or facts.get("broad_except", False)):
        bonuses.append({"points": 12, "reason": "uncovered error path and exception hazard"})
    if gap_kind == "branch" and facts.get("decision_count", 0) >= 10:
        bonuses.append({"points": 8, "reason": "uncovered branch and many decisions"})
    if facts.get("eval_exec_calls") and gap_kind:
        bonuses.append({"points": 6, "reason": "dynamic execution with coverage gap"})
    if regression is not None and regression >= 60 and quality >= 60:
        bonuses.append({"points": 5, "reason": "high regression and high quality"})
    if regression is None:
        base = 0.56 * exposure + 0.44 * quality
    else:
        base = 0.45 * exposure + 0.35 * quality + 0.20 * regression
    priority = round(_clamp(base + sum(int(item["points"]) for item in bonuses)))
    return ScoreBreakdown(hazard, structural, quality, exposure, regression, tuple(bonuses), priority)


def semantic_score(judgments: dict[str, Any], weights: dict[str, float] | None = None) -> int:
    default_weights = {
        "ambiguous error contract": 0.90,
        "surprising mutation": 0.85,
        "hidden I/O": 0.80,
        "too many jobs": 0.80,
        "implicit state dependence": 0.75,
        "hidden side effect": 0.70,
        "policy/mechanism mix": 0.65,
        "coupled concerns": 0.65,
        "misleading name": 0.45,
        "unexplained special case": 0.45,
    }
    source = weights or default_weights
    product = 1.0
    for rule, probability in judgments.items():
        if rule in source:
            product *= 1.0 - float(source[rule]) * float(probability)
    return round(_clamp(100.0 * (1.0 - product)))


def semantic_modifier(judgments: dict[str, Any]) -> int:
    return round(15 * semantic_score(judgments) / 100)


def augmented_priority(priority: int, judgments: dict[str, Any]) -> int:
    return round(_clamp(priority + semantic_modifier(judgments)))


__all__ = [
    "attention_band",
    "augmented_priority",
    "hazard_score",
    "priority_score",
    "quality_score",
    "semantic_modifier",
    "semantic_score",
    "rules",
    "rules_sha256",
    "rules_text",
    "structural_score",
]
