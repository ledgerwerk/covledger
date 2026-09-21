from pathlib import Path

import pytest

from covledger.analysis_scope import AnalysisScope, analysis_scope_from_config, matches_scope_rule, normalize_scope_rule
from covledger.ledgercore_backend import initialize_covledger
from covledger.quality import quality_report


def test_include_only_and_recursive_exclude() -> None:
    scope = AnalysisScope(include=("src",), exclude=("src/generated",))

    assert scope.allows_path("src/app.py")
    assert not scope.allows_path("src/generated/machine.py")
    assert not scope.allows_path("examples/demo.py")


def test_quality_uses_configured_scope(tmp_path: Path) -> None:
    initialize_covledger(tmp_path)
    (tmp_path / ".ledger" / "covledger" / "config.toml").write_text(
        '[analysis]\ninclude = ["package"]\nexclude = []\n', encoding="utf-8"
    )
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "demo.py").write_text("def demo():\n    return 2\n", encoding="utf-8")
    report = quality_report(tmp_path, root=tmp_path)
    assert {row["path"] for row in report["functions"]} == {"package/app.py"}


def test_directory_rules_and_basename_globs() -> None:
    assert matches_scope_rule("examples/nested/demo.py", "examples")
    assert matches_scope_rule("nested/context_covledger.unpack.py", "context_*.unpack.py")
    assert not matches_scope_rule("nested/context_covledger.py", "context_*.unpack.py")


def test_scope_normalization_and_validation() -> None:
    assert normalize_scope_rule(" ./examples\\nested ") == "examples/nested"
    with pytest.raises(ValueError, match="analysis.include"):
        analysis_scope_from_config({"analysis": {"include": ["../other-repo"]}})
    with pytest.raises(ValueError, match="analysis.exclude"):
        analysis_scope_from_config({"analysis": {"exclude": [""]}})
    with pytest.raises(ValueError, match="analysis.include"):
        analysis_scope_from_config({"analysis": {"include": "src"}})


def test_config_defaults_and_generated_precedence() -> None:
    scope = analysis_scope_from_config({"analysis": {}})
    assert scope.to_dict() == {
        "include": [],
        "exclude": ["context_*.unpack.py"],
        "include_generated": False,
    }
    scope = analysis_scope_from_config(
        {
            "analysis": {
                "include": ["src"],
                "exclude": ["src/generated"],
                "include_generated": True,
            }
        }
    )
    assert scope.include_generated is True
    assert not scope.allows_path("src/generated/machine.py")
