"""Validation and matching for explicit, durable user prioritization decisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


@dataclass(frozen=True, slots=True)
class UserDecision:
    symbol: str
    action: str
    reason: str
    rule: str | None = None


def decisions_from_config(config: dict[str, Any]) -> tuple[UserDecision, ...]:
    """Parse the deliberately small stable-symbol decision schema."""
    raw_decisions = config.get("decision", [])
    if not isinstance(raw_decisions, list):
        raise ValueError("invalid CovLedger decision config: expected an array of tables")
    decisions: list[UserDecision] = []
    seen: set[tuple[str, str, str | None]] = set()
    for index, raw in enumerate(raw_decisions):
        if not isinstance(raw, dict):
            raise ValueError(f"invalid CovLedger decision[{index}]: expected a table")
        unknown = set(raw) - {"symbol", "action", "reason", "rule"}
        if unknown:
            raise ValueError(f"invalid CovLedger decision[{index}]: unknown fields {sorted(unknown)}")
        symbol = raw.get("symbol")
        action = raw.get("action")
        reason = raw.get("reason")
        rule = raw.get("rule")
        if not isinstance(symbol, str) or symbol.count(":") != 1:
            raise ValueError(f"invalid CovLedger decision[{index}].symbol: expected path:qualified-name")
        path, qualname = symbol.split(":", 1)
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(character in path for character in "*?[")
            or PurePosixPath(path).as_posix() != path
            or not qualname
            or qualname.strip() != qualname
        ):
            raise ValueError(f"invalid CovLedger decision[{index}].symbol: expected a stable relative symbol")
        if not isinstance(action, str) or action not in {"ignore", "ignore-finding"}:
            raise ValueError(f"invalid CovLedger decision[{index}].action: expected ignore or ignore-finding")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"invalid CovLedger decision[{index}].reason: expected a non-empty explanation")
        if action == "ignore-finding":
            if not isinstance(rule, str) or not rule.strip():
                raise ValueError(f"invalid CovLedger decision[{index}].rule: required for ignore-finding")
            rule = rule.strip()
        elif rule is not None:
            raise ValueError(f"invalid CovLedger decision[{index}]: rule is only valid for ignore-finding")
        identity = (symbol, action, rule)
        if identity in seen:
            raise ValueError(f"duplicate CovLedger decision for {symbol!r} ({action}, {rule!r})")
        seen.add(identity)
        decisions.append(UserDecision(symbol=symbol, action=action, reason=reason.strip(), rule=rule))
    return tuple(decisions)


__all__ = ["UserDecision", "decisions_from_config"]
