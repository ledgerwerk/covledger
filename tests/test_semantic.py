from covledger.scoring import augmented_priority, semantic_modifier, semantic_score


def test_semantic_evidence_is_continuous_and_separate() -> None:
    judgments = {"ambiguous error contract": 0.8, "misleading name": 0.2}
    score = semantic_score(judgments)
    assert 0 < score <= 100
    assert semantic_modifier(judgments) > 0
    assert augmented_priority(70, judgments) >= 70


def test_missing_semantic_cache_is_not_zero_evidence() -> None:
    assert semantic_score({}) == 0
    assert augmented_priority(70, {}) == 70
