"""CovLedger command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .diffing import diff_runs
from .gaps import gaps_for_run
from .next_query import next_query
from .quality import quality_report
from .runner import UnsupportedCommand, run_pytest
from .storage import RunNotFound, list_run_ids, load_run, runs_root


def _root(value: str) -> Path:
    return Path(value).resolve()


def _json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _run_dir(root: Path, run: dict[str, Any]) -> Path:
    return runs_root(root) / run["run_id"]


def _current_status(root: Path, path: str, expected: str | None) -> str:
    current = (root / path).resolve()
    if not current.is_file():
        return "missing"
    digest = hashlib.sha256(current.read_bytes()).hexdigest()
    return "fresh" if expected and digest == expected else "changed"


def _totals(run: dict[str, Any]) -> dict[str, Any] | None:
    return run.get("coverage", {}).get("totals")


def _print_summary(run: dict[str, Any]) -> None:
    suite = run.get("suite", {})
    passed = run.get("suite_passed", suite.get("passed", False))
    exit_code = run.get("exit_code", suite.get("exit_code"))
    status = "passed" if passed else f"failed ({exit_code})"
    print(f"run {run['run_id']}")
    print(f"suite: {status}")
    print(f"command: {' '.join(run.get('command', suite.get('command', [])))}")
    coverage = run.get("coverage", {})
    if coverage.get("status") != "available":
        print("coverage: unavailable")
        return
    totals = coverage["totals"]
    print()
    print(f"Lines     {totals['line_percent']:6.2f}% ({totals['covered_lines']}/{totals['statements']})")
    if totals["branches"]:
        print(f"Branches  {totals['branch_percent']:6.2f}% ({totals['covered_branches']}/{totals['branches']})")
    else:
        print("Branches  n/a (0 obligations)")


def _gaps(root: Path, run: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    return [candidate.to_dict() for candidate in gaps_for_run(run, _run_dir(root, run))[:limit]]


def _print_gaps(root: Path, run: dict[str, Any], limit: int) -> None:
    rows = _gaps(root, run, limit)
    if not rows:
        print("No unresolved line or branch gaps.")
        return
    for row in rows:
        gap = row["gap"]
        print(f"{row['path']}:{row['line']}  {gap['kind']}: {gap['label']}")
        if "from_line" in gap:
            print(f"  {gap['from_line']} -> {gap['to_line']}")


def _file_result(root: Path, run: dict[str, Any], path: str) -> dict[str, Any]:
    item = run.get("coverage", {}).get("files", {}).get(path)
    if item is None:
        raise ValueError(f"file not measured in run: {path}")
    result = {
        "run_id": run["run_id"],
        "path": path,
        "coverage": item,
        "current_file": _current_status(root, path, item.get("source_sha256")),
        "gaps": [candidate.to_dict() for candidate in gaps_for_run(run, _run_dir(root, run)) if candidate.path == path],
    }
    snapshot = _run_dir(root, run) / "sources" / path
    if snapshot.is_file():
        lines = snapshot.read_text(encoding="utf-8", errors="replace").splitlines()
        result["source_excerpts"] = [
            {"line": number, "text": lines[number - 1] if 0 < number <= len(lines) else ""}
            for number in item.get("missing_lines", [])[:20]
        ]
    return result


def _print_file(root: Path, run: dict[str, Any], path: str) -> None:
    result = _file_result(root, run, path)
    item = result["coverage"]
    print(path)
    print(f"  lines: {item['line_percent']:.2f}% ({item['covered_lines']}/{item['statements']})")
    print(f"  branches: {item['branch_percent']:.2f}% ({item['covered_branches']}/{item['branches']})")
    print(f"  missing lines: {item['missing_lines'] or 'none'}")
    print(f"  missing branches: {item['missing_branches'] or 'none'}")
    print(f"  source hash: {item.get('source_sha256')}")
    print(f"  current-file status: {result['current_file']}")
    for excerpt in result.get("source_excerpts", []):
        print(f"    {excerpt['line']:>5}: {excerpt['text']}")


def _print_diff(result: dict[str, Any]) -> None:
    print(f"{result['older']} -> {result['newer']}")
    print(f"lines: {result['line_percent_before']:.2f}% -> {result['line_percent_after']:.2f}%")
    if result["branch_percent_before"] is not None and result["branch_percent_after"] is not None:
        print(f"branches: {result['branch_percent_before']:.2f}% -> {result['branch_percent_after']:.2f}%")
    for path, item in result["files"].items():
        print(path)
        if item.get("source_changed"):
            print("  source changed: obligation-level comparison unavailable")
            continue
        if item["newly_covered_lines"]:
            print(f"  + covered lines {item['newly_covered_lines']}")
        if item["newly_uncovered_lines"]:
            print(f"  - uncovered lines {item['newly_uncovered_lines']}")
        if item["newly_covered_branches"]:
            print(f"  + covered branches {item['newly_covered_branches']}")
        if item["newly_uncovered_branches"]:
            print(f"  - uncovered branches {item['newly_uncovered_branches']}")


def _print_next(result: dict[str, Any]) -> None:
    if result["status"] == "blocked":
        if result["reason"] == "suite-failed":
            print("latest run failed")
            print("coverage may be partial")
            print("no next coverage target selected")
        elif result["reason"] == "stale-run":
            print("latest run is stale for the remaining uncovered candidates")
            print("rerun:")
            print("    covledger run -- pytest -q")
        else:
            print(f"next blocked: {result['reason']}")
        return
    candidate = result.get("candidate")
    if candidate is None:
        print("No unresolved coverage candidate.")
        return
    print(f"{candidate['path']}:{candidate['line']}")
    print()
    if candidate["gap"]["kind"] == "error-path":
        print(f"{candidate['gap']['label']}:")
    else:
        print(f"{candidate['gap']['kind']}: {candidate['gap']['label']}")
    if candidate.get("function"):
        print()
        print("function:")
        print(f"    {candidate['function']['qualname']}")
    findings = candidate["deterministic"]["findings"]
    print()
    print("deterministic findings:")
    if findings:
        for finding in findings:
            print(f"    {finding}")
    else:
        print("    none")
    print()
    print("semantic findings:")
    judgments = candidate["semantic"]["judgments"]
    if judgments:
        for rule, probability in sorted(judgments.items()):
            print(f"    {rule:<24} {float(probability):.2f}")
    else:
        print("    none cached")
    print()
    print("suggested action:")
    print(f"    {candidate['suggested_action']}")
    print()
    print("why:")
    for reason in candidate["why"]:
        print(f"    {reason}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="covledger", description="Python test evidence for coding agents.")
    parser.add_argument("--version", action="version", version=f"covledger {__version__}")
    parser.add_argument("--root", default=".", help="project root (default: current directory)")
    sub = parser.add_subparsers(dest="command_name", required=True)

    run = sub.add_parser("run", help="run pytest through the coverage backend")
    run.add_argument("command", nargs=argparse.REMAINDER, help="pytest command after --")
    run.add_argument("--json", action="store_true", dest="json_output")

    runs = sub.add_parser("runs", help="list or query stored runs")
    runs.add_argument("selector", nargs="?", help="run id or latest")
    runs.add_argument("action", nargs="?", choices=["gaps", "file"])
    runs.add_argument("path", nargs="?")
    runs.add_argument("--limit", type=int, default=10)
    runs.add_argument("--json", action="store_true", dest="json_output")

    diff = sub.add_parser("diff", help="compare two stored runs")
    diff.add_argument("older")
    diff.add_argument("newer")
    diff.add_argument("--json", action="store_true", dest="json_output")

    quality = sub.add_parser("quality", help="inspect deterministic Python facts and optional PyJev judgments")
    quality.add_argument("target", nargs="?", default=".")
    quality.add_argument("--semantic", action="store_true")
    quality.add_argument("--threshold", type=float, default=0.70)
    quality.add_argument("--refresh", action="store_true")
    quality.add_argument("--max-functions", type=int)
    quality.add_argument("--json", action="store_true", dest="json_output")

    next_parser = sub.add_parser("next", help="select the next uncovered behavior to inspect")
    next_parser.add_argument("selector", nargs="?", default="latest")
    next_parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _strip_separator(command: list[str]) -> list[str]:
    return command[1:] if command[:1] == ["--"] else command


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(args.root)
    try:
        if args.command_name == "run":
            report = run_pytest(root, _strip_separator(args.command))
            if args.json_output:
                _json(report)
            else:
                _print_summary(report)
            return int(report["exit_code"])

        if args.command_name == "runs":
            if args.selector is None:
                run_ids = list_run_ids(root)
                if args.json_output:
                    _json(run_ids)
                else:
                    print("\n".join(run_ids) if run_ids else "No CovLedger runs found.")
                return 0
            run = load_run(root, args.selector)
            if args.action is None:
                if args.json_output:
                    _json(run)
                else:
                    _print_summary(run)
                return 0
            if args.action == "gaps":
                result = {"run_id": run["run_id"], "gaps": _gaps(root, run, args.limit)}
                if args.json_output:
                    _json(result)
                else:
                    _print_gaps(root, run, args.limit)
                return 0
            if not args.path:
                raise ValueError("runs <selector> file requires a path")
            result = _file_result(root, run, args.path)
            if args.json_output:
                _json(result)
            else:
                _print_file(root, run, args.path)
            return 0

        if args.command_name == "diff":
            result = diff_runs(load_run(root, args.older), load_run(root, args.newer))
            if args.json_output:
                _json(result)
            else:
                _print_diff(result)
            return 0

        if args.command_name == "quality":
            if not 0.0 <= args.threshold <= 1.0:
                raise ValueError("threshold must be between 0 and 1")
            target = (
                (root / args.target).resolve()
                if not Path(args.target).is_absolute()
                else Path(args.target).resolve()
            )
            report = quality_report(
                target,
                root=root,
                semantic=args.semantic,
                threshold=args.threshold,
                refresh=args.refresh,
                max_functions=args.max_functions,
            )
            if args.json_output:
                _json(report)
            else:
                print(f"functions: {report['function_count']}")
                for item in report["functions"]:
                    if not item["exact_findings"] and not item.get("semantic_findings", []):
                        continue
                    print(f"{item['path']}:{item['line']}  {item['qualname']}")
                    for finding in item["exact_findings"]:
                        print(f"  exact     {finding}")
                    semantic = item.get("semantic", {}).get("judgments", {})
                    for finding in item.get("semantic_findings", []):
                        print(f"  semantic  {semantic[finding]:.2f}  {finding}")
            return 0

        if args.command_name == "next":
            result = next_query(root, args.selector)
            if args.json_output:
                _json(result)
            else:
                _print_next(result)
            return 0
    except (RunNotFound, UnsupportedCommand, FileNotFoundError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2
