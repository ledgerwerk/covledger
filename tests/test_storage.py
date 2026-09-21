import json
import tomllib
from pathlib import Path

import pytest
from ledgercore import parse_uuid7

from covledger.ledgercore_backend import initialize_covledger
from covledger.storage import (
    cache_root,
    list_run_ids,
    load_run,
    new_run_id,
    publish_run,
    resolve_run_id,
    runs_root,
)


def test_init_uses_external_runs_and_cache(tmp_path: Path) -> None:
    initialized = initialize_covledger(tmp_path)
    manifest = tomllib.loads((tmp_path / ".ledger" / "ledger.toml").read_text())
    mounts = manifest["ledgers"]["covledger"]["mounts"]

    assert mounts == {
        "runs": {"storage": "external", "root": "../ledger"},
        "cache": {"storage": "cache"},
    }
    expected_runs = tmp_path.parent / "ledger" / "covledger" / initialized.manifest.project_uuid / "runs"
    assert runs_root(tmp_path) == expected_runs
    assert runs_root(tmp_path).is_dir()
    assert cache_root(tmp_path).is_dir()
    assert not (tmp_path / ".covledger").exists()
    assert not (tmp_path / ".ledger" / "covledger" / "runs").exists()
    assert not (tmp_path / ".ledger" / "covledger" / "cache").exists()
    assert (tmp_path.parent / "ledger" / ".ledger-store.toml").is_file()
    assert (runs_root(tmp_path) / ".ledger-project.toml").is_file()
    assert (tmp_path / ".ledger" / "covledger" / ".ledger-project.toml").is_file()
    assert (tmp_path / ".ledger" / "covledger" / "config.toml").is_file()


def test_init_is_idempotent_and_preserves_run(tmp_path: Path) -> None:
    first = initialize_covledger(tmp_path)
    run_id = new_run_id()
    publish_run(runs_root(tmp_path) / run_id, {"run_id": run_id})

    second = initialize_covledger(tmp_path)

    assert second.manifest.project_uuid == first.manifest.project_uuid
    assert second.layout.mounts["runs"].path == first.layout.mounts["runs"].path
    assert list_run_ids(tmp_path) == [run_id]
    assert load_run(tmp_path, run_id)["run_id"] == run_id


def test_published_run_is_immutable_and_latest_resolves(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    first_id = new_run_id()
    second_id = new_run_id()
    publish_run(runs_root(tmp_path) / first_id, {"run_id": first_id})
    publish_run(runs_root(tmp_path) / second_id, {"run_id": second_id})

    assert load_run(tmp_path, "latest")["run_id"] == max(first_id, second_id)
    assert resolve_run_id(tmp_path, "latest") == max(first_id, second_id)
    with pytest.raises(FileExistsError):
        publish_run(runs_root(tmp_path) / first_id, {"run_id": "rewritten"})
    assert json.loads((runs_root(tmp_path) / first_id / "run.json").read_text())["run_id"] == first_id


def test_shared_external_store_isolated_by_project_uuid(tmp_path: Path) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    first = initialize_covledger(project_a)
    second = initialize_covledger(project_b)
    run_id = new_run_id()
    publish_run(runs_root(project_a) / run_id, {"run_id": run_id})

    assert first.manifest.project_uuid != second.manifest.project_uuid
    assert runs_root(project_a) != runs_root(project_b)
    assert list_run_ids(project_a) == [run_id]
    assert list_run_ids(project_b) == []


def test_missing_external_store_is_rejected_without_adoption(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    marker = tmp_path.parent / "ledger" / ".ledger-store.toml"
    marker.unlink()

    with pytest.raises(ValueError, match="initialize the external root explicitly"):
        runs_root(tmp_path)
    assert not marker.exists()


def test_no_covledger_fallback(tmp_path: Path) -> None:
    (tmp_path / ".covledger" / "runs" / "run_20260101T000000Z_aaaaaa").mkdir(parents=True)

    from covledger.storage import load_covledger_layout

    with pytest.raises(ValueError, match="covledger init"):
        load_covledger_layout(tmp_path)

def test_new_project_identity_is_uuidv7(tmp_path: Path) -> None:
    initialized = initialize_covledger(tmp_path)

    assert parse_uuid7(initialized.manifest.project_uuid) is not None


def test_init_preserves_unrelated_ledger_registration(tmp_path: Path) -> None:
    project_uuid = "0192f9bd-7e5e-7d5c-ae7c-2fcd90c4d3cb"
    manifest = (
        "schema_version = 3\n\n"
        "[project]\n"
        f'uuid = "{project_uuid}"\n'
        'name = "existing"\n\n'
        "[ledgers.taskledger.mounts.data]\n"
        'storage = "project"\n'
    )
    (tmp_path / ".ledger").mkdir()
    (tmp_path / ".ledger" / "ledger.toml").write_text(manifest, encoding="utf-8")

    initialize_covledger(tmp_path)
    result = tomllib.loads((tmp_path / ".ledger" / "ledger.toml").read_text())

    assert result["project"]["uuid"] == project_uuid
    assert result["project"]["name"] == "existing"
    assert result["ledgers"]["taskledger"] == {"mounts": {"data": {"storage": "project"}}}
