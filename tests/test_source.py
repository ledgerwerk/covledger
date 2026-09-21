from pathlib import Path

from covledger.source import extract_functions


def test_extracts_python_facts(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text(
        "def risky(items=[]):\n"
        "    try:\n"
        "        if items:\n"
        "            return eval(items[0])\n"
        "    except Exception:\n"
        "        return None\n"
        "    return 0\n",
        encoding="utf-8",
    )
    [function] = extract_functions(path, root=tmp_path)
    assert function.qualname == "risky"
    assert function.mutable_default is True
    assert function.broad_except is True
    assert function.eval_exec_calls == ("eval",)
    assert "mutable-default" in function.exact_findings
    assert "broad-except" in function.exact_findings
