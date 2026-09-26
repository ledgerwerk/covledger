import hashlib
import json
import tomllib
from pathlib import Path

import pytest
from ledgercore import uuid7

import covledger.runner as runner
from covledger.ledgercore_backend import initialize_covledger
from covledger.runner import run_pytest
from covledger.storage import cache_root, current_analysis_path, load_current_analysis, publish_current_analysis


def _project(root: Path, *, app_source: str | None = None) -> None:
    initialize_covledger(root)
    (root / "app.py").write_text(
        app_source or "def classify(value):\n    if value > 0:\n        return 'positive'\n    return 'other'\n",
        encoding="utf-8",
    )
    (root / "test_app.py").write_text(
        "from app import classify\n\ndef test_positive():\n    assert classify(1) == 'positive'\n",
        encoding="utf-8",
    )


def _empty_work(cache: Path) -> bool:
    work = cache / "work"
    return not work.exists() or not any(work.iterdir())


def test_run_publishes_one_compact_current_analysis_and_cleans_work(tmp_path: Path) -> None:
    _project(tmp_path)

    analysis = run_pytest(tmp_path, ["pytest", "-q"])

    cache = cache_root(tmp_path)
    current = load_current_analysis(tmp_path)
    app_hash = hashlib.sha256((tmp_path / "app.py").read_bytes()).hexdigest()
    assert analysis["suite"]["passed"] is True
    assert analysis["suite"]["pytest_args"] == ["pytest", "-q"]
    assert current["analysis_id"] == analysis["analysis_id"]
    assert current["coverage"]["status"] == "available"
    app = current["coverage"]["files"]["app.py"]
    assert 4 in app["missing_lines"]
    assert [2, 4] in app["missing_branches"]
    assert current["scope"]["files"] == {"app.py": {"sha256": app_hash}}
    assert current["scope"]["config_sha256"]
    assert set(current) == {"schema_version", "analysis_id", "suite", "coverage", "scope", "assessment"}
    assert {path.name for path in cache.iterdir()} == {".ledger-project.toml", "current.json", "work"}
    assert _empty_work(cache)
    assert not list(cache.rglob("*.py"))
    assert not list(cache.rglob(".coverage"))
    assert not list(cache.rglob("coverage.json"))
    assert not (tmp_path / ".ledger" / "covledger" / "cache").exists()
    assert not (tmp_path.parent / "ledger").exists()
    mounts = tomllib.loads((tmp_path / ".ledger" / "ledger.toml").read_text())["ledgers"]["covledger"]["mounts"]
    assert mounts == {"cache": {"storage": "cache"}}


def test_run_replaces_current_result_and_keeps_failed_suite_status(tmp_path: Path) -> None:
    _project(tmp_path)
    first = run_pytest(tmp_path, ["pytest", "-q"])

    (tmp_path / "test_app.py").write_text(
        "from app import classify\n\ndef test_positive():\n    assert classify(-1) == 'positive'\n",
        encoding="utf-8",
    )
    second = run_pytest(tmp_path, ["pytest", "-q"])

    assert first["analysis_id"] != second["analysis_id"]
    assert second["suite"] == {"passed": False, "exit_code": 1, "pytest_args": ["pytest", "-q"]}
    assert load_current_analysis(tmp_path)["analysis_id"] == second["analysis_id"]
    assert len(list(cache_root(tmp_path).glob("current.json"))) == 1
    assert _empty_work(cache_root(tmp_path))


def test_analysis_scope_excludes_files_from_current_evidence(tmp_path: Path) -> None:
    _project(tmp_path)
    (tmp_path / ".ledger" / "covledger" / "config.toml").write_text(
        '[analysis]\ninclude = []\nexclude = ["examples"]\ninclude_generated = false\n',
        encoding="utf-8",
    )
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "demo.py").write_text(
        "def demo():\n    if False:\n        return 1\n    return 0\n",
        encoding="utf-8",
    )

    analysis = run_pytest(tmp_path, ["pytest", "-q"])

    assert set(analysis["coverage"]["files"]) == {"app.py"}
    assert set(analysis["scope"]["files"]) == {"app.py"}
    assert all(not item["path"].startswith("examples/") for item in analysis["assessment"]["hotspots"])
    assert all(not gap["path"].startswith("examples/") for gap in analysis["assessment"]["gaps"])


@pytest.mark.parametrize(
    "stage_name",
    [
        "_write_coveragerc",
        "subprocess.run",
        "_load_coverage",
        "quality_document_from_sources",
        "build_assessment",
        "publish_current_analysis",
    ],
)
def test_runner_cleans_work_and_leaves_no_current_result_after_stage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_name: str,
) -> None:
    _project(tmp_path)
    cache = cache_root(tmp_path)
    publish_current_analysis(
        tmp_path,
        {
            "schema_version": 1,
            "analysis_id": str(uuid7()),
            "suite": {"passed": True, "exit_code": 0},
            "coverage": {"status": "unavailable"},
            "scope": {"config_sha256": "old", "files": {}},
            "assessment": {"summary": {}, "hotspots": [], "findings": [], "gaps": []},
        },
    )

    def fail(*args, **kwargs):
        raise RuntimeError(f"forced {stage_name} failure")

    if stage_name == "subprocess.run":
        monkeypatch.setattr(runner.subprocess, "run", fail)
    else:
        monkeypatch.setattr(runner, stage_name, fail)

    with pytest.raises(RuntimeError, match="forced"):
        run_pytest(tmp_path, ["pytest", "-q"])

    assert not current_analysis_path(tmp_path).exists()
    assert _empty_work(cache)


def test_run_work_area_contains_no_permanent_raw_coverage(tmp_path: Path) -> None:
    _project(tmp_path)

    run_pytest(tmp_path, ["python", "-m", "pytest", "-q"])

    cache = cache_root(tmp_path)
    assert _empty_work(cache)
    assert not any(cache.rglob(".coverage"))
    assert not any(cache.rglob("coveragerc"))
    assert not any(cache.rglob("coverage.json"))
    current = json.loads(current_analysis_path(tmp_path).read_text(encoding="utf-8"))
    assert all("snapshot" not in value for value in current["scope"]["files"].values())
