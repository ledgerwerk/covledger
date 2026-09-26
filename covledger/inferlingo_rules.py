"""Exact InferLingo labels and provenance for CovLedger assessments."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
from typing import Any

from inferlingo import ExactUnifier, KnowledgeBase, Provenance


def rules_text() -> str:
    return importlib.resources.files("covledger").joinpath("priority_rules.nl").read_text(encoding="utf-8")


def rules_sha256() -> str:
    return hashlib.sha256(rules_text().encode("utf-8")).hexdigest()


def _facts_for_hotspot(
    assessment: dict[str, Any], hotspot: dict[str, Any]
) -> list[tuple[str, dict[str, str], Provenance]]:
    fn = hotspot["id"]
    provenance = Provenance(
        source=hotspot["path"],
        line=hotspot["line"],
        kind="covledger-assessment",
        metadata={"function_id": fn, "analysis_id": assessment.get("analysis_id")},
    )
    facts: list[tuple[str, dict[str, str], Provenance]] = []
    exact_findings = set(hotspot.get("facts", {}).get("exact_findings", []))
    if "broad-except" in exact_findings or "bare-except" in exact_findings:
        facts.append(("Function {fn} catches a broad exception", {"fn": fn}, provenance))
    if any(hotspot.get("facts", {}).get("gap_kinds", [])) or hotspot.get("gap_ids"):
        if any(
            gap["gap"]["kind"] == "error-path"
            for gap in assessment["gaps"]
            if gap.get("gap_id") in hotspot.get("gap_ids", [])
        ):
            facts.append(("Function {fn} has an uncovered error path", {"fn": fn}, provenance))
    if hotspot.get("facts", {}).get("line_count", 0) >= 50:
        facts.append(("Function {fn} is long", {"fn": fn}, provenance))
    if hotspot.get("facts", {}).get("decision_count", 0) >= 10:
        facts.append(("Function {fn} has many decisions", {"fn": fn}, provenance))
    return facts


def derive_proofs(assessment: dict[str, Any], hotspot: dict[str, Any]) -> list[dict[str, Any]]:
    rules = importlib.resources.files("covledger").joinpath("priority_rules.nl")
    knowledge = KnowledgeBase.from_file(rules, unifier=ExactUnifier())
    for template, values, provenance in _facts_for_hotspot(assessment, hotspot):
        knowledge.add_fact(template, provenance=provenance, **values)
    proofs: list[dict[str, Any]] = []
    for query in ("Function {fn} needs error-path review?", "Function {fn} needs structural review?"):
        result = asyncio.run(knowledge.ask(query))
        for solution in result.solutions:
            rendered = solution.proof.render()
            source = f"{hotspot['path']}:{hotspot['line']}"
            if hotspot["path"] not in rendered:
                rendered = f"{rendered}\nEvidence source: {source}"
            proofs.append(
                {
                    "query": query,
                    "bindings": dict(solution.bindings),
                    "rendered": rendered,
                }
            )
    return proofs


__all__ = ["derive_proofs", "rules_sha256", "rules_text"]
