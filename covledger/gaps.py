"""Source-aware coverage gap candidates for a current analysis."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .identity import gap_id
from .source import SourceRegion, index_regions
from .storage import resolve_source


@dataclass(frozen=True, slots=True)
class GapCandidate:
    kind: str
    path: str
    line: int
    label: str
    function: dict[str, Any] | None = None
    from_line: int | None = None
    to_line: int | None = None
    id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        gap: dict[str, Any] = {"kind": self.kind, "label": self.label}
        if self.from_line is not None:
            gap["from_line"] = self.from_line
        if self.to_line is not None:
            gap["to_line"] = self.to_line
        result: dict[str, Any] = {"path": self.path, "line": self.line, "gap": gap}
        if self.id is not None:
            result["gap_id"] = self.id
        if self.function is not None:
            result["function"] = self.function
        return result


def _function_for(functions: list[Any], line: int) -> dict[str, Any] | None:
    matches = [item for item in functions if item.line <= line <= item.end_line]
    if not matches:
        return None
    item = min(matches, key=lambda value: (value.end_line - value.line, value.line, value.qualname))
    return {"qualname": item.qualname, "line": item.line, "end_line": item.end_line}


def _regions_for_line(regions: list[SourceRegion], line: int, kinds: set[str]) -> list[SourceRegion]:
    return [region for region in regions if region.kind in kinds and region.line <= line <= region.end_line]


def derive_gaps(path: str, coverage_file: dict[str, Any], source: str) -> list[GapCandidate]:
    regions = index_regions(source)
    functions = extract_functions_from_source(source, path)
    missing_lines = {int(line) for line in coverage_file.get("missing_lines", [])}
    candidates: list[GapCandidate] = []
    explained_lines: set[int] = set()

    for handler in [region for region in regions if region.kind == "except"]:
        inside = sorted(line for line in missing_lines if handler.line <= line <= handler.end_line)
        if not inside:
            continue
        anchor = handler.line if handler.line in missing_lines else inside[0]
        candidates.append(
            GapCandidate(
                kind="error-path",
                path=path,
                line=anchor,
                label=handler.label,
                function=_function_for(functions, anchor),
            )
        )
        explained_lines.update(inside)

    decision_kinds = {"if", "for", "async-for", "while", "match"}
    for raw_branch in coverage_file.get("missing_branches", []):
        from_line, to_line = (int(raw_branch[0]), int(raw_branch[1]))
        enclosing = _regions_for_line(regions, from_line, decision_kinds)
        decision = min(enclosing, key=lambda item: (item.end_line - item.line, item.line)) if enclosing else None
        line = decision.line if decision else from_line
        label = f"uncovered branch from {decision.kind}" if decision else "uncovered branch"
        candidates.append(
            GapCandidate(
                kind="branch",
                path=path,
                line=line,
                label=label,
                function=_function_for(functions, line),
                from_line=from_line,
                to_line=to_line,
            )
        )

    for line in sorted(missing_lines - explained_lines):
        candidates.append(
            GapCandidate(
                kind="line",
                path=path,
                line=line,
                label="uncovered line",
                function=_function_for(functions, line),
            )
        )
    ordered = sorted(
        candidates,
        key=lambda item: (item.line, {"error-path": 0, "branch": 1, "line": 2}[item.kind], item.kind),
    )
    source_hash = str(coverage_file.get("source_sha256", ""))
    return [
        replace(
            item,
            id=gap_id(
                path,
                source_hash,
                item.function.get("qualname") if item.function else None,
                item.kind,
                item.from_line,
                item.to_line,
                item.line,
            ),
        )
        for item in ordered
    ]


def extract_functions_from_source(source: str, path: str) -> list[Any]:
    import ast

    from .source import _FunctionCollector

    collector = _FunctionCollector(source, path)
    tree = ast.parse(source, filename=path)
    for node in tree.body:
        collector.visit(node)
    return collector.functions


def gaps_for_analysis(analysis: dict[str, Any], project_root: Path) -> list[GapCandidate]:
    """Derive gaps from live checkout files whose hashes match the analysis."""
    coverage = analysis.get("coverage", {})
    if coverage.get("status") != "available":
        return []
    scope_files = analysis.get("scope", {}).get("files", {})
    result: list[GapCandidate] = []
    for path, item in sorted(coverage.get("files", {}).items()):
        try:
            source_path = resolve_source(project_root, path)
            source_bytes = source_path.read_bytes()
            source = source_bytes.decode("utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read current source for cached analysis: {path}") from exc
        expected = scope_files.get(path, {}).get("sha256") or item.get("source_sha256")
        actual = hashlib.sha256(source_bytes).hexdigest()
        if expected and actual != expected:
            raise ValueError(f"source changed while building current CovLedger analysis: {path}")
        result.extend(derive_gaps(path, item, source))
    return result


__all__ = ["GapCandidate", "derive_gaps", "gaps_for_analysis"]
