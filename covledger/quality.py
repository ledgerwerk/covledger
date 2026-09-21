"""Deterministic Python quality facts with an explicit PyJev boundary."""

from __future__ import annotations

import hashlib
import importlib.resources
from pathlib import Path
from typing import Any

from ledgercore import dumps_json, load_json_object, write_json

from .source import FunctionFacts, discover_python_files, extract_functions, source_sha256
from .storage import semantic_cache_path

DEFAULT_THRESHOLD = 0.70
DECISION = "covledger-quality"


def collect_functions(target: Path, *, root: Path) -> list[FunctionFacts]:
    functions: list[FunctionFacts] = []
    for path in discover_python_files(target):
        functions.extend(extract_functions(path, root=root))
    return sorted(functions, key=lambda item: (item.path, item.line, item.qualname))


def facts_dict(item: FunctionFacts) -> dict[str, Any]:
    facts = item.facts()
    return {
        "path": item.path,
        "qualname": item.qualname,
        "line": item.line,
        "end_line": item.end_line,
        "line_count": item.line_count,
        "parameter_count": item.parameter_count,
        "max_nesting": item.max_nesting,
        "return_count": item.return_count,
        "decision_count": item.decision_count,
        "bare_except": item.bare_except,
        "broad_except": item.broad_except,
        "mutable_default": item.mutable_default,
        "eval_exec_calls": list(item.eval_exec_calls),
        "exact_findings": list(item.exact_findings),
        "facts": facts,
    }


def _config_text() -> str:
    return importlib.resources.files("covledger").joinpath("quality_rules.toml").read_text(encoding="utf-8")


def _rules_sha256() -> str:
    return hashlib.sha256(_config_text().encode("utf-8")).hexdigest()


