from covledger.identity import (
    finding_id,
    function_id,
    gap_id,
    unique_ids,
)


def test_function_id_ignores_line_shifts() -> None:
    assert function_id("./covledger/cli.py", "main") == function_id("covledger/cli.py", "main")


def test_function_id_changes_for_path_or_qualname() -> None:
    original = function_id("covledger/cli.py", "main")
    assert function_id("other.py", "main") != original
    assert function_id("covledger/cli.py", "other") != original


def test_finding_id_uses_stable_discriminator_not_line() -> None:
    first = finding_id("app.py", "run", "exact", "broad-except", "broad-except#1")
    shifted = finding_id("app.py", "run", "exact", "broad-except", "broad-except#1")
    assert first == shifted
    assert finding_id("app.py", "run", "exact", "mutable-default", "parameter=items") != first


def test_gap_id_is_snapshot_scoped() -> None:
    first = gap_id("app.py", "a" * 64, "run", "line", None, None, 4)
    shifted = gap_id("app.py", "b" * 64, "run", "line", None, None, 4)
    assert first != shifted
    assert first.startswith("G-")


def test_collision_registry_extends_digest() -> None:
    rows = [("F", f"key-{index}") for index in range(4)]
    result = unique_ids(rows)
    assert len(result) == len(rows)
    assert len(set(result.values())) == len(rows)
