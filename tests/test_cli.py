import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from covledger.cli import build_parser

PROJECT_UUID = "0192f9bd-7e5e-7d5c-ae7c-2fcd90c4d3cb"


def _invoke(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "covledger", "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_legacy_manifest(root: Path) -> Path:
    manifest_path = root / ".ledger" / "ledger.toml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        "schema_version = 3\n\n"
        "[project]\n"
        f'uuid = "{PROJECT_UUID}"\n'
        'name = "existing"\n\n'
        "[ledgers.taskledger.mounts.data]\n"
        'storage = "project"\n\n'
        "[ledgers.releaseledger.mounts.indexes]\n"
        'storage = "cache"\n\n'
        "[ledgers.covledger.mounts.runs]\n"
        'storage = "external"\n'
        'root = "../ledger"\n\n'
        "[ledgers.covledger.mounts.cache]\n"
        'storage = "cache"\n',
        encoding="utf-8",
    )
    return manifest_path


def test_cli_exposes_current_analysis_commands_without_history_or_diff() -> None:
    help_text = build_parser().format_help()
    for command in ("init", "run", "quality", "overview", "findings", "inspect", "next", "report", "cache"):
        assert command in help_text
    assert "runs" not in help_text
    assert "diff" not in help_text
    for argv in (["runs"], ["diff"], ["init", "--runs-storage", "external"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)


def test_init_registers_cache_only_and_cache_path_uses_ledgercore_mount(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialized = _invoke(project, "init", "--json")
    assert initialized.returncode == 0, initialized.stderr
    result = json.loads(initialized.stdout)

    manifest = tomllib.loads((project / ".ledger" / "ledger.toml").read_text(encoding="utf-8"))
    assert manifest["ledgers"]["covledger"]["mounts"] == {"cache": {"storage": "cache"}}
    assert Path(result["cache"]).is_dir()
    assert result["legacy_runs_path"] is None
    assert "runs" not in result
    assert not (project / ".covledger").exists()

    cache_path = _invoke(project, "cache", "path")
    assert cache_path.returncode == 0, cache_path.stderr
    assert cache_path.stdout.strip() == result["cache"]


def test_init_migration_notice_preserves_legacy_external_run_data(tmp_path: Path) -> None:
    project = tmp_path / "project"
    manifest_path = _write_legacy_manifest(project)
    legacy_run = tmp_path / "ledger" / "covledger" / PROJECT_UUID / "runs" / "old" / "run.json"
    legacy_run.parent.mkdir(parents=True)
    legacy_run.write_text('{"legacy": true}', encoding="utf-8")

    initialized = _invoke(project, "init")
    assert initialized.returncode == 0, initialized.stderr
    assert "no longer uses persistent run storage" in initialized.stdout
    assert "left untouched" in initialized.stdout
    assert str(legacy_run.parents[1]) in initialized.stdout
    assert legacy_run.read_text(encoding="utf-8") == '{"legacy": true}'
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["ledgers"]["covledger"]["mounts"] == {"cache": {"storage": "cache"}}
    assert "taskledger" in manifest["ledgers"]
    assert "releaseledger" in manifest["ledgers"]


def test_parser_accepts_pytest_command_after_separator() -> None:
    args = build_parser().parse_args(["run", "--", "-q"])
    assert args.command_name == "run"
    assert args.command == ["--", "-q"]
