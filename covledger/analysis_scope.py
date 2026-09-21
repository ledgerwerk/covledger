"""Canonical repository analysis scope policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

_DEFAULT_EXCLUDE = ("context_*.unpack.py",)
_GLOB_CHARS = frozenset("*?[")


def normalize_scope_rule(value: str) -> str:
    """Normalize a configured scope rule to a repository-relative POSIX string."""
    if not isinstance(value, str):
        raise ValueError(f"scope rules must be strings, got {value!r}")
    rule = value.strip().replace("\\", "/")
    while rule.startswith("./"):
        rule = rule[2:]
    if not rule or rule == ".":
        raise ValueError("scope rules must not be empty")
    if rule.startswith("/") or PurePosixPath(rule).is_absolute() or (len(rule) >= 2 and rule[1] == ":"):
        raise ValueError("scope rules must be repository-relative")
    if any(component == ".." for component in rule.split("/")):
        raise ValueError("scope rules must not contain '..'")
    return rule


def matches_scope_rule(display_path: str, rule: str) -> bool:
    """Return whether a normalized repository-relative path matches a rule."""
    path = display_path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    normalized_rule = normalize_scope_rule(rule)
    has_glob = any(character in normalized_rule for character in _GLOB_CHARS)
    if not has_glob:
        return path == normalized_rule or path.startswith(f"{normalized_rule}/")
    if "/" not in normalized_rule:
        return fnmatchcase(PurePosixPath(path).name, normalized_rule)
    return fnmatchcase(path, normalized_rule)


@dataclass(frozen=True, slots=True)
class AnalysisScope:
    """Immutable policy deciding which eligible repository files are analyzed."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = _DEFAULT_EXCLUDE
    include_generated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "include": list(self.include),
            "exclude": list(self.exclude),
            "include_generated": self.include_generated,
        }

    def allows_path(self, display_path: str) -> bool:
        """Apply include rules first, then exclusions, against a relative POSIX path."""
        included = not self.include or any(matches_scope_rule(display_path, rule) for rule in self.include)
        excluded = any(matches_scope_rule(display_path, rule) for rule in self.exclude)
        return included and not excluded


def _rules_from_config(config: Mapping[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = config.get(key, list(default))
    if not isinstance(value, list):
        raise ValueError(f"invalid analysis.{key} value {value!r}: expected an array of strings")
    rules: list[str] = []
    for entry in value:
        try:
            rules.append(normalize_scope_rule(entry))
        except ValueError as exc:
            raise ValueError(f"invalid analysis.{key} entry {entry!r}: {exc}") from exc
    return tuple(rules)


def analysis_scope_from_config(config: Mapping[str, Any]) -> AnalysisScope:
    """Parse and validate the canonical analysis policy from a CovLedger config."""
    raw_analysis = config.get("analysis", {})
    if not isinstance(raw_analysis, Mapping):
        raise ValueError(f"invalid analysis value {raw_analysis!r}: expected a table")
    include = _rules_from_config(raw_analysis, "include", ())
    exclude = _rules_from_config(raw_analysis, "exclude", _DEFAULT_EXCLUDE)
    include_generated = raw_analysis.get("include_generated", False)
    if not isinstance(include_generated, bool):
        raise ValueError(f"invalid analysis.include_generated value {include_generated!r}: expected a boolean")
    return AnalysisScope(include=include, exclude=exclude, include_generated=include_generated)


__all__ = [
    "AnalysisScope",
    "analysis_scope_from_config",
    "matches_scope_rule",
    "normalize_scope_rule",
]
