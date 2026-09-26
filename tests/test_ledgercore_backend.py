import tomllib
from pathlib import Path

import pytest

from covledger.ledgercore_backend import CACHE_MOUNT, initialize_covledger

PROJECT_UUID = "0192f9bd-7e5e-7d5c-ae7c-2fcd90c4d3cb"


def _write_manifest(root: Path, covledger_registration: str) -> Path:
    manifest_path = root / ".ledger" / "ledger.toml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "schema_version = 3\n\n"
        "[project]\n"
        f'uuid = "{PROJECT_UUID}"\n'
        'name = "existing"\n\n'
        "[ledgers.taskledger.mounts.data]\n"
        'storage = "project"\n\n'
        "[ledgers.releaseledger.mounts.indexes]\n"
        'storage = "cache"\n\n'
        f"{covledger_registration}",
        encoding="utf-8",
    )
    return manifest_path


def test_init_registers_only_resolved_ledgercore_cache(tmp_path: Path) -> None:
    initialized = initialize_covledger(tmp_path)
    manifest = tomllib.loads((tmp_path / ".ledger" / "ledger.toml").read_text())
    mounts = manifest["ledgers"]["covledger"]["mounts"]
    cache_path = initialized.layout.mounts[CACHE_MOUNT].path

    assert mounts == {"cache": {"storage": "cache"}}
    assert cache_path.is_dir()
    assert (cache_path / ".ledger-project.toml").is_file()
    assert not (tmp_path / ".ledger" / "covledger" / "cache").exists()
    assert not (tmp_path.parent / "ledger").exists()
    assert initialized.legacy_runs_path is None

    config = tomllib.loads((tmp_path / ".ledger" / "covledger" / "config.toml").read_text())
    assert config["config_version"] == 2
    assert config["analysis"]["include"] == []


def test_init_is_idempotent_and_preserves_config_edits(tmp_path: Path) -> None:
    first = initialize_covledger(tmp_path)
    config_path = tmp_path / ".ledger" / "covledger" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[[decision]]\nsymbol = "pkg.module:wrapper"\naction = "ignore"\nreason = "presentation wrapper"\n',
        encoding="utf-8",
    )
    edited_config = config_path.read_text(encoding="utf-8")

    second = initialize_covledger(tmp_path)

    assert second.manifest.project_uuid == first.manifest.project_uuid
    assert second.layout.mounts[CACHE_MOUNT].path == first.layout.mounts[CACHE_MOUNT].path
    assert config_path.read_text(encoding="utf-8") == edited_config
    assert not (tmp_path / ".ledger" / "covledger" / "runs").exists()


def test_init_migrates_exact_legacy_registration_without_touching_external_runs(tmp_path: Path) -> None:
    root = tmp_path / "project"
    manifest_path = _write_manifest(
        root,
        "[ledgers.covledger.mounts.runs]\n"
        'storage = "external"\n'
        'root = "../ledger"\n\n'
        "[ledgers.covledger.mounts.cache]\n"
        'storage = "cache"\n',
    )
    legacy_runs = root.parent / "ledger" / "covledger" / PROJECT_UUID / "runs"
    sentinel = legacy_runs / "old-run" / "run.json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text('{"legacy": true}', encoding="utf-8")
    original_unrelated = tomllib.loads(manifest_path.read_text())["ledgers"]
    del original_unrelated["covledger"]

    initialized = initialize_covledger(root)
    migrated = tomllib.loads(manifest_path.read_text())
    mounts = migrated["ledgers"]["covledger"]["mounts"]
    unrelated = migrated["ledgers"].copy()
    del unrelated["covledger"]

    assert initialized.manifest.project_uuid == PROJECT_UUID
    assert initialized.legacy_runs_path == legacy_runs
    assert mounts == {"cache": {"storage": "cache"}}
    assert unrelated == original_unrelated
    assert sentinel.read_text(encoding="utf-8") == '{"legacy": true}'
    assert not (root.parent / "ledger" / ".ledger-store.toml").exists()


def test_init_rejects_unrecognized_registration_without_rewriting_it(tmp_path: Path) -> None:
    root = tmp_path / "project"
    manifest_path = _write_manifest(
        root,
        '[ledgers.covledger.mounts.runs]\nstorage = "project"\n\n[ledgers.covledger.mounts.cache]\nstorage = "cache"\n',
    )
    original = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="unexpected mount layout"):
        initialize_covledger(root)

    assert manifest_path.read_bytes() == original
    assert not (root / ".ledger" / "covledger").exists()


def test_init_upgrades_config_version_without_losing_user_content(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    config_path = tmp_path / ".ledger" / "covledger" / "config.toml"
    legacy_config = (
        'config_version = 1 # user config version\n\n[analysis]\ninclude = ["src"]\nexclude = ["generated"]\n'
        '\n[[decision]]\nsymbol = "pkg.mod:f"\naction = "ignore"\nreason = "intentional"\n'
    )
    config_path.write_text(legacy_config, encoding="utf-8")

    initialize_covledger(tmp_path)

    expected = legacy_config.replace("config_version = 1", "config_version = 2", 1)
    assert config_path.read_text(encoding="utf-8") == expected
    parsed = tomllib.loads(expected)
    assert parsed["config_version"] == 2
    assert parsed["analysis"]["include"] == ["src"]
    assert parsed["decision"][0]["reason"] == "intentional"
