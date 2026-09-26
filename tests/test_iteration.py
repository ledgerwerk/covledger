import hashlib
from pathlib import Path

import pytest

from covledger.gaps import derive_gaps
from covledger.ledgercore_backend import initialize_covledger
from covledger.next_query import next_query
from covledger.quality import semantic_cache_key
from covledger.runner import run_pytest
from covledger.source import extract_functions
from covledger.storage import StaleAnalysisError, load_fresh_current_analysis


def _project(root: Path, *, failing_test: bool = False) -> None:
    initialize_covledger(root)
    (root / "app.py").write_text(
        "def process(value):\n    try:\n        return value\n    except Exception:\n        return None\n",
        encoding="utf-8",
    )
    assertion = "assert False" if failing_test else "assert process(1) == 1"
    (root / "test_app.py").write_text(
        f"from app import process\n\ndef test_process():\n    {assertion}\n",
        encoding="utf-8",
    )


def test_missing_except_handler_is_error_path() -> None:
    source = "def process(value):\n    try:\n        return value\n    except Exception:\n        return None\n"
    candidates = derive_gaps("app.py", {"missing_lines": [4, 5], "missing_branches": []}, source)
    assert [item.kind for item in candidates] == ["error-path"]
    assert candidates[0].label == "except Exception"
    assert candidates[0].function == {"qualname": "process", "line": 1, "end_line": 5}
    assert candidates[0].id is not None and candidates[0].id.startswith("G-")


def test_semantic_cache_key_ignores_path_and_start_line(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("def same(value):\n    return value\n", encoding="utf-8")
    second.write_text("\n\n\ndef same(value):\n    return value\n", encoding="utf-8")
    one = extract_functions(first, root=tmp_path)[0]
    two = extract_functions(second, root=tmp_path)[0]
    assert one.line != two.line
    assert semantic_cache_key(one) == semantic_cache_key(two)


def test_next_selects_current_uncovered_error_path(tmp_path: Path) -> None:
    _project(tmp_path)

    analysis = run_pytest(tmp_path, ["pytest", "-q"])
    result = next_query(tmp_path)

    assert result["status"] == "ok"
    assert result["analysis_id"] == analysis["analysis_id"]
    assert "run_id" not in result
    assert result["candidate"]["gap"]["kind"] == "error-path"
    assert result["candidate"]["gap"]["label"] == "except Exception"
    assert result["candidate"]["function"]["qualname"] == "process"
    assert result["candidate"]["semantic"]["source"] == "none"


def test_next_blocks_failed_suite_without_history(tmp_path: Path) -> None:
    _project(tmp_path, failing_test=True)

    analysis = run_pytest(tmp_path, ["pytest", "-q"])
    result = next_query(tmp_path)

    assert analysis["suite"]["exit_code"] != 0
    assert result == {
        "schema_version": 1,
        "analysis_id": analysis["analysis_id"],
        "status": "blocked",
        "reason": "suite-failed",
    }


def test_fresh_current_analysis_rejects_changed_and_missing_source(tmp_path: Path) -> None:
    _project(tmp_path)
    analysis = run_pytest(tmp_path, ["pytest", "-q"])

    (tmp_path / "app.py").write_text("def process(value):\n    return value\n", encoding="utf-8")
    with pytest.raises(StaleAnalysisError, match="app.py"):
        load_fresh_current_analysis(tmp_path)
    with pytest.raises(StaleAnalysisError, match="app.py"):
        next_query(tmp_path)

    (tmp_path / "app.py").unlink()
    with pytest.raises(StaleAnalysisError, match="app.py"):
        load_fresh_current_analysis(tmp_path)
    assert analysis["scope"]["files"]["app.py"]["sha256"]


def test_fresh_current_analysis_rejects_config_changes(tmp_path: Path) -> None:
    _project(tmp_path)
    run_pytest(tmp_path, ["pytest", "-q"])
    config_path = tmp_path / ".ledger" / "covledger" / "config.toml"
    before = config_path.read_text(encoding="utf-8")
    config_path.write_text(before.replace("include = []", 'include = ["src"]'), encoding="utf-8")

    with pytest.raises(StaleAnalysisError, match="CovLedger configuration"):
        load_fresh_current_analysis(tmp_path)


def test_freshness_rejects_cached_paths_outside_project(tmp_path: Path) -> None:
    _project(tmp_path)
    analysis = run_pytest(tmp_path, ["pytest", "-q"])
    analysis["scope"]["files"]["../outside.py"] = {"sha256": hashlib.sha256(b"outside").hexdigest()}
    from covledger.storage import validate_current_source_state

    with pytest.raises(StaleAnalysisError, match="outside.py"):
        validate_current_source_state(tmp_path, analysis)


def test_next_requires_current_analysis(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)

    with pytest.raises(FileNotFoundError, match="covledger run -- pytest"):
        next_query(tmp_path)
