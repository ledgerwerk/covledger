"""CovLedger's narrow adapter around canonical Ledgercore storage."""

from __future__ import annotations

import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ledgercore import (
    atomic_write_text,
    initialize_config_binding,
    initialize_storage_binding,
    load_ledger_project,
    locate_ledger_project,
    resolve_ledger_layout,
    validate_ledger_layout_storage,
    validate_storage_binding,
    write_ledger_manifest,
)
from ledgercore.manifest import LedgerProjectManifest, LedgerRegistration, MountDefinition
from ledgercore.uuids import uuid7

TOOL_NAME = "covledger"
CACHE_MOUNT = "cache"
_LEGACY_RUNS_MOUNT = "runs"
_LEGACY_EXTERNAL_ROOT = "../ledger"

_DEFAULT_CONFIG = """config_version = 2

[ledger]
code = "cov"
name = "covledger"

[coverage]
branch = true

[quality]
semantic_threshold = 0.70

[analysis]
include_generated = false
include = []
exclude = [
  "context_*.unpack.py",
]

[priority]
high = 70
critical = 85
"""


@dataclass(frozen=True, slots=True)
class CovLedgerInitialization:
    manifest: LedgerProjectManifest
    layout: Any
    legacy_runs_path: Path | None = None


def locate_covledger_project(start: Path) -> Any:
    """Locate a canonical Ledgercore project without creating files."""
    return locate_ledger_project(start)


def _covledger_registration() -> LedgerRegistration:
    return LedgerRegistration(
        TOOL_NAME,
        {CACHE_MOUNT: MountDefinition(CACHE_MOUNT, "cache", None)},
    )


def _legacy_covledger_registration() -> LedgerRegistration:
    return LedgerRegistration(
        TOOL_NAME,
        {
            _LEGACY_RUNS_MOUNT: MountDefinition(
                _LEGACY_RUNS_MOUNT,
                "external",
                _LEGACY_EXTERNAL_ROOT,
            ),
            CACHE_MOUNT: MountDefinition(CACHE_MOUNT, "cache", None),
        },
    )


def build_covledger_manifest_with_registration(
    manifest: LedgerProjectManifest | None,
    *,
    project_uuid: str,
    project_name: str,
) -> LedgerProjectManifest:
    """Return a cache-only schema-3 manifest, preserving unrelated registrations.

    Only the exact historical ``runs=external:../ledger + cache=cache`` layout
    is migrated. Unknown layouts may contain user data and must not be discarded.
    """
    normalized_uuid = str(uuid.UUID(project_uuid))
    if manifest is not None and manifest.project_uuid != normalized_uuid:
        raise ValueError("project UUID conflicts with the existing Ledger manifest")

    registrations = dict(manifest.ledgers) if manifest is not None else {}
    existing = registrations.get(TOOL_NAME)
    cache_only = _covledger_registration()
    if existing is None:
        registrations[TOOL_NAME] = cache_only
    elif existing == cache_only:
        pass
    elif existing == _legacy_covledger_registration():
        registrations[TOOL_NAME] = cache_only
    else:
        raise ValueError(
            "cannot migrate CovLedger registration with an unexpected mount layout; "
            "expected only the cache mount or the recognized legacy runs + cache mounts"
        )

    name = project_name if manifest is None else (project_name or manifest.project_name)
    return LedgerProjectManifest(
        schema_version=3,
        project_uuid=normalized_uuid,
        project_name=name,
        ledgers=registrations,
    )


def _legacy_runs_path(project_root: Path) -> Path | None:
    """Resolve the old runs path through Ledgercore before upgrading the manifest."""
    locator = locate_covledger_project(project_root)
    if locator is None or locator.is_legacy or locator.project_root != project_root:
        return None
    loaded = load_ledger_project(project_root)
    if loaded.manifest.ledgers.get(TOOL_NAME) != _legacy_covledger_registration():
        return None
    layout = resolve_ledger_layout(
        loaded.locator,
        loaded.manifest,
        TOOL_NAME,
        local_overrides=loaded.local_overrides,
    )
    return layout.mounts[_LEGACY_RUNS_MOUNT].path


def ensure_covledger_ledger_registration(
    project_root: Path,
    *,
    project_name: str | None = None,
) -> LedgerProjectManifest:
    """Create, preserve, or safely upgrade the canonical project registration."""
    root = project_root.expanduser().resolve()
    locator = locate_covledger_project(root)
    if locator is not None and locator.is_legacy:
        raise ValueError(
            "CovLedger requires a canonical .ledger/ledger.toml project; run `covledger init` "
            "in a project without legacy ledger configuration"
        )

    manifest_path = root / ".ledger" / "ledger.toml"
    if locator is not None:
        if locator.project_root != root:
            raise ValueError(f"Ledger project root is {locator.project_root}, not {root}")
        current = load_ledger_project(root).manifest
        requested_name = project_name or current.project_name or root.name
        candidate = build_covledger_manifest_with_registration(
            current,
            project_uuid=current.project_uuid,
            project_name=requested_name,
        )
    else:
        candidate = build_covledger_manifest_with_registration(
            None,
            project_uuid=str(uuid7()),
            project_name=project_name or root.name,
        )

    if locator is None or candidate != current:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        write_ledger_manifest(manifest_path, candidate)
    return candidate


