"""Disposable CovLedger state in Ledgercore's checkout-scoped cache."""

from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from pathlib import Path
from typing import Any

from ledgercore import (
    atomic_write_text,
    dumps_json,
    ensure_inside_base,
    load_json_object,
    parse_uuid7,
    relative_to_base,
    uuid7,
)

from .ledgercore_backend import CACHE_MOUNT, load_covledger_ledger_layout

CURRENT_ANALYSIS_FILENAME = "current.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": 2,
    "ledger": {"code": "cov", "name": "covledger"},
    "coverage": {"branch": True},
    "quality": {"semantic_threshold": 0.70},
    "analysis": {
        "include_generated": False,
        "include": [],
        "exclude": ["context_*.unpack.py"],
    },
    "priority": {"high": 70, "critical": 85},
    "decision": [],
}

_ANALYSIS_CONFIG_DEFAULTS: dict[str, Any] = {
    "coverage": {"branch": True},
    "quality": {"semantic_threshold": 0.70},
    "analysis": {
        "include_generated": False,
        "include": [],
        "exclude": ["context_*.unpack.py"],
    },
    "priority": {"high": 70, "critical": 85},
    "decision": [],
}


def _layout(root: Path) -> Any:
    return load_covledger_ledger_layout(root)


def load_covledger_config(root: Path) -> dict[str, Any]:
    """Load durable project policy, using defaults only if config is absent."""
    config_path = _layout(root).tool_config_path
    if config_path is None or not config_path.is_file():
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid CovLedger config at {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"invalid CovLedger config at {config_path}: expected a TOML table")
    return config


def cache_root(root: Path) -> Path:
    """Return the cache path resolved by Ledgercore for this checkout."""
    return _layout(root).mounts[CACHE_MOUNT].path


def current_analysis_path(root: Path) -> Path:
    """Return the one replaceable cached analysis path."""
    return cache_root(root) / CURRENT_ANALYSIS_FILENAME


def load_current_analysis(root: Path) -> dict[str, Any]:
    """Load the current compact analysis or explain how to create one."""
    path = current_analysis_path(root)
    if path.is_symlink():
        raise ValueError(f"refusing to read symlinked current analysis: {path}")
    if not path.is_file():
        raise FileNotFoundError("no current CovLedger analysis; run `covledger run -- pytest ...`")
    payload = load_json_object(path, label="current CovLedger analysis")
    if payload.get("schema_version") != 1:
        raise ValueError(f"unsupported current CovLedger analysis schema in {path}")
    return payload


class StaleAnalysisError(ValueError):
    """Raised when cached analysis no longer describes the current checkout."""

    def __init__(self, changed: list[str]) -> None:
        self.changed = tuple(changed)
        details = ", ".join(changed)
        super().__init__(f"current CovLedger analysis is stale ({details}); rerun with `covledger run -- pytest ...`")


def validate_current_source_state(root: Path, current: dict[str, Any]) -> None:
    """Require every cached source hash and the analysis-affecting config to match."""
    scope = current.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("invalid current CovLedger analysis: missing scope metadata")
    files = scope.get("files")
    if not isinstance(files, dict):
        raise ValueError("invalid current CovLedger analysis: invalid source hash map")
    expected_config = scope.get("config_sha256")
    if not isinstance(expected_config, str) or not expected_config:
        raise ValueError("invalid current CovLedger analysis: missing config fingerprint")

    changed: list[str] = []
    for relative_path, metadata in sorted(files.items()):
        if not isinstance(relative_path, str) or not isinstance(metadata, dict):
            raise ValueError("invalid current CovLedger analysis: malformed source hash entry")
        expected_hash = metadata.get("sha256")
        if not isinstance(expected_hash, str) or not expected_hash:
            raise ValueError(f"invalid current CovLedger analysis: missing source hash for {relative_path}")
        try:
            source_path = resolve_source(root, relative_path)
            if not source_path.is_file() or hashlib.sha256(source_path.read_bytes()).hexdigest() != expected_hash:
                changed.append(relative_path)
        except Exception:
            changed.append(relative_path)
    if analysis_config_sha256(root) != expected_config:
        changed.append("CovLedger configuration")
    if changed:
        raise StaleAnalysisError(changed)


def load_fresh_current_analysis(root: Path) -> dict[str, Any]:
    """Load current.json and reject changed source or policy before use."""
    current = load_current_analysis(root)
    validate_current_source_state(root, current)
    return current


