from pathlib import Path

from covledger.assessment import assessment_for_run
from covledger.ledgercore_backend import initialize_covledger
from covledger.runner import run_pytest


def test_run_persists_assessment_and_hotspot_scores(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def process(value):\n    try:\n        return value\n    except Exception:\n        return None\n",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "from app import process\n\ndef test_process():\n    assert process(1) == 1\n",
        encoding="utf-8",
    )
    initialize_covledger(tmp_path)
    run = run_pytest(tmp_path, ["pytest", "-q"])
    assessment = assessment_for_run(tmp_path, run)
    assert assessment["schema_version"] == 1
    assert assessment["scorer"]["rules_sha256"]
    assert assessment["hotspots"]
    assert (Path(run["run_dir"]) / "assessment.json").is_file()
    assert assessment["hotspots"][0]["score"]["priority"] <= 100
    assert assessment["hotspots"][0]["score"]["regression"] is None
