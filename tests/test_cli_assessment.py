import json
import subprocess
import sys
from pathlib import Path

from covledger.ledgercore_backend import initialize_covledger
from covledger.runner import run_pytest


def test_overview_findings_and_inspect_commands(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def process(value):\n    try:\n        return value\n    except Exception:\n        return None\n",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "from app import process\n\ndef test_process():\n    assert process(1) == 1\n",
        encoding="utf-8",
    )
    initialize_covledger(tmp_path)
    run_pytest(tmp_path, ["pytest", "-q"])
    base = [sys.executable, "-m", "covledger", "--root", str(tmp_path)]
    overview = subprocess.run([*base, "overview", "--json"], check=True, capture_output=True, text=True)
    overview_data = json.loads(overview.stdout)
    assert overview_data["schema_version"] == 1
    details = subprocess.run([*base, "runs", "--details", "--json"], check=True, capture_output=True, text=True)
    details_data = json.loads(details.stdout)
    assert details_data["schema_version"] == 1
    assert details_data["runs"][0]["run_id"][:8]
    findings = subprocess.run([*base, "findings", "--json"], check=True, capture_output=True, text=True)
    finding_data = json.loads(findings.stdout)
    function_id = finding_data["findings"][0]["function_id"]
    inspected = subprocess.run([*base, "inspect", function_id, "--json"], check=True, capture_output=True, text=True)
    assert json.loads(inspected.stdout)["query_id"] == function_id
