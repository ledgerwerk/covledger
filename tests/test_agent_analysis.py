from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import covledger.agent_analysis as agent_analysis
import covledger.quality as quality
from covledger.cli import build_parser, main
from covledger.coverage_data import unavailable_coverage
from covledger.ledgercore_backend import initialize_covledger
from covledger.next_query import next_query
from covledger.quality import semantic_cache_key
from covledger.runner import run_pytest
from covledger.source import extract_functions
from covledger.storage import StaleAnalysisError, load_current_analysis, publish_current_analysis, semantic_cache_path


def _project(root: Path, *, two_functions: bool = False) -> None:
    initialize_covledger(root)
    if two_functions:
        source = (
            "def process(value):\n"
            "    try:\n"
            "        if value:\n"
            "            return value\n"
            "        return None\n"
            "    except Exception:\n"
            "        return None\n"
            "\n"
            "def secondary(value):\n"
            "    try:\n"
            "        if value:\n"
            "            return value\n"
            "        return None\n"
            "    except Exception:\n"
            "        return None\n"
        )
    else:
        source = (
            "def process(value):\n"
            "    try:\n"
            "        if value:\n"
            "            return value\n"
            "        return None\n"
            "    except Exception:\n"
            "        return None\n"
        )
    (root / "app.py").write_text(source, encoding="utf-8")
    (root / "test_app.py").write_text(
        "from app import process\n\ndef test_process():\n    assert process(1) == 1\n",
        encoding="utf-8",
    )


def _fake_semantic(calls: list[str]):
    def decide(item: Any, *, root: Path, refresh: bool = False) -> dict[str, Any]:
        calls.append(item.path + ":" + item.qualname)
        return {
            "cache_key": "fake-cache-key",
            "model": "fake-model",
            "request_id": f"fake-request-{len(calls)}",
            "usage": {"input_tokens": 12, "output_tokens": 3},
            "judgments": {"ambiguous error contract": 0.9},
        }

    return decide


def test_default_analysis_matches_next_and_calls_semantics_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    analysis = run_pytest(tmp_path, ["pytest", "-q"])
    expected = next_query(tmp_path)["candidate"]
    calls: list[str] = []
    monkeypatch.setattr(agent_analysis, "semantic_judgments", _fake_semantic(calls))

    result = agent_analysis.analyze_current(tmp_path)

    assert result["schema_version"] == 1
    assert result["command"] == "analyze"
    assert result["analysis_id"] == analysis["analysis_id"]
    assert result["status"] == "ok"
    assert result["mode"] == "single"
    assert result["selection"]["strategy"] == "current-next-priority"
    assert result["selection"]["selected_function_count"] == 1
    assert result["selection"]["selected_gap_count"] >= 2
    assert result["targets"][0]["function_id"] == expected["function_id"]
    target = result["targets"][0]
    assert target["primary_gap"]["gap_id"] == expected["gap_id"]
    assert target["gaps"]
    assert target["coverage"]["missing_lines"] > 0
    assert target["priority"]["score"] == expected["priority_score"]
    assert target["semantic"]["status"] == "fresh"
    assert target["semantic"]["request_id"] == "fake-request-1"
    assert target["semantic"]["findings"] == ["ambiguous error contract"]
    expected_source = (
        "def process(value):\n"
        "    try:\n"
        "        if value:\n"
        "            return value\n"
        "        return None\n"
        "    except Exception:\n"
        "        return None\n"
    )
    assert target["source"]["function_text"] == expected_source
    assert target["source"]["sha256"]
    assert target["source"]["file_sha256"]
    assert target["deterministic"]["findings"]
    assert {"finding_id", "id", "line", "path", "function_id"} <= target["deterministic"]["findings"][0].keys()
    assert target["proofs"]
    assert target["agent"]["recommended_sequence"]
    assert result["cost"]["api_requests"] == 1
    assert result["cost"]["max_api_requests"] == 1
    assert result["cost"]["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert calls == ["app.py:process"]


def test_batch_budget_is_preflighted_before_any_semantic_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path, two_functions=True)
    run_pytest(tmp_path, ["pytest", "-q"])
    calls: list[str] = []
    monkeypatch.setattr(agent_analysis, "semantic_judgments", _fake_semantic(calls))

    missing_budget = agent_analysis.analyze_current(tmp_path, all_functions=True)
    assert missing_budget["status"] == "budget-blocked"
    assert missing_budget["cost"]["api_requests"] == 0
    assert missing_budget["cost"]["max_api_requests"] is None
    result = agent_analysis.analyze_current(tmp_path, all_functions=True, max_requests=1)

    assert result["status"] == "budget-blocked"
    assert result["mode"] == "all"
    assert result["cost"]["required_api_requests"] > result["cost"]["max_api_requests"]
    assert result["cost"]["api_requests"] == 0
    assert result["cost"]["budget_sufficient"] is False
    assert calls == []
    assert len(result["targets"]) == result["selection"]["selected_function_count"]


