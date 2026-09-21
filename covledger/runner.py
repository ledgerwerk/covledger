"""Execute pytest through the MVP Coverage.py backend and publish one immutable run."""

from __future__ import annotations

import os
import platform
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import coverage

from . import __version__
from .coverage_data import normalize_coverage
from .storage import publish_run, runs_root


class UnsupportedCommand(ValueError):
    pass


def _pytest_args(command: list[str]) -> list[str]:
    if not command:
        raise UnsupportedCommand("missing test command; use `covledger run -- pytest ...`")
    first = Path(command[0]).name.lower()
    if first in {"pytest", "pytest.exe"}:
        return command[1:]
    if len(command) >= 3 and first in {"python", "python3", "python.exe", Path(sys.executable).name.lower()}:
        if command[1:3] == ["-m", "pytest"]:
            return command[3:]
    raise UnsupportedCommand(
        "MVP runner supports `pytest ...` or `python -m pytest ...`; arbitrary executors are a later layer"
    )


def _run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"run_{now.strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"


def _write_coveragerc(path: Path, data_file: Path) -> None:
    text = f"""[run]\nbranch = True\nsource = .\ndata_file = {data_file.as_posix()}\nomit =\n    */tests/*\n    tests/*\n    */test_*.py\n    */.venv/*\n    */venv/*\n    */site-packages/*\n    */.tox/*\n    */.nox/*\n    */build/*\n    */dist/*\n    */.covledger/*\n\n[report]\nskip_empty = True\n"""
    path.write_text(text, encoding="utf-8")


def run_pytest(root: Path, command: list[str]) -> dict[str, Any]:
    root = root.resolve()
    pytest_args = _pytest_args(command)
    run_id = _run_id()
    run_dir = runs_root(root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    data_file = run_dir / ".coverage"
    rcfile = run_dir / "coveragerc"
    raw_json = run_dir / "coverage.raw.json"
    _write_coveragerc(rcfile, data_file)

    started = datetime.now(UTC)
    env = os.environ.copy()
    executed = [sys.executable, "-m", "coverage", "run", f"--rcfile={rcfile}", "-m", "pytest", *pytest_args]
    completed = subprocess.run(executed, cwd=root, env=env, check=False)

    cov = coverage.Coverage(config_file=str(rcfile), data_file=str(data_file))
    cov.load()
    cov.json_report(outfile=str(raw_json), pretty_print=True)
    normalized = normalize_coverage(raw_json, project_root=root, run_dir=run_dir)
    finished = datetime.now(UTC)

    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "tool": {"name": "covledger", "version": __version__},
        "created_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "project_root": str(root),
        "command": command,
        "executed_command": executed,
        "exit_code": int(completed.returncode),
        "suite_passed": completed.returncode == 0,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "coverage": normalized,
    }
    publish_run(run_dir, report)
    return report
