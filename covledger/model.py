"""Small data helpers shared by the CLI and evidence code."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CoverageFile:
    path: str
    source_sha256: str | None
    statements: int
    covered_lines: int
    missing_lines: tuple[int, ...]
    branches: int
    covered_branches: int
    missing_branches: tuple[tuple[int, int], ...]

    @property
    def line_percent(self) -> float:
        if self.statements == 0:
            return 100.0
        return 100.0 * self.covered_lines / self.statements

    @property
    def branch_percent(self) -> float:
        if self.branches == 0:
            return 100.0
        return 100.0 * self.covered_branches / self.branches

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["missing_lines"] = list(self.missing_lines)
        data["missing_branches"] = [list(branch) for branch in self.missing_branches]
        data["line_percent"] = self.line_percent
        data["branch_percent"] = self.branch_percent
        return data
