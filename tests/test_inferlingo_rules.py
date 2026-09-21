from covledger.inferlingo_rules import derive_proofs, rules_sha256


def test_exact_rules_derive_error_path_proof() -> None:
    assessment = {
        "run_id": "run-1",
        "gaps": [
            {"gap_id": "G-1", "path": "app.py", "line": 4, "gap": {"kind": "error-path", "label": "except Exception"}},
        ],
    }
    hotspot = {
        "id": "F-1",
        "path": "app.py",
        "line": 1,
        "facts": {"exact_findings": ["broad-except"], "line_count": 5, "decision_count": 0},
        "gap_ids": ["G-1"],
    }
    proofs = derive_proofs(assessment, hotspot)
    assert proofs
    assert any("error-path review" in proof["query"] for proof in proofs)
    assert any("app.py" in proof["rendered"] for proof in proofs)
    assert len(rules_sha256()) == 64
