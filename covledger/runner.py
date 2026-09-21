"""Execute pytest through Coverage.py and publish immutable evidence."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import coverage
from ledgercore import write_json

from . import __version__
from .assessment import build_assessment
from .coverage_data import normalize_coverage, unavailable_coverage
from .quality import quality_document_from_sources
from .storage import new_run_id, publish_staged_run, stage_run


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
        "runner supports `pytest ...` or `python -m pytest ...`; arbitrary executors are a later layer"
    )


def _write_coveragerc(path: Path, data_file: Path) -> None:
    text = f"""[run]
branch = True
source = .
data_file = {data_file.as_posix()}
omit =
    */tests/*
    tests/*
    */test_*.py
    */.venv/*
    */venv/*
    */site-packages/*
    */.tox/*
    */.nox/*
    */build/*
    */dist/*
    */.covledger/*
    */.ledger/*

[report]
skip_empty = True
"""
    path.write_text(text, encoding="utf-8")


def _load_coverage(stage: Path, root: Path, data_file: Path, raw_json: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        cov = coverage.Coverage(data_file=str(data_file), branch=True, source=[str(root)])
        cov.load()
        cov.json_report(outfile=str(raw_json), pretty_print=True)
        return normalize_coverage(raw_json, project_root=root, run_dir=stage)
    except Exception as exc:
        return unavailable_coverage(exc), {"schema_version": 1, "files": {}}


def run_pytest(root: Path, command: list[str]) -> dict[str, Any]:
    root = root.resolve()
    pytest_args = _pytest_args(command)
    started = time.monotonic()
    run_id = new_run_id()
    stage = stage_run(root, run_id)
    raw_dir = stage / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    data_file = raw_dir / ".coverage"
    rcfile = raw_dir / "coveragerc"
    raw_json = raw_dir / "coverage.json"
    _write_coveragerc(rcfile, data_file)

    env = os.environ.copy()
    executed = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        f"--rcfile={rcfile}",
        f"--data-file={data_file}",
        "-m",
        "pytest",
        *pytest_args,
    ]
    completed = subprocess.run(executed, cwd=root, env=env, check=False)
    coverage_data, scope = _load_coverage(stage, root, data_file, raw_json)

    sources = {
        path: stage / entry["snapshot"]
        for path, entry in scope["files"].items()
        if (stage / entry["snapshot"]).is_file()
    }
    quality = quality_document_from_sources(sources, root=stage / "sources")
    metadata: dict[str, Any] = {
        "schema_version": 2,
        "run_id": run_id,
        "tool": {"name": "covledger", "version": __version__},
        "duration_seconds": time.monotonic() - started,
        "suite": {
            "command": command,
            "executed_command": executed,
            "exit_code": int(completed.returncode),
            "passed": completed.returncode == 0,
        },
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "artifacts": {
            "scope": "scope.json",
            "coverage": "coverage.json",
            "quality": "quality.json",
            "assessment": "assessment.json",
            "sources": "sources",
        },
    }
    evidence = dict(metadata)
    evidence.update({"coverage": coverage_data, "quality": quality})
    assessment = build_assessment(root, evidence, stage)
    write_json(stage / "scope.json", scope)
    write_json(stage / "coverage.json", coverage_data)
    write_json(stage / "quality.json", quality)
    write_json(stage / "assessment.json", assessment)
    write_json(stage / "run.json", metadata)
    published = publish_staged_run(stage, run_id)
    result = dict(metadata)
    result.update({"scope": scope, "coverage": coverage_data, "quality": quality})
    result["assessment"] = assessment
    result.update(
        {
            "suite_passed": metadata["suite"]["passed"],
            "exit_code": metadata["suite"]["exit_code"],
            "command": command,
        }
    )
    result["run_dir"] = str(published)
    return result
