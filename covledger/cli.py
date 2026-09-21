"""CovLedger command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .assessment import assessment_for_run
from .diffing import diff_runs
from .gaps import gaps_for_run
from .inferlingo_rules import derive_proofs
from .ledgercore_backend import initialize_covledger
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
    print(f"P{candidate['priority_score']} {candidate['band']} {candidate['function_id']}")
    print(f"{candidate['path']}:{candidate['line']}")
    print()
    print(f"{candidate['gap']['label']}:")
    print()
    print("function:")
    print(f"    {candidate['function']['qualname']}")
    print()
    print("priority:")
    breakdown = candidate["score_breakdown"]
    print(f"    quality      {breakdown['quality']}")
    print(f"    exposure     {breakdown['exposure']}")
    print(f"    regression   {breakdown['regression'] if breakdown['regression'] is not None else 'unavailable'}")
    print(f"    total        {breakdown['priority']}")
    print()
    print("deterministic findings:")
    for finding in candidate["deterministic"].get("finding_ids", []) or ["none"]:
        print(f"    {finding}")
    print()
    print("suggested action:")
    print(f"    {candidate['suggested_action']}")
    print()
    print("repository context:")
    context = candidate["repository_context"]
    print(f"    work mode: {context['work_mode']['name']}")
    print(f"    critical: {context['counts']['critical']}")
    print(f"    high: {context['counts']['high']}")
    print()
    print("queue:")
    print("    covledger findings --min-score 70")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="covledger", description="Python test evidence for coding agents.")
    parser.add_argument("--version", action="version", version=f"covledger {__version__}")
    parser.add_argument("--root", default=".", help="project root (default: current directory)")
    sub = parser.add_subparsers(dest="command_name", required=True)

    init = sub.add_parser("init", help="initialize canonical Ledgercore storage for CovLedger")
    init.add_argument("--external-root", default="../ledger")
    init.add_argument("--project-name")
    init.add_argument("--runs-storage", choices=["external", "user-data", "project"], default="external")
    init.add_argument("--json", action="store_true", dest="json_output")

    run = sub.add_parser("run", help="run pytest through the coverage backend")
    run.add_argument("command", nargs=argparse.REMAINDER, help="pytest command after --")
    run.add_argument("--json", action="store_true", dest="json_output")

    runs = sub.add_parser("runs", help="list or query stored runs")
    runs.add_argument("selector", nargs="?", help="run id or latest")
    runs.add_argument("action", nargs="?", choices=["gaps", "file"])
    runs.add_argument("path", nargs="?")
    runs.add_argument("--limit", type=int, default=10)
    runs.add_argument("--details", action="store_true")
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
    quality.add_argument("--include-generated", action="store_true")
    quality.add_argument("--top", type=int)
    quality.add_argument("--json", action="store_true", dest="json_output")

    overview = sub.add_parser("overview", help="show the repository risk overview for a run")
    overview.add_argument("selector", nargs="?", default="latest")
    overview.add_argument("--json", action="store_true", dest="json_output")

    findings = sub.add_parser("findings", help="list ranked run-backed hotspots")
    findings.add_argument("selector", nargs="?", default="latest")
    findings.add_argument("--limit", type=int, default=20)
    findings.add_argument("--min-score", type=int, default=0)
    findings.add_argument("--band", choices=["critical", "high", "medium", "low"])
    findings.add_argument("--file")
    findings.add_argument("--kind")
    findings.add_argument("--rule")
    findings.add_argument("--semantic", action="store_true")
    findings.add_argument("--top", type=int)
    findings.add_argument("--json", action="store_true", dest="json_output")

    inspect = sub.add_parser("inspect", help="inspect a stable function, finding, or gap identifier")
    inspect.add_argument("query_id")
    inspect.add_argument("--run", dest="selector", default="latest")
    inspect.add_argument("--semantic", action="store_true")
    inspect.add_argument("--json", action="store_true", dest="json_output")

    next_parser = sub.add_parser("next", help="select the next uncovered behavior to inspect")
    next_parser.add_argument("selector", nargs="?", default="latest")
    next_parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _strip_separator(command: list[str]) -> list[str]:
    return command[1:] if command[:1] == ["--"] else command

def _handle_init(root: Path, args: argparse.Namespace) -> int:
    initialized = initialize_covledger(
        root,
        project_name=args.project_name,
        runs_storage=args.runs_storage,
        external_root=args.external_root,
    )
    runs_mount = initialized.layout.mounts["runs"]
    cache_mount = initialized.layout.mounts["cache"]
    result = {
        "project_root": str(initialized.layout.project_root),
        "project_uuid": initialized.manifest.project_uuid,
        "project_name": initialized.manifest.project_name,
        "manifest": str(initialized.layout.manifest_path),
        "config": str(initialized.layout.tool_config_path),
        "runs": str(runs_mount.path),
        "cache": str(cache_mount.path),
        "runs_storage": runs_mount.storage,
        "external_root": str(runs_mount.root) if runs_mount.root is not None else None,
    }
    if args.json_output:
        _json(result)
    else:
        print("initialized covledger")
        print(f"project: {result['project_name']} ({result['project_uuid']})")
        print(f"config: {result['config']}")
        print(f"runs: {result['runs']}")
        print(f"cache: {result['cache']}")
    return 0



def _handle_run(root: Path, args: argparse.Namespace) -> int:
    report = run_pytest(root, _strip_separator(args.command))
    if args.json_output:
        _json(report)
    else:
        _print_summary(report)
    return int(report["exit_code"])



def _run_details(root: Path, run_id: str) -> dict[str, Any]:
    run = load_run(root, run_id)
    totals = run.get("coverage", {}).get("totals") or {}
    try:
        assessment = assessment_for_run(root, run)
        counts = assessment["summary"]["counts"]
        top = assessment["hotspots"][0]["score"]["priority"] if assessment["hotspots"] else None
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        counts = {"high": None, "critical": None}
        top = None
    return {
        "run_id": run_id,
        "suite_passed": run.get("suite_passed", run.get("suite", {}).get("passed", False)),
        "line_percent": totals.get("line_percent"),
        "branch_percent": totals.get("branch_percent"),
        "gap_count": len(gaps_for_run(run, _run_dir(root, run))),
        "high_count": counts.get("high"),
        "critical_count": counts.get("critical"),
        "top_priority_score": top,
    }



def _print_run_table(rows: list[dict[str, Any]]) -> None:
    print("RUN       SUITE  LINES   BRANCH  GAPS  HIGH  CRIT  TOP")
    for row in rows:
        run = row["run_id"][:8]
        suite = "pass" if row["suite_passed"] else "fail"
        lines = f"{row['line_percent']:.1f}%" if row["line_percent"] is not None else "?"
        branch = f"{row['branch_percent']:.1f}%" if row["branch_percent"] is not None else "?"
        values = [
            row["gap_count"],
            row["high_count"] if row["high_count"] is not None else "?",
            row["critical_count"] if row["critical_count"] is not None else "?",
            row["top_priority_score"] if row["top_priority_score"] is not None else "?",
        ]
        print(
            f"{run:<9} {suite:<6} {lines:>6} {branch:>7} "
            f"{values[0]:>5} {values[1]:>5} {values[2]:>5} {values[3]:>5}"
        )


def _handle_runs(root: Path, args: argparse.Namespace) -> int:
    if args.selector is None:
        run_ids = list_run_ids(root)
        if args.details:
            rows = [_run_details(root, run_id) for run_id in run_ids]
            result = {"schema_version": 1, "runs": rows}
            if args.json_output:
                _json(result)
            else:
                _print_run_table(rows)
            return 0
        if args.json_output:
            _json(run_ids)
        else:
            _print_run_table([_run_details(root, run_id) for run_id in run_ids] if run_ids else [])
        return 0
    run = load_run(root, args.selector)
    if args.action is None:
        if args.details:
            result = _run_details(root, run["run_id"])
            if args.json_output:
                _json(result)
            else:
                _print_run_table([result])
        elif args.json_output:
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



def _handle_diff(root: Path, args: argparse.Namespace) -> int:
    result = diff_runs(load_run(root, args.older), load_run(root, args.newer))
    if args.json_output:
        _json(result)
    else:
        _print_diff(result)
    return 0



def _handle_quality(root: Path, args: argparse.Namespace) -> int:
    if not 0.0 <= args.threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    target = (root / args.target).resolve() if not Path(args.target).is_absolute() else Path(args.target).resolve()
    report = quality_report(
        target,
        root=root,
        semantic=args.semantic,
        threshold=args.threshold,
        refresh=args.refresh,
        max_functions=args.max_functions if args.max_functions is not None else args.top,
        include_generated=args.include_generated,
    )
    if args.json_output:
        _json(report)
    else:
        print(f"functions: {report['function_count']}")
        ranked = sorted(
            report["functions"],
            key=lambda item: (-item.get("quality_score", 0), item["path"], item["line"], item["qualname"]),
        )
        for item in ranked:
            if not item["exact_findings"] and not item.get("semantic_findings", []):
                continue
            print(
                f"Q={item.get('quality_score', 0):>2}  {item['function_id']} "
                f"{item['path']}:{item['line']}  {item['qualname']}"
            )
            for finding in item["findings"]:
                print(f"        {finding['finding_id']}  {finding['id']}")
            semantic = item.get("semantic", {}).get("judgments", {})
            for finding in item.get("semantic_findings", []):
                print(f"        semantic {semantic[finding]:.2f}  {finding}")
    return 0



def _handle_next(root: Path, args: argparse.Namespace) -> int:
    result = next_query(root, args.selector)
    if args.json_output:
        _json(result)
    else:
        _print_next(result)
    return 0



def _load_assessment(root: Path, selector: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run = load_run(root, selector)
    return run, assessment_for_run(root, run)



def _handle_overview(root: Path, args: argparse.Namespace) -> int:
    run, assessment = _load_assessment(root, args.selector)
    summary = assessment["summary"]
    result = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "suite": run.get("suite", {}),
        "scope": {
            "functions": summary["functions"],
            "functions_with_findings": summary["functions_with_findings"],
            "functions_with_gaps": summary["functions_with_gaps"],
            "stale_current_files": summary["stale_current_files"],
        },
        "attention": summary["counts"],
        "work_mode": summary["work_mode"],
        "risk_map": summary["risk_map"],
        "top_hotspots": assessment["hotspots"][:10],
    }
    if args.json_output:
        _json(result)
        return 0
    print(f"run: {run['run_id']}")
    suite = run.get("suite", {})
    print(f"suite: {'passed' if suite.get('passed', run.get('suite_passed', False)) else 'failed'}")
    print("scope")
    print(f"  python functions:        {summary['functions']}")
    print(f"  functions with findings: {summary['functions_with_findings']}")
    print(f"  functions with gaps:     {summary['functions_with_gaps']}")
    print(f"  stale current files:     {summary['stale_current_files']}")
    print("attention")
    for band in ("critical", "high", "medium", "low"):
        print(f"  {band}: {summary['counts'][band]}")
    print(f"work mode: {summary['work_mode']['name']}")
    print(f"reason: {', '.join(summary['work_mode']['reasons'])}")
    print("top hotspots")
    for item in assessment["hotspots"][:10]:
        print(
            f"  {item['score']['priority']:>3} {item['score']['band']:<8} "
            f"{item['id']} {item['path']}:{item['line']} {item['qualname']}"
        )
    return 0



def _handle_findings(root: Path, args: argparse.Namespace) -> int:
    _, assessment = _load_assessment(root, args.selector)
    rows = []
    for hotspot in assessment["hotspots"]:
        score = hotspot["score"]
        if score["priority"] < args.min_score:
            continue
        if args.band and score["band"] != args.band:
            continue
        if args.file and hotspot["path"] != args.file:
            continue
        if args.kind and not any(
            gap["gap"]["kind"] == args.kind
            for gap in assessment["gaps"]
            if (gap.get("function") or {}).get("qualname") == hotspot["qualname"]
            and gap.get("path") == hotspot["path"]
        ):
            continue
        if args.rule and not any(
            finding.get("id") == args.rule
            for finding in assessment["findings"]
            if finding.get("function_id") == hotspot["id"]
        ):
            continue
        row = {
            "function_id": hotspot["id"],
            "path": hotspot["path"],
            "line": hotspot["line"],
            "qualname": hotspot["qualname"],
            "priority_score": score["priority"],
            "band": score["band"],
            "score_breakdown": score,
            "finding_ids": hotspot["finding_ids"],
            "gap_ids": hotspot["gap_ids"],
        }
        rows.append(row)
    limit = args.top if args.top is not None else args.limit
    rows = rows[:limit]
    result = {"schema_version": 1, "run_id": assessment["run_id"], "findings": rows}
    if args.json_output:
        _json(result)
        return 0
    print("P  BAND      ID             LOCATION                              WHY")
    for row in rows:
        reasons = []
        if row["gap_ids"]:
            reasons.append("coverage gap")
        if row["finding_ids"]:
            reasons.append("deterministic findings")
        reason = " + ".join(reasons) or "quality"
        print(
            f"{row['priority_score']:>2} {row['band']:<9} {row['function_id']:<14} "
            f"{row['path']}:{row['line']} {row['qualname']}  {reason}"
        )
    return 0



def _handle_inspect(root: Path, args: argparse.Namespace) -> int:
    query_id = args.query_id
    try:
        run, assessment = _load_assessment(root, args.selector)
    except RunNotFound:
        if args.selector != "latest":
            raise
        report = quality_report(root, root=root, semantic=args.semantic)
        for row in report["functions"]:
            if row.get("function_id") == query_id or any(
                item.get("finding_id") == query_id for item in row.get("findings", [])
            ):
                result = {
                    "schema_version": 1,
                    "query_id": query_id,
                    "run_id": None,
                    "hotspot": row,
                    "highlight": None,
                    "proofs": [],
                }
                if args.json_output:
                    _json(result)
                else:
                    print(f"{query_id}\n{row['path']}:{row['line']}\n{row['qualname']}")
                    print(f"quality: {row['quality_score']} {row['attention_band']}")
                return 0
        raise ValueError(f"unknown identifier: {query_id}") from None
    hotspot = next((item for item in assessment["hotspots"] if item["id"] == query_id), None)
    highlight: dict[str, Any] | None = None
    if hotspot is None:
        finding = next((item for item in assessment["findings"] if item.get("finding_id") == query_id), None)
        gap = next(
            (
                item
                for item in assessment["gaps"]
                if item.get("gap_id") == query_id or item.get("gap", {}).get("gap_id") == query_id
            ),
            None,
        )
        if finding is not None:
            hotspot = next((item for item in assessment["hotspots"] if item["id"] == finding["function_id"]), None)
            highlight = finding
        elif gap is not None:
            hotspot = next((item for item in assessment["hotspots"] if query_id in item["gap_ids"]), None)
            highlight = gap
    if hotspot is None:
        raise ValueError(f"unknown identifier: {query_id}")
    proofs = derive_proofs(assessment, hotspot)
    result = {
        "schema_version": 1,
        "query_id": query_id,
        "run_id": run["run_id"],
        "hotspot": hotspot,
        "highlight": highlight,
        "proofs": proofs,
    }
    if args.json_output:
        _json(result)
        return 0
    print(query_id)
    print(f"{hotspot['path']}:{hotspot['line']}")
    print(hotspot["qualname"])
    score = hotspot["score"]
    print("priority")
    print(f"  score:       {score['priority']} {score['band']}")
    print(f"  quality:     {score['quality']}")
    print(f"  exposure:    {score['exposure']}")
    print(f"  regression:  {score['regression'] if score['regression'] is not None else 'unavailable'}")
    print(f"  bonuses:     {sum(item['points'] for item in score['bonuses'])}")
    print("facts")
    for key in ("line_count", "max_nesting", "decision_count", "return_count", "parameter_count"):
        if key in hotspot["facts"]:
            print(f"  {key}: {hotspot['facts'][key]}")
    print("coverage")
    coverage = hotspot["coverage"]
    print(f"  line:       {coverage['line_percent']:.2f}%")
    print(f"  branch:     {coverage['branch_percent']:.2f}%")
    print(f"  gaps:       {len(hotspot['gap_ids'])}")
    print(f"current source: {hotspot['current_source']}")
    if highlight is not None:
        print(f"highlight: {highlight}")
    return 0




HANDLERS = {
    "init": _handle_init,
    "run": _handle_run,
    "runs": _handle_runs,
    "diff": _handle_diff,
    "overview": _handle_overview,
    "findings": _handle_findings,
    "inspect": _handle_inspect,
    "quality": _handle_quality,
    "next": _handle_next,
}



def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(args.root)
    try:
        return HANDLERS[args.command_name](root, args)
    except (RunNotFound, UnsupportedCommand, FileNotFoundError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
