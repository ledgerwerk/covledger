from pathlib import Path

from covledger.ledgercore_backend import initialize_covledger
from covledger.runner import run_pytest
from covledger.storage import runs_root


def test_run_records_line_and_branch_gaps(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    (tmp_path / "app.py").write_text(
        "def classify(value):\n"
        "    if value > 0:\n"
        "        return 'positive'\n"
        "    return 'other'\n",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "from app import classify\n\n"
        "def test_positive():\n"
        "    assert classify(1) == 'positive'\n",
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