def test_analyze_cli_emits_rich_json_and_short_human_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _project(tmp_path)
    run_pytest(tmp_path, ["pytest", "-q"])
    calls: list[str] = []
    monkeypatch.setattr(agent_analysis, "semantic_judgments", _fake_semantic(calls))

    assert main(["--root", str(tmp_path), "analyze", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["command"] == "analyze"
    assert payload["status"] == "ok"
    assert payload["targets"][0]["source"]["function_text"].startswith("def process")

    assert main(["--root", str(tmp_path), "analyze"]) == 0
    human = capsys.readouterr().out
    assert "analysis ok (single)" in human
    assert "gap:" in human
    assert "cost: API requests 1 / 1" in human
    assert "suggested action:" in human


def test_analyze_cli_rejects_invalid_budget_and_conflicting_modes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--root", str(tmp_path), "analyze", "--max-requests", "0"]) == 2
    assert "--max-requests must be a positive integer" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "analyze", "--plan", "--refresh"]) == 2
    assert "--plan and --refresh" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "analyze", "--cache-only", "--refresh"]) == 2
    assert "--cache-only and --refresh" in capsys.readouterr().err


def test_analyze_cli_parser_exposes_all_modes() -> None:
    args = build_parser().parse_args(["analyze", "--all", "--max-requests", "4", "--cache-only", "--json"])
    assert args.all_functions is True
    assert args.max_requests == 4
    assert args.cache_only is True
    assert args.json_output is True


def _seed_semantic_cache(root: Path) -> None:
    item = extract_functions(root / "app.py", root=root)[0]
    key = semantic_cache_key(item)
    path = semantic_cache_path(root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cache_schema": 3,
                "cache_key": key,
                "model": "cached-model",
                "request_id": "cached-request",
                "usage": {"input_tokens": 8},
                "judgments": {"ambiguous error contract": 0.8},
            }
        ),
        encoding="utf-8",
    )


def test_plan_and_cache_only_never_call_semantic_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    run_pytest(tmp_path, ["pytest", "-q"])
    calls: list[str] = []
    monkeypatch.setattr(agent_analysis, "semantic_judgments", _fake_semantic(calls))

    planned = agent_analysis.analyze_current(tmp_path, plan=True)
    cache_only = agent_analysis.analyze_current(tmp_path, cache_only=True)

    assert planned["status"] == "planned"
    assert planned["cost"]["required_api_requests"] == 1
    assert planned["cost"]["api_requests"] == 0
    assert planned["targets"][0]["semantic"]["status"] == "not-run-plan"
    assert planned["targets"][0]["semantic"]["judgments"] is None
    assert cache_only["status"] == "cache-only"
    assert cache_only["cost"]["api_requests"] == 0
    assert cache_only["targets"][0]["semantic"]["status"] == "not-run-cache-miss"
    assert calls == []


def test_cached_semantics_are_reused_and_refresh_stays_single_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    run_pytest(tmp_path, ["pytest", "-q"])
    _seed_semantic_cache(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(agent_analysis, "semantic_judgments", _fake_semantic(calls))

    cached = agent_analysis.analyze_current(tmp_path, max_requests=20)
    refreshed = agent_analysis.analyze_current(tmp_path, max_requests=20, refresh=True)

    assert cached["cost"]["api_requests"] == 0
    assert cached["cost"]["cache_hits"] == 1
    assert cached["targets"][0]["semantic"]["status"] == "cached"
    assert cached["targets"][0]["semantic"]["request_id"] == "cached-request"
    assert refreshed["cost"]["max_api_requests"] == 1
    assert refreshed["cost"]["required_api_requests"] == 1
    assert refreshed["cost"]["cache_hits"] == 0
    assert refreshed["cost"]["api_requests"] == 1
    assert refreshed["targets"][0]["semantic"]["status"] == "fresh"
    assert calls == ["app.py:process"]


def test_batch_refresh_budget_blocks_before_any_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path, two_functions=True)
    run_pytest(tmp_path, ["pytest", "-q"])
    calls: list[str] = []
    monkeypatch.setattr(agent_analysis, "semantic_judgments", _fake_semantic(calls))

    result = agent_analysis.analyze_current(tmp_path, all_functions=True, max_requests=1, refresh=True)

    assert result["status"] == "budget-blocked"
    assert result["cost"]["required_api_requests"] > 1
    assert result["cost"]["cache_hits"] == 0
    assert result["cost"]["api_requests"] == 0
    assert calls == []


