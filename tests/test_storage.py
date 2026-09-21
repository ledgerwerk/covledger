import json
from pathlib import Path

import pytest

from covledger.storage import load_run, publish_run, resolve_run_id


def test_published_run_is_immutable_and_latest_resolves(tmp_path: Path) -> None:
    run_dir = tmp_path / ".covledger" / "runs" / "run_20260101T000000Z_aaaaaa"
    run_dir.mkdir(parents=True)
    publish_run(run_dir, {"run_id": run_dir.name})
    assert load_run(tmp_path, "latest")["run_id"] == run_dir.name
    assert resolve_run_id(tmp_path, "latest") == run_dir.name
    with pytest.raises(FileExistsError):
        publish_run(run_dir, {"run_id": "rewritten"})
    assert json.loads((run_dir / "run.json").read_text())["run_id"] == run_dir.name
