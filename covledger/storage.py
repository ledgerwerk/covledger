"""Ledgercore-backed immutable CovLedger storage."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ledgercore import (
    atomic_create_text,
    dumps_json,
    ensure_inside_base,
    load_json_object,
    parse_uuid7,
    relative_to_base,
    uuid7,
)

from .ledgercore_backend import load_covledger_ledger_layout

RUNS_DIR = "runs"


class RunNotFound(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CovLedgerLayout:
    project_root: Path
    runs_path: Path
    cache_path: Path
    tool_config_path: Path
    ledgercore: Any | None = None


def load_covledger_layout(root: Path, *, for_write: bool = False) -> CovLedgerLayout:
    """Resolve and validate the canonical CovLedger Ledgercore layout."""
    del for_write
    layout = load_covledger_ledger_layout(root)
    return CovLedgerLayout(
        layout.project_root,
        layout.mounts["runs"].path,
        layout.mounts["cache"].path,
        layout.tool_config_path,
        layout,
    )


def load_covledger_config(root: Path) -> dict[str, Any]:
    import tomllib

    layout = load_covledger_layout(root)
    if not layout.tool_config_path.is_file():
        return {
            "config_version": 1,
            "ledger": {"code": "cov", "name": "covledger"},
            "coverage": {"branch": True},
            "quality": {"semantic_threshold": 0.70},
        }
    return tomllib.loads(layout.tool_config_path.read_text(encoding="utf-8"))


def new_run_id() -> str:
    return str(uuid7())


def validate_run_id(value: str) -> str:
    try:
        return str(parse_uuid7(value))
    except Exception as exc:
        raise ValueError(f"invalid CovLedger run id: {value}") from exc


def runs_root(root: Path) -> Path:
    return load_covledger_layout(root).runs_path


def cache_root(root: Path) -> Path:
    return load_covledger_layout(root).cache_path


def list_run_ids(root: Path) -> list[str]:
    directory = runs_root(root)
    result: list[str] = []
    for path in directory.iterdir():
        if not path.is_dir() or not (path / "run.json").is_file():
            continue
        try:
            run_id = validate_run_id(path.name)
            load_json_object(path / "run.json", label="run metadata")
        except (OSError, ValueError, TypeError):
            continue
        result.append(run_id)
    return sorted(set(result), reverse=True)


def resolve_run_id(root: Path, selector: str) -> str:
    if selector == "latest":
        runs = list_run_ids(root)
        if not runs:
            raise RunNotFound("no CovLedger runs found")
        return runs[0]
    run_id = validate_run_id(selector)
    path = runs_root(root) / run_id / "run.json"
    if not path.is_file():
        raise RunNotFound(f"run not found: {selector}")
    return run_id


def _load_run_artifacts(run_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    if "coverage" in metadata:
        return metadata
    result = dict(metadata)
    for name in ("scope", "coverage", "quality"):
        path = run_dir / f"{name}.json"
        if path.is_file():
            result[name] = load_json_object(path, label=f"{name} artifact")
    result.setdefault("suite_passed", result.get("suite", {}).get("passed", False))
    result.setdefault("exit_code", result.get("suite", {}).get("exit_code"))
    result.setdefault("command", result.get("suite", {}).get("command", []))
    return result


def load_run(root: Path, selector: str) -> dict[str, Any]:
    run_id = resolve_run_id(root, selector)
    run_dir = runs_root(root) / run_id
    metadata = load_json_object(run_dir / "run.json", label="run metadata")
    return _load_run_artifacts(run_dir, metadata)


def load_artifact(root: Path, selector: str, name: str) -> dict[str, Any]:
    run_id = resolve_run_id(root, selector)
    return load_json_object(runs_root(root) / run_id / f"{name}.json", label=f"{name} artifact")


def stage_run(root: Path, run_id: str | None = None) -> Path:
    layout = load_covledger_layout(root)
    canonical_id = validate_run_id(run_id or new_run_id())
    while True:
        stage = layout.runs_path / f".tmp-{canonical_id}-{secrets.token_hex(6)}"
        try:
            stage.mkdir(parents=True, exist_ok=False)
            return stage
        except FileExistsError:
            continue


def publish_staged_run(stage_dir: Path, run_id: str) -> Path:
    run_id = validate_run_id(run_id)
    target = stage_dir.parent / run_id
    if target.exists():
        raise FileExistsError(f"refusing to rewrite published run: {target}")
    os.replace(stage_dir, target)
    return target


def publish_artifact(directory: Path, name: str, payload: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    atomic_create_text(directory / f"{name}.json", dumps_json(payload))


def publish_run(run_dir: Path, report: dict[str, Any]) -> None:
    """Compatibility helper for direct publication."""
    target = run_dir / "run.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"refusing to rewrite published run: {target}")
    if run_dir.parent.name == RUNS_DIR:
        validate_run_id(run_dir.name)
    if "coverage" in report and "scope" in report:
        metadata = dict(report)
        coverage = metadata.pop("coverage")
        scope = metadata.pop("scope")
        quality = metadata.pop("quality", {"schema_version": 2, "files": {}})
        metadata.setdefault("schema_version", 2)
        publish_artifact(run_dir, "scope", scope)
        publish_artifact(run_dir, "coverage", coverage)
        publish_artifact(run_dir, "quality", quality)
        atomic_create_text(target, dumps_json(metadata))
        return
    atomic_create_text(target, dumps_json(report))


def semantic_cache_path(root: Path, key: str) -> Path:
    if "/" in key or "\\" in key or key != Path(key).name:
        raise ValueError("invalid semantic cache key")
    return cache_root(root) / "jev" / f"{key}.json"


def source_relative(root: Path, path: Path) -> str:
    return relative_to_base(root.resolve(), ensure_inside_base(root.resolve(), path, field_name="source path"))


def resolve_source(root: Path, relative_path: str) -> Path:
    base = root.resolve()
    candidate = (base / relative_path).resolve()
    return ensure_inside_base(base, candidate, field_name="source path")


def ensure_artifact_path(base: Path, relative_path: str) -> Path:
    return ensure_inside_base(base.resolve(), (base / relative_path).resolve(), field_name="artifact path")


__all__ = [
    "CovLedgerLayout",
    "RunNotFound",
    "cache_root",
    "ensure_artifact_path",
    "list_run_ids",
    "load_artifact",
    "load_covledger_config",
    "load_covledger_layout",
    "load_run",
    "new_run_id",
    "publish_artifact",
    "publish_run",
    "publish_staged_run",
    "resolve_run_id",
    "resolve_source",
    "runs_root",
    "semantic_cache_path",
    "source_relative",
    "stage_run",
    "validate_run_id",
]
