from pathlib import Path

from covledger.diffing import diff_runs
from covledger.gaps import derive_gaps
from covledger.ledgercore_backend import initialize_covledger
from covledger.next_query import next_query
from covledger.quality import semantic_cache_key
from covledger.runner import run_pytest
from covledger.source import extract_functions


def test_missing_except_handler_is_error_path() -> None:
    source = "def process(value):\n    try:\n        return value\n    except Exception:\n        return None\n"
    candidates = derive_gaps("app.py", {"missing_lines": [4, 5], "missing_branches": []}, source)
    assert [item.kind for item in candidates] == ["error-path"]
    assert candidates[0].label == "except Exception"
    assert candidates[0].function == {"qualname": "process", "line": 1, "end_line": 5}
    assert candidates[0].id is not None
    assert candidates[0].id.startswith("G-")


def test_semantic_cache_key_ignores_path_and_start_line(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("def same(value):\n    return value\n", encoding="utf-8")
    second.write_text("\n\n\ndef same(value):\n    return value\n", encoding="utf-8")
    one = extract_functions(first, root=tmp_path)[0]
    two = extract_functions(second, root=tmp_path)[0]
    assert one.line != two.line
    assert semantic_cache_key(one) == semantic_cache_key(two)


def test_next_selects_uncovered_error_path_without_semantic_cache(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def process(value):\n"
        "    try:\n"
        "        return value\n"
        "    except Exception:\n"
        "        return None\n",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "from app import process\n\n"
        "def test_process():\n"
        "    assert process(1) == 1\n",
        encoding="utf-8",
    )
    initialize_covledger(tmp_path)
    run = run_pytest(tmp_path, ["pytest", "-q"])
    result = next_query(tmp_path)
    assert result["status"] == "ok"
    assert result["run_id"] == run["run_id"]
    assert result["candidate"]["gap"]["kind"] == "error-path"
    assert result["candidate"]["gap"]["label"] == "except Exception"
    assert result["candidate"]["function"]["qualname"] == "process"
    assert result["candidate"]["semantic"]["source"] == "none"


def test_next_blocks_failed_suite(tmp_path: Path) -> None:
    (tmp_path / "test_failure.py").write_text("def test_failure():\n    assert False\n", encoding="utf-8")
    initialize_covledger(tmp_path)
    run = run_pytest(tmp_path, ["pytest", "-q"])
    assert run["exit_code"] != 0
    assert next_query(tmp_path) == {
        "schema_version": 2,
        "run_id": run["run_id"],
        "status": "blocked",
        "reason": "suite-failed",
    }


def test_diff_does_not_compare_changed_source() -> None:
    old = {
        "run_id": "old",
        "coverage": {
            "totals": {"line_percent": 50.0, "branch_percent": 50.0},
            "files": {"app.py": {"source_sha256": "a", "missing_lines": [2], "missing_branches": [[1, 2]]}},
        },
    }
    new = {
        "run_id": "new",
        "suite_passed": True,
        "coverage": {
            "totals": {"line_percent": 75.0, "branch_percent": 75.0},
            "files": {"app.py": {"source_sha256": "b", "missing_lines": [], "missing_branches": []}},
        },
    }
    result = diff_runs(old, new)
    assert result["source_changed"] is True
    assert result["files"]["app.py"]["source_changed"] is True
    assert "newly_covered_lines" not in result["files"]["app.py"]
