from covledger.scoring import attention_band, priority_score, quality_score, rules_sha256, structural_score


def test_structural_score_grows_with_function_length() -> None:
    short = {"line_count": 51, "decision_count": 0, "max_nesting": 0, "return_count": 0, "parameter_count": 0}
    long = {"line_count": 150, "decision_count": 0, "max_nesting": 0, "return_count": 0, "parameter_count": 0}
    assert structural_score(long) > structural_score(short)


def test_hazard_and_quality_are_bounded() -> None:
    facts = {
        "line_count": 180,
        "decision_count": 30,
        "max_nesting": 8,
        "return_count": 15,
        "parameter_count": 12,
        "bare_except": True,
        "broad_except": True,
        "mutable_default": True,
        "eval_exec_calls": ["eval"],
        "exact_findings": ["bare-except", "broad-except"],
    }
    hazard, structural, quality = quality_score(facts)
    assert 0 <= hazard <= 100
    assert 0 <= structural <= 100
    assert 0 <= quality <= 100


def test_error_path_exception_receives_bonus() -> None:
    base = {
        "line_count": 10,
        "decision_count": 0,
        "max_nesting": 0,
        "return_count": 0,
        "parameter_count": 0,
        "broad_except": True,
        "exact_findings": ["broad-except"],
    }
    score = priority_score(base, gap_kind="error-path")
    assert any(item["points"] == 12 for item in score.bonuses)
    assert score.priority >= score.quality


def test_attention_bands_and_rules_hash() -> None:
    assert attention_band(90) == "critical"
    assert attention_band(70) == "high"
    assert len(rules_sha256()) == 64
