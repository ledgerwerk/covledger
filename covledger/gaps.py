"""Source-aware coverage gap candidates shared by historical queries and next."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .source import SourceRegion, index_regions


@dataclass(frozen=True, slots=True)
class GapCandidate:
    kind: str
    path: str
    line: int
    label: str
    function: dict[str, Any] | None = None
    from_line: int | None = None
    to_line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        gap: dict[str, Any] = {"kind": self.kind, "label": self.label}
        if self.from_line is not None:
            gap["from_line"] = self.from_line
        if self.to_line is not None:
            gap["to_line"] = self.to_line
        result: dict[str, Any] = {"path": self.path, "line": self.line, "gap": gap}
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
    return sorted(
        candidates,
        key=lambda item: (item.line, {"error-path": 0, "branch": 1, "line": 2}[item.kind], item.kind),
    )


def extract_functions_from_source(source: str, path: str) -> list[Any]:
    import ast

    from .source import _FunctionCollector

    collector = _FunctionCollector(source, path)
    tree = ast.parse(source, filename=path)
    for node in tree.body:
        collector.visit(node)
    return collector.functions


def gaps_for_file(path: str, coverage_file: dict[str, Any], snapshot: Path) -> list[GapCandidate]:
    return derive_gaps(path, coverage_file, snapshot.read_text(encoding="utf-8", errors="replace"))


def gaps_for_run(run: dict[str, Any], run_dir: Path) -> list[GapCandidate]:
    coverage = run.get("coverage", {})
    if coverage.get("status") != "available":
        return []
    result: list[GapCandidate] = []
    for path, item in sorted(coverage.get("files", {}).items()):
        snapshot = run_dir / "sources" / path
        if snapshot.is_file():
            result.extend(gaps_for_file(path, item, snapshot))
    return result


__all__ = ["GapCandidate", "derive_gaps", "gaps_for_file", "gaps_for_run"]
