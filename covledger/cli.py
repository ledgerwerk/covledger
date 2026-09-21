"""CovLedger command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .quality import quality_report
from .runner import UnsupportedCommand, run_pytest
from .storage import RunNotFound, list_run_ids, load_run, resolve_run_id, runs_root


def _root(value: str) -> Path:
    return Path(value).resolve()


def _json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _totals(run: dict[str, Any]) -> dict[str, Any]:
    return run["coverage"]["totals"]


def _print_summary(run: dict[str, Any]) -> None:
    totals = _totals(run)
    status = "passed" if run["suite_passed"] else f"failed ({run['exit_code']})"
    print(f"run {run['run_id']}")
    print(f"suite: {status}")
    print(f"command: {' '.join(run['command'])}")
    print()
    print(f"Lines     {totals['line_percent']:6.2f}% ({totals['covered_lines']}/{totals['statements']})")
    if totals["branches"]:
        print(f"Branches  {totals['branch_percent']:6.2f}% ({totals['covered_branches']}/{totals['branches']})")
    else:
        print("Branches  n/a (0 obligations)")


def _gaps(run: dict[str, Any], limit: int) -> list[tuple[str, dict[str, Any]]]:
    files = run["coverage"]["files"]
    rows = [
        (path, item)
        for path, item in files.items()
        if item["missing_lines"] or item["missing_branches"]
    ]
    rows.sort(key=lambda pair: (-len(pair[1]["missing_branches"]), -len(pair[1]["missing_lines"]), pair[0]))
    return rows[:limit]


def _print_gaps(run: dict[str, Any], limit: int) -> None:
    rows = _gaps(run, limit)
    if not rows:
        print("No unresolved line or branch gaps.")
        return
    for path, item in rows:
        print(path)
        print(f"  missing lines: {item['missing_lines'] or 'none'}")
        print(f"  missing branches: {item['missing_branches'] or 'none'}")


def _print_file(root: Path, selector: str, run: dict[str, Any], path: str) -> None:
    item = run["coverage"]["files"].get(path)
    if item is None:
        raise ValueError(f"file not measured in run: {path}")
    print(path)
    print(f"  lines: {item['line_percent']:.2f}% ({item['covered_lines']}/{item['statements']})")
    print(f"  branches: {item['branch_percent']:.2f}% ({item['covered_branches']}/{item['branches']})")
    print(f"  missing lines: {item['missing_lines'] or 'none'}")
    print(f"  missing branches: {item['missing_branches'] or 'none'}")

    run_id = resolve_run_id(root, selector)
    snapshot = runs_root(root) / run_id / "sources" / path
    if snapshot.is_file() and item["missing_lines"]:
        lines = snapshot.read_text(encoding="utf-8", errors="replace").splitlines()
        print("  source gaps:")
        for line_number in item["missing_lines"][:20]:
            text = lines[line_number - 1] if 0 < line_number <= len(lines) else ""
            print(f"    {line_number:>5}: {text}")


def _diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_files = old["coverage"]["files"]
    new_files = new["coverage"]["files"]
    paths = sorted(set(old_files) | set(new_files))
    files: dict[str, Any] = {}
    for path in paths:
        before = old_files.get(path, {"missing_lines": [], "missing_branches": []})
        after = new_files.get(path, {"missing_lines": [], "missing_branches": []})
        before_lines = set(before["missing_lines"])
        after_lines = set(after["missing_lines"])
        before_branches = {tuple(item) for item in before["missing_branches"]}
        after_branches = {tuple(item) for item in after["missing_branches"]}
        newly_covered_lines = sorted(before_lines - after_lines)
        newly_uncovered_lines = sorted(after_lines - before_lines)
        newly_covered_branches = sorted(before_branches - after_branches)
        newly_uncovered_branches = sorted(after_branches - before_branches)
        if any((newly_covered_lines, newly_uncovered_lines, newly_covered_branches, newly_uncovered_branches)):
            files[path] = {
                "newly_covered_lines": newly_covered_lines,
                "newly_uncovered_lines": newly_uncovered_lines,
                "newly_covered_branches": [list(item) for item in newly_covered_branches],
                "newly_uncovered_branches": [list(item) for item in newly_uncovered_branches],
            }
    return {
        "older": old["run_id"],
        "newer": new["run_id"],
        "suite_passed": new["suite_passed"],
        "line_percent_before": _totals(old)["line_percent"],
        "line_percent_after": _totals(new)["line_percent"],
        "branch_percent_before": _totals(old)["branch_percent"],
        "branch_percent_after": _totals(new)["branch_percent"],
        "files": files,
    }


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
            return int(report["exit_code"] != 0)

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
                if args.json_output:
                    _json({"run_id": run["run_id"], "gaps": dict(_gaps(run, args.limit))})
                else:
                    _print_gaps(run, args.limit)
                return 0
            if args.action == "file":
                if not args.path:
                    raise ValueError("runs <selector> file requires a path")
                if args.json_output:
                    item = run["coverage"]["files"].get(args.path)
                    if item is None:
                        raise ValueError(f"file not measured in run: {args.path}")
                    _json({"run_id": run["run_id"], "path": args.path, "coverage": item})
                else:
                    _print_file(root, args.selector, run, args.path)
                return 0

        if args.command_name == "diff":
            result = _diff(load_run(root, args.older), load_run(root, args.newer))
            if args.json_output:
                _json(result)
            else:
                print(f"{result['older']} -> {result['newer']}")
                print(f"lines: {result['line_percent_before']:.2f}% -> {result['line_percent_after']:.2f}%")
                print(f"branches: {result['branch_percent_before']:.2f}% -> {result['branch_percent_after']:.2f}%")
                for path, item in result["files"].items():
                    print(path)
                    if item["newly_covered_lines"]:
                        print(f"  + covered lines {item['newly_covered_lines']}")
                    if item["newly_uncovered_lines"]:
                        print(f"  - uncovered lines {item['newly_uncovered_lines']}")
                    if item["newly_covered_branches"]:
                        print(f"  + covered branches {item['newly_covered_branches']}")
                    if item["newly_uncovered_branches"]:
                        print(f"  - uncovered branches {item['newly_uncovered_branches']}")
            return 0

        if args.command_name == "quality":
            if not 0.0 <= args.threshold <= 1.0:
                raise ValueError("threshold must be between 0 and 1")
            target = (root / args.target).resolve() if not Path(args.target).is_absolute() else Path(args.target).resolve()
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
                    exact = item["exact_findings"]
                    semantic_findings = item.get("semantic_findings", [])
                    if not exact and not semantic_findings:
                        continue
                    print(f"{item['path']}:{item['line']}  {item['qualname']}")
                    for finding in exact:
                        print(f"  exact     {finding}")
                    semantic = item.get("semantic", {}).get("judgments", {})
                    for finding in semantic_findings:
                        print(f"  semantic  {semantic[finding]:.2f}  {finding}")
            return 0

    except (RunNotFound, UnsupportedCommand, FileNotFoundError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # PyJev/network errors are surfaced without hiding their cause.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2
