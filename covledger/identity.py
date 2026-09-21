"""Deterministic identifiers for functions, findings, and coverage gaps."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import PurePosixPath


def normalize_path(path: str) -> str:
    """Return the canonical project-relative path used in identity keys."""
    value = PurePosixPath(path.replace("\\", "/")).as_posix()
    while value.startswith("./"):
        value = value[2:]
    return value


def function_key(path: str, qualname: str) -> str:
    return f"{normalize_path(path)}\0{qualname}"


def _short_id(prefix: str, key: str, length: int = 10) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def function_id(path: str, qualname: str) -> str:
    return _short_id("F", function_key(path, qualname))


def finding_key(
    path: str,
    qualname: str,
    origin: str,
    rule: str,
    discriminator: str,
) -> str:
    return "\0".join((function_key(path, qualname), origin, rule, discriminator))


def finding_id(
    path: str,
    qualname: str,
    origin: str,
    rule: str,
    discriminator: str,
) -> str:
    return _short_id("Q", finding_key(path, qualname, origin, rule, discriminator))


def gap_key(
    path: str,
    source_sha256: str,
    function: str | None,
    kind: str,
    from_line: int | None,
    to_line: int | None,
    anchor: int,
) -> str:
    return "\0".join(
        (
            normalize_path(path),
            source_sha256,
            function or "",
            kind,
            str(from_line if from_line is not None else ""),
            str(to_line if to_line is not None else ""),
            str(anchor),
        )
    )


def gap_id(
    path: str,
    source_sha256: str,
    function: str | None,
    kind: str,
    from_line: int | None,
    to_line: int | None,
    anchor: int,
) -> str:
    return _short_id("G", gap_key(path, source_sha256, function, kind, from_line, to_line, anchor))


def unique_ids(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Return stable IDs, extending digest length if a report collision occurs."""
    rows = list(items)
    result: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for prefix, key in rows:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        for length in (10, 12, 16, 20, 64):
            candidate = f"{prefix}-{digest[:length]}"
            previous = by_id.get(candidate)
            if previous is None or previous == key:
                by_id[candidate] = key
                result[key] = candidate
                break
        else:
            raise ValueError(f"identity collision for {prefix}: {key!r}")
    return result


__all__ = [
    "finding_id",
    "finding_key",
    "function_id",
    "function_key",
    "gap_id",
    "gap_key",
    "normalize_path",
    "unique_ids",
]
