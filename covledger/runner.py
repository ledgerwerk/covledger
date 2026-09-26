"""Execute pytest with temporary Coverage.py files and cache one current analysis."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import coverage
from ledgercore import uuid7

from .analysis_scope import AnalysisScope, analysis_scope_from_config
from .assessment import build_assessment
from .coverage_data import normalize_coverage, unavailable_coverage
from .quality import quality_document_from_sources
from .source import discover_python_files
from .storage import (
    analysis_config_sha256,
    clear_current_analysis,
    create_work_dir,
    load_covledger_config,
    load_current_analysis,
    publish_current_analysis,
)


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


def _write_coveragerc(path: Path, data_file: Path, *, branch: bool) -> None:
    text = f"""[run]
branch = {branch}
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


def _load_coverage(
    root: Path,
    data_file: Path,
    raw_json: Path,
    analysis_files: list[Path],
    allowed_paths: frozenset[str],
    analysis_scope: AnalysisScope,
    *,
    branch: bool,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    try:
        cov = coverage.Coverage(data_file=str(data_file), branch=branch, source=[str(root)])
        cov.load()
        cov.json_report(
            morfs=[str(path) for path in analysis_files],
            outfile=str(raw_json),
            pretty_print=True,
        )
        return normalize_coverage(
            raw_json,
            project_root=root,
            allowed_paths=allowed_paths,
            analysis_scope=analysis_scope,
        )
    except Exception as exc:
        return unavailable_coverage(exc), {}


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pytest(root: Path, command: list[str]) -> dict[str, Any]:
    """Run pytest, atomically replace current.json, and always remove temporary work."""
    root = root.resolve()
    pytest_args = _pytest_args(command)
    # A new valid run attempt invalidates the prior analysis before doing work.
    clear_current_analysis(root)
    config = load_covledger_config(root)
    analysis_scope = analysis_scope_from_config(config)
    analysis_files = discover_python_files(root, root=root, scope=analysis_scope)
    if analysis_scope.include and not analysis_files:
        raise ValueError("analysis.include matched no eligible Python source files")
    allowed_paths = frozenset(path.relative_to(root).as_posix() for path in analysis_files)
    coverage_config = config.get("coverage", {})
    branch = coverage_config.get("branch", True) if isinstance(coverage_config, dict) else True
    if not isinstance(branch, bool):
        raise ValueError("invalid coverage.branch value: expected a boolean")
    config_sha256 = analysis_config_sha256(root)
    analysis_id = str(uuid7())
    work_dir = create_work_dir(root, analysis_id)
    data_file = work_dir / ".coverage"
    rcfile = work_dir / "coveragerc"
    raw_json = work_dir / "coverage.json"

    try:
        _write_coveragerc(rcfile, data_file, branch=branch)
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
        completed = subprocess.run(executed, cwd=root, env=os.environ.copy(), check=False)
        coverage_data, coverage_sources = _load_coverage(
            root,
            data_file,
            raw_json,
            analysis_files,
            allowed_paths,
            analysis_scope,
            branch=branch,
        )

        sources = {path.relative_to(root).as_posix(): path for path in analysis_files}
        quality = quality_document_from_sources(sources, root=root)
        source_state: dict[str, dict[str, str]] = {}
        for path in sorted(set(coverage_sources) | set(quality.get("files", {}))):
            current_path = root / path
            source_hash = _source_hash(current_path)
            coverage_hash = coverage_sources.get(path, {}).get("sha256")
            if coverage_hash and coverage_hash != source_hash:
                raise ValueError(f"source changed while running CovLedger analysis: {path}")
            source_state[path] = {"sha256": source_hash}

        if analysis_config_sha256(root) != config_sha256:
            raise ValueError("CovLedger configuration changed while analysis was running; rerun tests")
        scope = {
            "analysis": analysis_scope.to_dict(),
            "config_sha256": config_sha256,
            "files": source_state,
        }
        analysis: dict[str, Any] = {
            "schema_version": 1,
            "analysis_id": analysis_id,
            "suite": {
                "pytest_args": ["pytest", *pytest_args],
                "passed": completed.returncode == 0,
                "exit_code": int(completed.returncode),
            },
            "coverage": coverage_data,
            "scope": scope,
        }
        analysis["assessment"] = build_assessment(root, analysis | {"quality": quality})
        publish_current_analysis(root, analysis)
        return load_current_analysis(root)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
