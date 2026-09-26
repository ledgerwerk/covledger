import json
from pathlib import Path

from covledger.ledgercore_backend import initialize_covledger
from covledger.runner import run_pytest
from covledger.storage import cache_root, load_current_analysis


def test_run_builds_compact_in_memory_assessment_for_current_analysis(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def process(value):\n    try:\n        return value\n    except Exception:\n        return None\n",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "from app import process\n\ndef test_process():\n    assert process(1) == 1\n",
        encoding="utf-8",
    )
    initialize_covledger(tmp_path)

    analysis = run_pytest(tmp_path, ["pytest", "-q"])
    cached = load_current_analysis(tmp_path)
    assessment = cached["assessment"]

    assert assessment["schema_version"] == 1
    assert assessment["analysis_id"] == analysis["analysis_id"]
    assert assessment["scorer"]["rules_sha256"]
    assert assessment["hotspots"]
    assert assessment["hotspots"][0]["score"]["priority"] <= 100
    assert assessment["hotspots"][0]["score"]["regression"] is None
    assert assessment == analysis["assessment"]
    assert {path.name for path in cache_root(tmp_path).iterdir()} == {".ledger-project.toml", "current.json", "work"}
    assert not any(path.name == "sources" for path in cache_root(tmp_path).rglob("*"))
    assert (
        json.loads((cache_root(tmp_path) / "current.json").read_text(encoding="utf-8"))["analysis_id"]
        == analysis["analysis_id"]
    )
