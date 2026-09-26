import tomllib
from pathlib import Path

import pytest
from ledgercore import uuid7, validate_storage_binding

from covledger.ledgercore_backend import CACHE_MOUNT, initialize_covledger
from covledger.storage import (
    analysis_config_sha256,
    cache_root,
    clear_cache,
    clear_current_analysis,
    create_work_dir,
    current_analysis_path,
    load_current_analysis,
    publish_current_analysis,
    semantic_cache_path,
)


def _payload(analysis_id: str, suite_passed: bool = True) -> dict:
    return {
        "schema_version": 1,
        "analysis_id": analysis_id,
        "suite": {"passed": suite_passed, "exit_code": 0 if suite_passed else 1},
        "coverage": {"status": "unavailable"},
        "scope": {"config_sha256": "config-hash", "files": {}},
        "assessment": {"summary": {}, "hotspots": [], "findings": [], "gaps": []},
    }


def test_cache_root_uses_resolved_ledgercore_mount(tmp_path: Path) -> None:
    initialized = initialize_covledger(tmp_path)

    assert cache_root(tmp_path) == initialized.layout.mounts[CACHE_MOUNT].path
    assert not (tmp_path / ".ledger" / "covledger" / "cache").exists()
    assert not (tmp_path.parent / "ledger").exists()


def test_current_analysis_is_one_atomically_replaceable_file(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    first_id = str(uuid7())
    second_id = str(uuid7())

    path = publish_current_analysis(tmp_path, _payload(first_id))
    assert path == current_analysis_path(tmp_path)
    assert load_current_analysis(tmp_path)["analysis_id"] == first_id

    publish_current_analysis(tmp_path, _payload(second_id, suite_passed=False))
    current = load_current_analysis(tmp_path)
    assert current["analysis_id"] == second_id
    assert current["suite"]["passed"] is False
    assert list(cache_root(tmp_path).glob("current*")) == [path]


def test_missing_current_analysis_has_actionable_error(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)

    with pytest.raises(FileNotFoundError, match="covledger run -- pytest"):
        load_current_analysis(tmp_path)


def test_work_directory_is_scoped_under_cache_and_rejects_path_traversal(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    analysis_id = str(uuid7())

    work_dir = create_work_dir(tmp_path, analysis_id)

    assert work_dir == cache_root(tmp_path) / "work" / analysis_id
    assert work_dir.is_dir()
    with pytest.raises(ValueError, match="invalid CovLedger analysis id"):
        create_work_dir(tmp_path, "../../outside")


def test_clear_current_analysis_removes_only_current_result(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    publish_current_analysis(tmp_path, _payload(str(uuid7())))
    semantic = semantic_cache_path(tmp_path, "abc123")
    semantic.parent.mkdir(parents=True)
    semantic.write_text("{}", encoding="utf-8")

    clear_current_analysis(tmp_path)

    assert not current_analysis_path(tmp_path).exists()
    assert semantic.is_file()


def test_analysis_config_fingerprint_ignores_formatting_but_tracks_policy(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    config_path = tmp_path / ".ledger" / "covledger" / "config.toml"
    original = config_path.read_text(encoding="utf-8")
    fingerprint = analysis_config_sha256(tmp_path)

    config_path.write_text("# comment\n" + original, encoding="utf-8")
    assert analysis_config_sha256(tmp_path) == fingerprint

    config_path.write_text(original.replace("include = []", 'include = ["src"]'), encoding="utf-8")
    assert analysis_config_sha256(tmp_path) != fingerprint


def test_clear_cache_removes_only_covledger_cache_children_and_keeps_binding(tmp_path: Path) -> None:
    initialized = initialize_covledger(tmp_path)
    cache = cache_root(tmp_path)
    marker = cache / ".ledger-project.toml"
    marker_contents = marker.read_text(encoding="utf-8")
    publish_current_analysis(tmp_path, _payload(str(uuid7())))
    entry = semantic_cache_path(tmp_path, "abc123")
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("{}", encoding="utf-8")
    work = cache / "work" / str(uuid7())
    work.mkdir(parents=True)
    (work / ".coverage").write_text("temporary", encoding="utf-8")
    unrelated = cache / "unowned.txt"
    unrelated.write_text("keep", encoding="utf-8")

    clear_cache(tmp_path)

    assert not current_analysis_path(tmp_path).exists()
    assert not (cache / "jev").exists()
    assert not (cache / "work").exists()
    assert marker.read_text(encoding="utf-8") == marker_contents
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert validate_storage_binding(initialized.layout.mounts[CACHE_MOUNT]).valid
    manifest = tomllib.loads((tmp_path / ".ledger" / "ledger.toml").read_text(encoding="utf-8"))
    assert manifest["ledgers"]["covledger"]["mounts"] == {"cache": {"storage": "cache"}}
