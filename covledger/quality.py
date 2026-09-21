"""Deterministic Python facts with an optional explicit PyJev semantic boundary."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
from pathlib import Path
from typing import Any

from .source import FunctionFacts, discover_python_files, extract_functions
from .storage import state_root

DEFAULT_THRESHOLD = 0.70
DECISION = "covledger-quality"


def collect_functions(target: Path, *, root: Path) -> list[FunctionFacts]:
    functions: list[FunctionFacts] = []
    for path in discover_python_files(target):
        functions.extend(extract_functions(path, root=root))
    return sorted(functions, key=lambda item: (item.path, item.line, item.qualname))


def facts_dict(item: FunctionFacts) -> dict[str, Any]:
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
    }


def _config_text() -> str:
    return importlib.resources.files("covledger").joinpath("quality_rules.toml").read_text(encoding="utf-8")


def _materialized_config(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "quality_rules.toml"
    text = _config_text()
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return path


def _semantic_key(item: FunctionFacts) -> str:
    payload = json.dumps(
        {
            "decision": DECISION,
            "config": _config_text(),
            "state": {
                "language": "python",
                "path": item.path,
                "qualified_name": item.qualname,
                "start_line": item.line,
                "exact_facts": facts_dict(item),
                "source": item.source,
            },
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def semantic_judgments(item: FunctionFacts, *, root: Path, refresh: bool = False) -> dict[str, Any]:
    from pyjev import BundleResult, Jev, NoulResult

    cache_root = state_root(root) / "cache" / "jev"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_file = cache_root / f"{_semantic_key(item)}.json"
    if cache_file.is_file() and not refresh:
        return json.loads(cache_file.read_text(encoding="utf-8"))

    config = _materialized_config(cache_root)
    state = {
        "language": "python",
        "path": item.path,
        "qualified_name": item.qualname,
        "start_line": item.line,
        "exact_facts": facts_dict(item),
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
        "model": result.model,
        "request_id": result.request_id,
        "usage": dict(result.usage),
        "judgments": judgments,
    }
    cache_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


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
        if semantic:
            semantic_result = semantic_judgments(function, root=root, refresh=refresh)
            row["semantic"] = semantic_result
            row["semantic_findings"] = [
                rule
                for rule, probability in semantic_result["judgments"].items()
                if float(probability) >= threshold
            ]
        rows.append(row)
    return {
        "schema_version": 1,
        "target": str(target),
        "semantic": semantic,
        "threshold": threshold,
        "function_count": len(rows),
        "functions": rows,
    }
