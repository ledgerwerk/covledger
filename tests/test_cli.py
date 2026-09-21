import json
import subprocess
import sys
from pathlib import Path

from covledger.cli import build_parser


def test_flat_root_option_and_runs_query_parse() -> None:
    args = build_parser().parse_args(["--root", "/tmp/project", "runs", "latest", "gaps", "--limit", "5"])
    assert args.root == "/tmp/project"
    assert args.selector == "latest"
    assert args.action == "gaps"
    assert args.limit == 5


def test_init_parser() -> None:
    args = build_parser().parse_args(["init", "--runs-storage", "external", "--json"])
    assert args.command_name == "init"
    assert args.runs_storage == "external"
    assert args.json_output is True


def test_cli_init_and_runs_external_storage(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    init = subprocess.run(
        [sys.executable, "-m", "covledger", "--root", str(project), "init", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(init.stdout)
    assert result["runs_storage"] == "external"
    assert Path(result["runs"]).is_dir()
    assert not (project / ".covledger").exists()
