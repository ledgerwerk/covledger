import hashlib
from pathlib import Path

import pytest

from covledger.assessment import build_assessment
from covledger.decisions import decisions_from_config
from covledger.ledgercore_backend import initialize_covledger
from covledger.quality import quality_document_from_sources, semantic_cache_key
from covledger.source import extract_functions


def _analysis(root: Path) -> dict:
    source_path = root / "pkg" / "module.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "def wrapper(value):\n    try:\n        return value\n    except Exception:\n        return None\n",
        encoding="utf-8",
    )
    quality = quality_document_from_sources({"pkg/module.py": source_path}, root=root)
    return {
        "schema_version": 1,
        "analysis_id": "analysis-test",
        "suite": {"passed": True, "exit_code": 0},
        "coverage": {"status": "unavailable", "files": {}},
        "scope": {
            "config_sha256": "config-hash",
            "files": {"pkg/module.py": {"sha256": hashlib.sha256(source_path.read_bytes()).hexdigest()}},
        },
        "quality": quality,
    }


def test_explicit_ignore_decision_removes_symbol_from_prioritization_only(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    analysis = _analysis(tmp_path)
    config_path = tmp_path / ".ledger" / "covledger" / "config.toml"
    baseline = build_assessment(tmp_path, analysis)
    assert len(baseline["hotspots"]) == 1

    with config_path.open("a", encoding="utf-8") as config:
        config.write(
            '\n[[decision]]\nsymbol = "pkg/module.py:wrapper"\naction = "ignore"\nreason = "presentation wrapper"\n'
        )

    decided = build_assessment(tmp_path, analysis)

    assert decided["hotspots"] == []
    assert analysis["coverage"]["status"] == "unavailable"
    assert (
        decisions_from_config(
            {"decision": [{"symbol": "pkg/module.py:wrapper", "action": "ignore", "reason": "presentation wrapper"}]}
        )[0].reason
        == "presentation wrapper"
    )


def test_rule_decision_hides_only_matching_finding_and_removal_restores_it(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    analysis = _analysis(tmp_path)
    config_path = tmp_path / ".ledger" / "covledger" / "config.toml"
    baseline = build_assessment(tmp_path, analysis)
    assert any(item["id"] == "broad-except" for item in baseline["findings"])

    with config_path.open("a", encoding="utf-8") as config:
        config.write(
            '\n[[decision]]\nsymbol = "pkg/module.py:wrapper"\naction = "ignore-finding"\n'
            'rule = "broad-except"\nreason = "intentional boundary"\n'
        )
    decided = build_assessment(tmp_path, analysis)
    assert not any(item["id"] == "broad-except" for item in decided["findings"])
    assert decided["hotspots"][0]["finding_ids"] == []

    config_contents = config_path.read_text(encoding="utf-8").replace("[[decision]]", "[[ignored]]")
    config_path.write_text(config_contents, encoding="utf-8")
    restored = build_assessment(tmp_path, analysis)
    assert any(item["id"] == "broad-except" for item in restored["findings"])


def test_decision_schema_rejects_unstable_symbols_and_unknown_actions() -> None:
    with pytest.raises(ValueError, match="stable relative symbol"):
        decisions_from_config({"decision": [{"symbol": "../module.py:f", "action": "ignore", "reason": "why"}]})
    with pytest.raises(ValueError, match="expected ignore or ignore-finding"):
        decisions_from_config({"decision": [{"symbol": "pkg/module.py:f", "action": "dismiss", "reason": "why"}]})


def test_semantic_cache_key_tracks_model_but_not_path_or_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    first_path.write_text("def same(value):\n    return value\n", encoding="utf-8")
    second_path.write_text("\n\ndef same(value):\n    return value\n", encoding="utf-8")
    first_function = extract_functions(first_path, root=tmp_path)[0]
    second_function = extract_functions(second_path, root=tmp_path)[0]

    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "model-a")
    first_key = semantic_cache_key(first_function)
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "model-b")
    second_key = semantic_cache_key(second_function)

    assert first_key != second_key
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "model-a")
    assert semantic_cache_key(second_function) == first_key