def clear_current_analysis(root: Path) -> None:
    """Remove only current.json, leaving semantic cache and ownership metadata intact."""
    path = current_analysis_path(root)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise ValueError(f"refusing to remove unexpected current analysis path: {path}")


def create_work_dir(root: Path, analysis_id: str | None = None) -> Path:
    """Create a private per-analysis work directory under the resolved cache mount."""
    canonical_id = str(uuid7()) if analysis_id is None else str(analysis_id)
    if "/" in canonical_id or "\\" in canonical_id or Path(canonical_id).name != canonical_id:
        raise ValueError(f"invalid CovLedger analysis id: {canonical_id}")
    try:
        canonical_id = str(parse_uuid7(canonical_id))
    except Exception as exc:
        raise ValueError(f"invalid CovLedger analysis id: {canonical_id}") from exc
    cache = cache_root(root)
    cache_resolved = cache.resolve()
    work_root = cache / "work"
    if work_root.is_symlink():
        raise ValueError(f"refusing to use symlinked CovLedger work directory: {work_root}")
    work_root.mkdir(parents=True, exist_ok=True)
    try:
        work_root.resolve().relative_to(cache_resolved)
    except ValueError as exc:
        raise ValueError(f"CovLedger work directory escapes the cache mount: {work_root}") from exc
    work_dir = work_root / canonical_id
    work_dir.mkdir(parents=False, exist_ok=False)
    return work_dir


def publish_current_analysis(root: Path, payload: dict[str, Any]) -> Path:
    """Atomically replace current.json after the complete analysis is available."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("current CovLedger analysis payload must use schema_version 1")
    path = current_analysis_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, dumps_json(payload))
    return path


def _analysis_config(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section, defaults in _ANALYSIS_CONFIG_DEFAULTS.items():
        raw = config.get(section, defaults)
        if section == "decision":
            if not isinstance(raw, list):
                raise ValueError("invalid CovLedger decision config: expected an array of tables")
            result[section] = raw
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"invalid CovLedger {section} config: expected a table")
        result[section] = {**defaults, **raw}
    return result


def analysis_config_sha256(root: Path) -> str:
    """Hash canonical analysis-affecting policy, excluding TOML formatting/comments."""
    config = load_covledger_config(root)
    canonical = json.dumps(
        _analysis_config(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def semantic_cache_path(root: Path, key: str) -> Path:
    """Return a content-addressed PyJev entry under Ledgercore's cache mount."""
    if not key or "/" in key or "\\" in key or key in {".", ".."} or Path(key).name != key:
        raise ValueError("invalid semantic cache key")
    return cache_root(root) / "jev" / f"{key}.json"


def _remove_owned_child(cache: Path, name: str) -> None:
    """Remove one named CovLedger-owned cache child without following its symlink."""
    child = cache / name
    if child.parent != cache:
        raise ValueError(f"invalid CovLedger cache child: {child}")
    if child.is_symlink():
        child.unlink()
    elif child.is_dir():
        if name == CURRENT_ANALYSIS_FILENAME:
            raise ValueError(f"refusing to remove unexpected current analysis directory: {child}")
        shutil.rmtree(child)
    elif child.exists():
        child.unlink()


def clear_cache(root: Path) -> None:
    """Clear CovLedger's disposable cache children and validate Ledgercore ownership."""
    layout = _layout(root)
    cache = layout.mounts[CACHE_MOUNT].path
    for name in (CURRENT_ANALYSIS_FILENAME, "jev", "work"):
        _remove_owned_child(cache, name)
    # Reloading validates that the Ledgercore-owned marker survived the deletion.
    load_covledger_ledger_layout(root)


def source_relative(root: Path, path: Path) -> str:
    """Return a project-relative POSIX path after enforcing project containment."""
    base = root.resolve()
    return relative_to_base(base, ensure_inside_base(base, path, field_name="source path"))


def resolve_source(root: Path, relative_path: str) -> Path:
    """Resolve a cached relative source path inside the current project checkout."""
    base = root.resolve()
    candidate = (base / relative_path).resolve()
    return ensure_inside_base(base, candidate, field_name="source path")


__all__ = [
    "CURRENT_ANALYSIS_FILENAME",
    "StaleAnalysisError",
    "analysis_config_sha256",
    "cache_root",
    "clear_cache",
    "clear_current_analysis",
    "create_work_dir",
    "current_analysis_path",
    "load_covledger_config",
    "load_current_analysis",
    "load_fresh_current_analysis",
    "publish_current_analysis",
    "resolve_source",
    "semantic_cache_path",
    "source_relative",
    "validate_current_source_state",
]
