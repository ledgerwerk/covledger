"""CovLedger's narrow adapter around canonical Ledgercore storage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ledgercore import (
    atomic_write_text,
    initialize_config_binding,
    initialize_external_store,
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
RUNS_MOUNT = "runs"
CACHE_MOUNT = "cache"
DEFAULT_EXTERNAL_ROOT = "../ledger"

_DEFAULT_CONFIG = """config_version = 1

[ledger]
code = \"cov\"
name = \"covledger\"

[coverage]
branch = true

[quality]
semantic_threshold = 0.70
"""


@dataclass(frozen=True, slots=True)
class CovLedgerInitialization:
    manifest: LedgerProjectManifest
    layout: Any


def locate_covledger_project(start: Path) -> Any:
    """Locate a canonical Ledgercore project without creating files."""
    return locate_ledger_project(start)


def _covledger_registration(
    *, runs_storage: str, external_root: str | None
) -> LedgerRegistration:
    if runs_storage not in {"external", "user-data", "project"}:
        raise ValueError(f"unsupported CovLedger runs storage {runs_storage!r}")
    return LedgerRegistration(
        TOOL_NAME,
        {
            RUNS_MOUNT: MountDefinition(
                RUNS_MOUNT,
                runs_storage,
                external_root if runs_storage == "external" else None,
            ),
            CACHE_MOUNT: MountDefinition(CACHE_MOUNT, "cache", None),
        },
    )


def build_covledger_manifest_with_registration(
    manifest: LedgerProjectManifest | None,
    *,
    project_uuid: str,
    project_name: str,
    runs_storage: str = "external",
    external_root: str | None = DEFAULT_EXTERNAL_ROOT,
) -> LedgerProjectManifest:
    """Return a schema-3 manifest while preserving unrelated registrations."""
    normalized_uuid = str(uuid.UUID(project_uuid))
    if manifest is not None and manifest.project_uuid != normalized_uuid:
        raise ValueError("project UUID conflicts with the existing Ledger manifest")

    registrations = dict(manifest.ledgers) if manifest is not None else {}
    if TOOL_NAME not in registrations:
        registrations[TOOL_NAME] = _covledger_registration(
            runs_storage=runs_storage,
            external_root=external_root,
        )

    name = project_name if manifest is None else (project_name or manifest.project_name)
    return LedgerProjectManifest(
        schema_version=3,
        project_uuid=normalized_uuid,
        project_name=name,
        ledgers=registrations,
    )


def ensure_covledger_ledger_registration(
    project_root: Path,
    *,
    project_name: str | None = None,
    runs_storage: str = "external",
    external_root: str = DEFAULT_EXTERNAL_ROOT,
) -> LedgerProjectManifest:
    """Create or augment the canonical project manifest for CovLedger."""
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
        loaded = load_ledger_project(root)
        current = loaded.manifest
        requested_name = project_name or current.project_name or root.name
        candidate = build_covledger_manifest_with_registration(
            current,
            project_uuid=current.project_uuid,
            project_name=requested_name,
            runs_storage=runs_storage,
            external_root=external_root,
        )
    else:
        candidate = build_covledger_manifest_with_registration(
            None,
            project_uuid=str(uuid7()),
            project_name=project_name or root.name,
            runs_storage=runs_storage,
            external_root=external_root,
        )

    if locator is None or candidate != current:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        write_ledger_manifest(manifest_path, candidate)
    return candidate


def load_covledger_ledger_layout(
    start: Path, *, validate_storage: bool = True
) -> Any:
    """Load and optionally validate the canonical CovLedger layout."""
    root = start.expanduser().resolve()
    locator = locate_covledger_project(root)
    if locator is None or locator.is_legacy:
        legacy = root / ".covledger"
        suffix = " Legacy .covledger storage is unsupported." if legacy.exists() else ""
        raise ValueError(
            f"CovLedger is not initialized for this project. Run `covledger init`.{suffix}"
        )
    if locator.project_root != root:
        raise ValueError(f"Ledger project root is {locator.project_root}, not {root}")

    loaded = load_ledger_project(root)
    if TOOL_NAME not in loaded.manifest.ledgers:
        raise ValueError(
            "CovLedger is not registered in .ledger/ledger.toml. Run `covledger init`."
        )
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
        layout.mounts[RUNS_MOUNT]
        layout.mounts[CACHE_MOUNT]
    except KeyError as exc:
        raise ValueError("CovLedger layout must define runs and cache mounts") from exc

    if validate_storage:
        report = validate_ledger_layout_storage(layout)
        invalid = [
            (result.path, result.reason)
            for result in report.results
            if not result.valid
        ]
        if layout.config_binding_path is not None and not layout.config_binding_path.is_file():
            invalid.append((layout.config_binding_path, "configuration binding marker is missing"))
        for mount in layout.mounts.values():
            result = validate_storage_binding(mount, allow_missing=False)
            if not result.valid:
                invalid.append((result.path, result.reason))
        if invalid:
            reasons = "; ".join(f"{path}: {reason}" for path, reason in invalid)
            raise ValueError(f"invalid CovLedger Ledgercore storage binding: {reasons}")
    return layout


def initialize_covledger_external_store(layout: Any) -> bool:
    """Initialize an external store only during explicit CovLedger init."""
    mount = layout.mounts[RUNS_MOUNT]
    if mount.storage != "external" or mount.root is None:
        return False
    existed = mount.root.exists()
    initialize_external_store(mount.root)
    return not existed


def initialize_covledger_bindings(layout: Any) -> None:
    """Initialize config and mount bindings through Ledgercore APIs."""
    initialize_config_binding(layout)
    for mount in layout.mounts.values():
        marker = mount.path / ".ledger-project.toml"
        initialize_storage_binding(mount, require_empty=not marker.exists())


def initialize_covledger(
    project_root: Path,
    *,
    project_name: str | None = None,
    runs_storage: str = "external",
    external_root: str = DEFAULT_EXTERNAL_ROOT,
) -> CovLedgerInitialization:
    """Create or reuse canonical CovLedger storage and return its validated layout."""
    manifest = ensure_covledger_ledger_registration(
        project_root,
        project_name=project_name,
        runs_storage=runs_storage,
        external_root=external_root,
    )
    layout = load_covledger_ledger_layout(project_root, validate_storage=False)
    initialize_covledger_external_store(layout)
    initialize_covledger_bindings(layout)
    config_path = layout.tool_config_path
    if config_path is None:
        raise ValueError("CovLedger layout has no project-local configuration path")
    if not config_path.exists():
        atomic_write_text(config_path, _DEFAULT_CONFIG)
    layout = load_covledger_ledger_layout(project_root)
    return CovLedgerInitialization(manifest, layout)


__all__ = [
    "CACHE_MOUNT",
    "DEFAULT_EXTERNAL_ROOT",
    "RUNS_MOUNT",
    "TOOL_NAME",
    "CovLedgerInitialization",
    "build_covledger_manifest_with_registration",
    "ensure_covledger_ledger_registration",
    "initialize_covledger",
    "initialize_covledger_bindings",
    "initialize_covledger_external_store",
    "load_covledger_ledger_layout",
    "locate_covledger_project",
]