def _materialized_config(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "quality_rules.toml"
    text = _config_text()
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return path


def _semantic_facts(item: FunctionFacts) -> dict[str, Any]:
    facts = item.facts()
    facts["broad_excepts"] = [{"caught": row["caught"]} for row in item.broad_excepts]
    facts["mutable_defaults"] = [
        {"parameter": row["parameter"], "expression": row["expression"]} for row in item.mutable_defaults
    ]
    facts["eval_exec_calls"] = [{"name": row["name"]} for row in item.eval_exec_call_details]
    facts["calls"] = [{"name": row["name"]} for row in item.calls]
    facts["imports"] = [
        {key: value for key, value in row.items() if key in {"module", "name"}} for row in item.imports
    ]
    return facts


def _semantic_key(item: FunctionFacts) -> str:
    payload = {
        "cache_schema": 2,
        "decision": DECISION,
        "rules_sha256": _rules_sha256(),
        "function_source_sha256": source_sha256(item.source),
        "exact_facts": _semantic_facts(item),
    }
    return hashlib.sha256(dumps_json(payload, compact=True).encode("utf-8")).hexdigest()

def semantic_cache_key(item: FunctionFacts) -> str:
    return _semantic_key(item)


def read_semantic_cache(root: Path, key: str) -> dict[str, Any] | None:
    path = semantic_cache_path(root, key)
    if not path.is_file():
        return None
    return load_json_object(path, label="semantic cache entry")


def semantic_judgments(item: FunctionFacts, *, root: Path, refresh: bool = False) -> dict[str, Any]:
    from pyjev import BundleResult, Jev, NoulResult

    cache_file = semantic_cache_path(root, _semantic_key(item))
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if cache_file.is_file() and not refresh:
        return load_json_object(cache_file, label="semantic cache entry")

    config = _materialized_config(cache_file.parent)
    state = {
        "language": "python",
        "path": item.path,
        "qualified_name": item.qualname,
        "start_line": item.line,
        "exact_facts": item.facts(),
        "source": item.source,
    }
    with Jev() as jev:
        result = jev.decide(DECISION, state=state, config=config)
    if not isinstance(result, BundleResult):
        raise TypeError(f"{DECISION!r} must return BundleResult, got {type(result).__name__}")
    judgments: dict[str, float] = {}
    for rule, answer in result.answers.items():
        if not isinstance(answer, NoulResult):
            raise TypeError(f"rule {rule!r} must return NoulResult, got {type(answer).__name__}")
        judgments[rule] = float(answer.value)
    payload = {
        "cache_schema": 2,
        "cache_key": _semantic_key(item),
        "model": result.model,
        "request_id": result.request_id,
        "usage": dict(result.usage),
        "judgments": judgments,
    }
    write_json(cache_file, payload)
    return payload


def _finding_rows(item: FunctionFacts) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    location_by_id = {
        "bare-except": [{"line": line} for line in item.bare_except_lines],
        "broad-except": list(item.broad_excepts),
        "mutable-default": list(item.mutable_defaults),
        "dynamic-code-execution": list(item.eval_exec_call_details),
    }
    for finding in item.exact_findings:
        locations = location_by_id.get(finding, [])
        if locations:
            for location in locations:
                rows.append({"id": finding, **location})
        else:
            rows.append({"id": finding, "line": item.line})
    return rows


def quality_document_from_sources(
    sources: dict[str, Path],
    *,
    root: Path,
    semantic: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
    refresh: bool = False,
) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for display_path, path in sorted(sources.items()):
        source = path.read_text(encoding="utf-8")
        functions = (
            extract_functions(path, root=path.parent)
            if path.parent == root
            else _extract_source_functions(source, display_path)
        )
        rows: list[dict[str, Any]] = []
        for item in functions:
            key = _semantic_key(item)
            row = {
                "qualname": item.qualname,
                "line": item.line,
                "end_line": item.end_line,
                "source_sha256": source_sha256(item.source),
                "facts": item.facts(),
                "findings": _finding_rows(item),
                "semantic_cache_key": key,
            }
            if semantic:
                result = semantic_judgments(item, root=root, refresh=refresh)
                row["semantic"] = result
                row["semantic_findings"] = [
                    rule for rule, probability in result["judgments"].items() if float(probability) >= threshold
                ]
            rows.append(row)
        files[display_path] = {"source_sha256": source_sha256(source), "functions": rows}
    return {
        "schema_version": 2,
        "analyzer": {"name": "covledger-python-ast", "version": 2},
        "files": files,
    }


def _extract_source_functions(source: str, display_path: str) -> list[FunctionFacts]:
    import ast

    tree = ast.parse(source, filename=display_path)
    from .source import _FunctionCollector

    collector = _FunctionCollector(source, display_path)
    for node in tree.body:
        collector.visit(node)
    return collector.functions


def quality_report(
    target: Path,
    *,
    root: Path,
    semantic: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
    refresh: bool = False,
    max_functions: int | None = None,
) -> dict[str, Any]:
    functions = collect_functions(target, root=root)
    if max_functions is not None:
        functions = functions[:max_functions]
    rows: list[dict[str, Any]] = []
    for function in functions:
        row = facts_dict(function)
        row["source_sha256"] = source_sha256(function.source)
        row["findings"] = _finding_rows(function)
        row["semantic_cache_key"] = _semantic_key(function)
        if semantic:
            semantic_result = semantic_judgments(function, root=root, refresh=refresh)
            row["semantic"] = semantic_result
            row["semantic_findings"] = [
                rule for rule, probability in semantic_result["judgments"].items() if float(probability) >= threshold
            ]
        rows.append(row)
    return {
        "schema_version": 2,
        "target": str(target),
        "semantic": semantic,
        "threshold": threshold,
        "function_count": len(rows),
        "functions": rows,
    }


__all__ = [
    "collect_functions",
    "facts_dict",
    "quality_document_from_sources",
    "quality_report",
    "read_semantic_cache",
    "semantic_cache_key",
    "semantic_judgments",
]
