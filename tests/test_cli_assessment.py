import csv
import io
import json
import subprocess
import sys
from pathlib import Path

from covledger.storage import cache_root, current_analysis_path


def _invoke(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "covledger", "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _project(root: Path) -> None:
    (root / "app.py").write_text(
        "def process(value):\n"
        "    try:\n"
        "        if value:\n"
        "            return value\n"
        "        return None\n"
        "    except Exception:\n"
        "        return None\n",
        encoding="utf-8",
    )
    (root / "test_app.py").write_text(
        "from app import process\n\ndef test_process():\n    assert process(1) == 1\n",
        encoding="utf-8",
    )


def test_quality_is_disposable_and_cli_reads_one_fresh_current_analysis(tmp_path: Path) -> None:
    _project(tmp_path)
    initialized = _invoke(tmp_path, "init")
    assert initialized.returncode == 0, initialized.stderr
    no_current = _invoke(tmp_path, "overview", "--json")
    assert no_current.returncode == 2
    assert "no current CovLedger analysis" in no_current.stderr

    quality = _invoke(tmp_path, "quality", "--json")
    assert quality.returncode == 0, quality.stderr
    assert json.loads(quality.stdout)["function_count"] >= 1
    assert not current_analysis_path(tmp_path).exists()

    run = _invoke(tmp_path, "run", "--", "pytest", "-q")
    assert run.returncode == 0, run.stderr
    assert "analysis " in run.stdout
    assert "suite: passed" in run.stdout

    overview = _invoke(tmp_path, "overview", "--json")
    assert overview.returncode == 0, overview.stderr
    overview_data = json.loads(overview.stdout)
    assert overview_data["schema_version"] == 1
    assert overview_data["analysis_id"]

    findings = _invoke(tmp_path, "findings", "--json")
    assert findings.returncode == 0, findings.stderr
    finding_data = json.loads(findings.stdout)
    assert finding_data["analysis_id"] == overview_data["analysis_id"]
    function_id = finding_data["findings"][0]["function_id"]
    inspected = _invoke(tmp_path, "inspect", function_id, "--json")
    assert inspected.returncode == 0, inspected.stderr
    inspect_data = json.loads(inspected.stdout)
    assert inspect_data["query_id"] == function_id
    assert inspect_data["source_excerpt"]

    report = _invoke(tmp_path, "report")
    assert report.returncode == 0, report.stderr
    assert f"analysis {overview_data['analysis_id']}" in report.stdout
    assert "Suggested action" in report.stdout
    csv_report = _invoke(tmp_path, "report", "--format", "csv")
    assert csv_report.returncode == 0, csv_report.stderr
    csv_rows = list(csv.DictReader(io.StringIO(csv_report.stdout)))
    assert csv_rows and csv_rows[0]["path"] == "app.py"
    assert csv_rows[0]["finding_ids"] or csv_rows[0]["gap_ids"]
    assert not any(path.suffix in {".md", ".csv"} for path in (tmp_path / ".ledger").rglob("*"))
    assert not any(path.suffix in {".md", ".csv"} for path in cache_root(tmp_path).rglob("*"))

    output = tmp_path / "explicit-report.md"
    written = _invoke(tmp_path, "report", "--output", str(output))
    assert written.returncode == 0, written.stderr
    assert written.stdout == ""
    assert output.read_text(encoding="utf-8").startswith("# CovLedger report")

    cache = cache_root(tmp_path)
    unrelated = cache / "other-tool" / "sentinel"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("preserve", encoding="utf-8")
    cleared = _invoke(tmp_path, "cache", "clear")
    assert cleared.returncode == 0, cleared.stderr
    assert not current_analysis_path(tmp_path).exists()
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert (cache / ".ledger-project.toml").is_file()


def test_current_analysis_rejects_source_and_config_drift(tmp_path: Path) -> None:
    _project(tmp_path)
    assert _invoke(tmp_path, "init").returncode == 0
    assert _invoke(tmp_path, "run", "--", "pytest", "-q").returncode == 0

    source = tmp_path / "app.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    stale_source = _invoke(tmp_path, "overview", "--json")
    assert stale_source.returncode == 2
    assert "stale" in stale_source.stderr

    assert _invoke(tmp_path, "run", "--", "pytest", "-q").returncode == 0
    config = tmp_path / ".ledger" / "covledger" / "config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("branch = true", "branch = false"), encoding="utf-8")
    stale_config = _invoke(tmp_path, "report")
    assert stale_config.returncode == 2
    assert "CovLedger configuration" in stale_config.stderr


def test_run_exit_code_is_pytest_exit_and_failed_suite_blocks_next(tmp_path: Path) -> None:
    (tmp_path / "test_failure.py").write_text("def test_failure():\n    assert False\n", encoding="utf-8")
    assert _invoke(tmp_path, "init").returncode == 0
    run = _invoke(tmp_path, "run", "--", "pytest", "-q")
    assert run.returncode == 1
    assert "suite: failed (1)" in run.stdout
    next_query = _invoke(tmp_path, "next", "--json")
    assert next_query.returncode == 0, next_query.stderr
    result = json.loads(next_query.stdout)
    assert result["status"] == "blocked"
    assert result["reason"] == "suite-failed"
