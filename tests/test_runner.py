import json
from pathlib import Path

import pytest

from covledger.diffing import diff_runs
from covledger.ledgercore_backend import initialize_covledger
from covledger.runner import run_pytest
from covledger.storage import runs_root


def test_run_records_line_and_branch_gaps(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    (tmp_path / "app.py").write_text(
        "def classify(value):\n    if value > 0:\n        return 'positive'\n    return 'other'\n",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "from app import classify\n\ndef test_positive():\n    assert classify(1) == 'positive'\n",
        encoding="utf-8",
    )
    report = run_pytest(tmp_path, ["pytest", "-q"])

    assert report["suite_passed"] is True
    app = report["coverage"]["files"]["app.py"]
    assert 4 in app["missing_lines"]
    assert [2, 4] in app["missing_branches"]
    run_dir = runs_root(tmp_path) / report["run_id"]
    assert run_dir.is_dir()
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "sources" / "app.py").read_text(encoding="utf-8").startswith("def classify")
    assert not (tmp_path / ".covledger").exists()
    assert not (tmp_path / ".ledger" / "covledger" / "runs").exists()


def test_run_respects_exclude_scope_and_persists_it(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    (tmp_path / ".ledger" / "covledger" / "config.toml").write_text(
        '[analysis]\ninclude = []\nexclude = ["examples"]\ninclude_generated = false\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def app():\n    if False:\n        return 1\n    return 0\n", encoding="utf-8")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "demo.py").write_text(
        "def demo():\n    if False:\n        return 1\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "from app import app\n\ndef test_app():\n    assert app() == 0\n",
        encoding="utf-8",
    )

    report = run_pytest(tmp_path, ["pytest", "-q"])
    assert set(report["coverage"]["files"]) == {"app.py"}
    assert set(report["quality"]["files"]) == {"app.py"}
    run_dir = runs_root(tmp_path) / report["run_id"]
    assert (run_dir / "sources" / "app.py").is_file()
    assert not (run_dir / "sources" / "examples" / "demo.py").exists()
    assert report["scope"]["analysis"] == {
        "include": [],
        "exclude": ["examples"],
        "include_generated": False,
    }
    assert all(not item["path"].startswith("examples/") for item in report["assessment"]["hotspots"])


def test_run_rejects_non_matching_include_scope(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    (tmp_path / ".ledger" / "covledger" / "config.toml").write_text(
        '[analysis]\ninclude = ["does-not-exist"]\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="analysis.include matched no eligible Python source files"):
        run_pytest(tmp_path, ["pytest", "-q"])


def test_historical_run_scope_is_immutable_and_diff_tracks_file_sets(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "examples").mkdir()
    (tmp_path / "src" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    (tmp_path / "examples" / "demo.py").write_text("def demo():\n    return 2\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "from src.app import app\n\ndef test_app():\n    assert app() == 1\n",
        encoding="utf-8",
    )
    config_path = tmp_path / ".ledger" / "covledger" / "config.toml"
    config_path.write_text('[analysis]\ninclude = ["src"]\n', encoding="utf-8")
    first = run_pytest(tmp_path, ["pytest", "-q"])
    config_path.write_text('[analysis]\ninclude = ["examples"]\n', encoding="utf-8")
    second = run_pytest(tmp_path, ["pytest", "-q"])

    assert set(first["coverage"]["files"]) == {"src/app.py"}
    assert set(second["coverage"]["files"]) == {"examples/demo.py"}
    first_dir = runs_root(tmp_path) / first["run_id"]
    assert json.loads((first_dir / "scope.json").read_text())["analysis"]["include"] == ["src"]
    diff = diff_runs(first, second)
    assert diff["files"]["examples/demo.py"]["before"]["source_sha256"] is None