def test_batch_analyzes_each_unique_function_once_and_keeps_all_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path, two_functions=True)
    run_pytest(tmp_path, ["pytest", "-q"])
    calls: list[str] = []
    monkeypatch.setattr(agent_analysis, "semantic_judgments", _fake_semantic(calls))

    result = agent_analysis.analyze_current(tmp_path, all_functions=True, max_requests=10)

    ids = [target["function_id"] for target in result["targets"]]
    assert result["status"] == "ok"
    assert len(ids) == len(set(ids))
    assert result["selection"]["selected_function_count"] == len(ids)
    assert result["selection"]["selected_gap_count"] == sum(len(target["gaps"]) for target in result["targets"])
    assert len(calls) == len(ids) == result["cost"]["api_requests"]
    assert all(target["gaps"] for target in result["targets"])


def test_failed_suite_and_unavailable_coverage_do_not_call_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    (tmp_path / "test_app.py").write_text(
        "from app import process\n\ndef test_process():\n    assert False\n", encoding="utf-8"
    )
    run_pytest(tmp_path, ["pytest", "-q"])
    calls: list[str] = []
    monkeypatch.setattr(agent_analysis, "semantic_judgments", _fake_semantic(calls))

    failed = agent_analysis.analyze_current(tmp_path)
    assert failed["status"] == "suite-failed"
    assert failed["targets"] == []
    assert failed["cost"]["api_requests"] == 0
    assert calls == []

    (tmp_path / "test_app.py").write_text(
        "from app import process\n\ndef test_process():\n    assert process(1) == 1\n", encoding="utf-8"
    )
    run_pytest(tmp_path, ["pytest", "-q"])
    current = load_current_analysis(tmp_path)
    current["coverage"] = unavailable_coverage(RuntimeError("test coverage unavailable"))
    publish_current_analysis(tmp_path, current)
    unavailable = agent_analysis.analyze_current(tmp_path)
    assert unavailable["status"] == "coverage-unavailable"
    assert unavailable["targets"] == []
    assert unavailable["cost"]["api_requests"] == 0
    assert calls == []


def test_stale_current_and_missing_stable_function_fail_without_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    run_pytest(tmp_path, ["pytest", "-q"])
    calls: list[str] = []
    monkeypatch.setattr(agent_analysis, "semantic_judgments", _fake_semantic(calls))

    source = tmp_path / "app.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    stale = agent_analysis.analyze_current(tmp_path)
    assert stale["status"] == "stale-analysis"
    assert stale["targets"] == []
    assert stale["cost"]["api_requests"] == 0
    assert stale["stale"]["changed"] == ["app.py"]
    assert stale["next_step"] == "covledger run -- pytest -q"
    assert calls == []
    assert calls == []

    run_pytest(tmp_path, ["pytest", "-q"])
    current = load_current_analysis(tmp_path)
    current["assessment"]["hotspots"][0]["id"] = "F-missing-live-function"
    publish_current_analysis(tmp_path, current)
    with pytest.raises(StaleAnalysisError, match="missing live function"):
        agent_analysis.analyze_current(tmp_path)
    assert calls == []


def test_no_actionable_gap_and_run_next_remain_api_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_covledger(tmp_path)
    (tmp_path / "app.py").write_text("def process(value):\n    return value\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "from app import process\n\ndef test_process():\n    assert process(1) == 1\n", encoding="utf-8"
    )
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("semantic-called")
        pytest.fail("run and next must not call semantic judgments")

    monkeypatch.setattr(quality, "semantic_judgments", forbidden)
    monkeypatch.setattr(agent_analysis, "semantic_judgments", forbidden)
    run_pytest(tmp_path, ["pytest", "-q"])
    assert next_query(tmp_path)["candidate"] is None
    result = agent_analysis.analyze_current(tmp_path)
    assert result["status"] == "no-target"
    assert result["targets"] == []
    assert result["cost"]["api_requests"] == 0
    assert calls == []


def test_aggregated_usage_preserves_unreported_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path, two_functions=True)
    run_pytest(tmp_path, ["pytest", "-q"])
    calls: list[str] = []

    def decide(item: Any, *, root: Path, refresh: bool = False) -> dict[str, Any]:
        calls.append(item.qualname)
        result: dict[str, Any] = {
            "model": "fake-model",
            "request_id": f"request-{len(calls)}",
            "judgments": {},
        }
        if len(calls) == 1:
            result["usage"] = {"input_tokens": 8, "output_tokens": 3}
        else:
            result["usage"] = {"input_tokens": 4}
        return result

    monkeypatch.setattr(agent_analysis, "semantic_judgments", decide)
    response = agent_analysis.analyze_current(tmp_path, all_functions=True, max_requests=5)

    assert response["cost"]["usage"] == {"input_tokens": 12}
    assert "output_tokens" not in response["cost"]["usage"]
    assert len(calls) == response["cost"]["api_requests"] == 2
