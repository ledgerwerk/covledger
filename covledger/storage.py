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
    initialize_config_binding,
    initialize_storage_binding,
    load_json_object,
    load_ledger_project,
    parse_uuid7,
    relative_to_base,
    resolve_ledger_layout,
    uuid7,
    validate_ledger_layout_storage,
)

STATE_DIR = ".covledger"
RUNS_DIR = "runs"
CACHE_DIR = "cache"


class RunNotFound(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CovLedgerLayout:
    project_root: Path
    runs_path: Path
    cache_path: Path
    tool_config_path: Path
    ledgercore: Any | None = None
    legacy: bool = False


def _legacy_layout(root: Path) -> CovLedgerLayout:
    state = root / STATE_DIR
    return CovLedgerLayout(root, state / RUNS_DIR, state / CACHE_DIR, state / "config.toml", legacy=True)


def load_covledger_layout(root: Path, *, for_write: bool = False) -> CovLedgerLayout:
    """Resolve CovLedger through the Ledgercore project manifest."""
    root = root.resolve()
    manifest = root / ".ledger" / "ledger.toml"
    if not manifest.is_file():
        return _legacy_layout(root)
    project = load_ledger_project(root)
    try:
        layout = resolve_ledger_layout(
            project.locator,
            project.manifest,
            "covledger",
            local_overrides=project.local_overrides,
        )
    except Exception as exc:
        raise ValueError(f"unable to resolve CovLedger Ledgercore layout: {exc}") from exc
    try:
        runs = layout.mounts["runs"].path
        cache = layout.mounts["cache"].path
    except KeyError as exc:
        raise ValueError("CovLedger layout must define runs and cache mounts") from exc
    result = CovLedgerLayout(root, runs, cache, layout.tool_config_path, layout)
    if for_write:
        _prepare_layout(result)
    else:
        try:
            validate_ledger_layout_storage(layout)
        except Exception as exc:
            if runs.exists() or cache.exists() or layout.tool_config_path.parent.exists():
                raise ValueError(f"invalid CovLedger Ledgercore storage binding: {exc}") from exc
    return result


def _prepare_layout(layout: CovLedgerLayout) -> None:
    if layout.legacy:
        layout.runs_path.mkdir(parents=True, exist_ok=True)
        layout.cache_path.mkdir(parents=True, exist_ok=True)
        return
    core = layout.ledgercore
    assert core is not None
    layout.tool_config_path.parent.mkdir(parents=True, exist_ok=True)
    if not core.config_binding_path.exists():
        initialize_config_binding(core)
    for mount_name in ("runs", "cache"):
        mount = core.mounts[mount_name]
        if not mount.binding_path.exists():
            if mount.path.exists() and any(mount.path.iterdir()):
                raise ValueError(f"refusing to adopt non-empty unbound mount: {mount.path}")
            initialize_storage_binding(mount, require_empty=True)
    layout.runs_path.mkdir(parents=True, exist_ok=True)
    layout.cache_path.mkdir(parents=True, exist_ok=True)

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
    layout = load_covledger_layout(root)
    directory = layout.runs_path
    if not directory.exists():
        return []
    result: list[str] = []
    for path in directory.iterdir():
        if not path.is_dir() or not (path / "run.json").is_file():
            continue
        try:
            run_id = path.name if layout.legacy else validate_run_id(path.name)
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
    layout = load_covledger_layout(root)
    run_id = selector if layout.legacy else validate_run_id(selector)
    path = layout.runs_path / run_id / "run.json"
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
    layout = load_covledger_layout(root, for_write=True)
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
    runs_dir = stage_dir.parent
    target = runs_dir / run_id
    if target.exists():
        raise FileExistsError(f"refusing to rewrite published run: {target}")
    os.replace(stage_dir, target)
    return target


def publish_artifact(directory: Path, name: str, payload: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    atomic_create_text(directory / f"{name}.json", dumps_json(payload))


def publish_run(run_dir: Path, report: dict[str, Any]) -> None:
    """Compatibility helper for direct publication and legacy tests."""
    target = run_dir / "run.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"refusing to rewrite published run: {target}")
    if run_dir.parent.name == RUNS_DIR and not run_dir.name.startswith("run_"):
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
