"""Typed records used by CovLedger's assessment layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    function_id: str
    origin: Literal["exact", "semantic"]
    rule: str
    path: str
    line: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CoverageGap:
    id: str
    function_id: str | None
    kind: Literal["error-path", "branch", "line"]
    path: str
    line: int
    label: str
    from_line: int | None = None
    to_line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    hazard: int
    structural: int
    quality: int
    exposure: int
    regression: int | None
    bonuses: tuple[dict[str, Any], ...]
    priority: int
    semantic: int | None = None
    semantic_modifier: int | None = None
    augmented_priority: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Hotspot:
    id: str
    path: str
    qualname: str
    line: int
    end_line: int
    facts: dict[str, Any]
    finding_ids: tuple[str, ...]
    gap_ids: tuple[str, ...]
    score: ScoreBreakdown
    current_source: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["finding_ids"] = list(self.finding_ids)
        data["gap_ids"] = list(self.gap_ids)
        data["score"] = self.score.to_dict()
        return data


__all__ = ["CoverageGap", "Finding", "Hotspot", "ScoreBreakdown"]