def load_covledger_ledger_layout(start: Path, *, validate_storage: bool = True) -> Any:
    """Load and optionally validate the canonical cache-only CovLedger layout."""
    root = start.expanduser().resolve()
    locator = locate_covledger_project(root)
    if locator is None or locator.is_legacy:
        legacy = root / ".covledger"
        suffix = " Legacy .covledger storage is unsupported." if legacy.exists() else ""
        raise ValueError(f"CovLedger is not initialized for this project. Run `covledger init`.{suffix}")
    if locator.project_root != root:
        raise ValueError(f"Ledger project root is {locator.project_root}, not {root}")

    loaded = load_ledger_project(root)
    registration = loaded.manifest.ledgers.get(TOOL_NAME)
    if registration is None:
        raise ValueError("CovLedger is not registered in .ledger/ledger.toml. Run `covledger init`.")
    if registration != _covledger_registration():
        if registration == _legacy_covledger_registration():
            raise ValueError("CovLedger has legacy runs storage; run `covledger init` to migrate to cache-only storage")
        raise ValueError("CovLedger registration has an unexpected mount layout; run `covledger init` to inspect it")

    try:
        layout = resolve_ledger_layout(
            loaded.locator,
            loaded.manifest,
            TOOL_NAME,
            local_overrides=loaded.local_overrides,
        )
    except Exception as exc:
        raise ValueError(f"unable to resolve CovLedger Ledgercore layout: {exc}") from exc

    try:
        cache_mount = layout.mounts[CACHE_MOUNT]
    except KeyError as exc:
        raise ValueError("CovLedger layout must define a cache mount") from exc
    if cache_mount.storage != "cache":
        raise ValueError("CovLedger cache mount must use Ledgercore cache storage")

    if validate_storage:
        report = validate_ledger_layout_storage(layout)
        invalid = [(result.path, result.reason) for result in report.results if not result.valid]
        if layout.config_binding_path is not None and not layout.config_binding_path.is_file():
            invalid.append((layout.config_binding_path, "configuration binding marker is missing"))
        result = validate_storage_binding(cache_mount, allow_missing=False)
        if not result.valid:
            invalid.append((result.path, result.reason))
        if invalid:
            reasons = "; ".join(f"{path}: {reason}" for path, reason in invalid)
            raise ValueError(f"invalid CovLedger Ledgercore storage binding: {reasons}")
    return layout


def initialize_covledger_bindings(layout: Any) -> None:
    """Initialize project config and the Ledgercore-owned cache binding."""
    initialize_config_binding(layout)
    mount = layout.mounts[CACHE_MOUNT]
    marker = mount.path / ".ledger-project.toml"
    initialize_storage_binding(mount, require_empty=not marker.exists())


def _migrate_config(config_path: Path) -> None:
    """Upgrade the config version without reserializing or losing user edits."""
    if not config_path.exists():
        atomic_write_text(config_path, _DEFAULT_CONFIG)
        return

    text = config_path.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid CovLedger config at {config_path}: {exc}") from exc
    version = parsed.get("config_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"invalid CovLedger config_version in {config_path}")
    if version == 2:
        return
    if version != 1:
        raise ValueError(f"unsupported CovLedger config_version {version} in {config_path}; expected 1 or 2")

    pattern = re.compile(r"(?m)^(?P<prefix>[ \t]*config_version[ \t]*=[ \t]*)1(?P<suffix>[ \t]*(?:#.*)?)$")
    match = pattern.search(text)
    if match:
        migrated = pattern.sub(f"{match.group('prefix')}2{match.group('suffix')}", text, count=1)
    else:
        migrated = f"config_version = 2\n{text}"
    atomic_write_text(config_path, migrated)


def initialize_covledger(
    project_root: Path,
    *,
    project_name: str | None = None,
) -> CovLedgerInitialization:
    """Create/reuse canonical project config and initialize its disposable cache."""
    root = project_root.expanduser().resolve()
    legacy_runs_path = _legacy_runs_path(root)
    manifest = ensure_covledger_ledger_registration(root, project_name=project_name)
    layout = load_covledger_ledger_layout(root, validate_storage=False)
    initialize_covledger_bindings(layout)
    config_path = layout.tool_config_path
    if config_path is None:
        raise ValueError("CovLedger layout has no project-local configuration path")
    _migrate_config(config_path)
    layout = load_covledger_ledger_layout(root)
    return CovLedgerInitialization(manifest, layout, legacy_runs_path)


__all__ = [
    "CACHE_MOUNT",
    "TOOL_NAME",
    "CovLedgerInitialization",
    "build_covledger_manifest_with_registration",
    "ensure_covledger_ledger_registration",
    "initialize_covledger",
    "initialize_covledger_bindings",
    "load_covledger_ledger_layout",
    "locate_covledger_project",
]
