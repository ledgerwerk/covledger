"""CovLedger's current-analysis command line interface."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .inferlingo_rules import derive_proofs
from .ledgercore_backend import initialize_covledger
from .next_query import next_query
from .quality import quality_report
from .runner import UnsupportedCommand, run_pytest
from .source import read_verified_source
from .storage import (
    StaleAnalysisError,
    cache_root,
    clear_cache,
    load_fresh_current_analysis,
)


def _root(value: str) -> Path:
    return Path(value).resolve()


def _json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _current(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = load_fresh_current_analysis(root)
    assessment = analysis.get("assessment")
    if not isinstance(assessment, dict):
        raise ValueError("current CovLedger analysis is missing its assessment")
    return analysis, assessment


def _handle_init(root: Path, args: argparse.Namespace) -> int:
    initialized = initialize_covledger(root, project_name=args.project_name)
    cache_path = initialized.layout.mounts["cache"].path
    result = {
        "project_root": str(initialized.layout.project_root),
        "project_uuid": initialized.manifest.project_uuid,
        "project_name": initialized.manifest.project_name,
        "manifest": str(initialized.layout.manifest_path),
        "config": str(initialized.layout.tool_config_path),
        "cache": str(cache_path),
        "legacy_runs_path": str(initialized.legacy_runs_path) if initialized.legacy_runs_path else None,
    }
    if args.json_output:
        _json(result)
        return 0
    print("initialized covledger")
    print(f"project: {result['project_name']} ({result['project_uuid']})")
    print(f"config: {result['config']}")
    print(f"cache: {result['cache']}")
    if initialized.legacy_runs_path is not None:
        print("CovLedger no longer uses persistent run storage.")
        print(f"Existing run data was left untouched at: {initialized.legacy_runs_path}")
        print("You may delete it manually when no longer needed.")
    return 0


def _print_run_summary(analysis: dict[str, Any]) -> None:
    suite = analysis["suite"]
    coverage = analysis["coverage"]
    print(f"analysis {analysis['analysis_id']}")
    suite_status = "passed" if suite["passed"] else f"failed ({suite['exit_code']})"
    print(f"suite: {suite_status}")
    if coverage.get("status") != "available":
        print("coverage: unavailable")
        return
    totals = coverage["totals"]
    print(f"lines: {totals['line_percent']:.2f}% ({totals['covered_lines']}/{totals['statements']})")
    if totals["branches"]:
        print(f"branches: {totals['branch_percent']:.2f}% ({totals['covered_branches']}/{totals['branches']})")
    else:
        print("branches: n/a (0 obligations)")


def _handle_run(root: Path, args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    analysis = run_pytest(root, command)
    if args.json_output:
        _json(analysis)
    else:
        _print_run_summary(analysis)
    return int(analysis["suite"]["exit_code"])


def _overview(analysis: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    summary = assessment["summary"]
    return {
        "schema_version": 1,
        "analysis_id": analysis["analysis_id"],
        "suite": analysis["suite"],
        "coverage": analysis["coverage"],
        "scope": {
            "functions": summary["functions"],
            "functions_with_findings": summary["functions_with_findings"],
            "functions_with_gaps": summary["functions_with_gaps"],
            "source_files": len(analysis.get("scope", {}).get("files", {})),
        },
        "attention": summary["counts"],
        "work_mode": summary["work_mode"],
        "risk_map": summary["risk_map"],
        "top_hotspots": assessment["hotspots"][:10],
    }


def _handle_overview(root: Path, args: argparse.Namespace) -> int:
    analysis, assessment = _current(root)
    result = _overview(analysis, assessment)
    if args.json_output:
        _json(result)
        return 0
    print(f"analysis: {analysis['analysis_id']}")
    print(f"suite: {'passed' if analysis['suite']['passed'] else 'failed'}")
    coverage = analysis["coverage"]
    if coverage.get("status") == "available":
        totals = coverage["totals"]
        print(f"lines: {totals['line_percent']:.2f}%")
        if totals["branches"]:
            print(f"branches: {totals['branch_percent']:.2f}%")
        else:
            print("branches: n/a")
    else:
        print("coverage: unavailable")
    summary = assessment["summary"]
    print("scope")
    print(f"  source files:             {result['scope']['source_files']}")
    print(f"  python functions:         {summary['functions']}")
    print(f"  functions with findings:  {summary['functions_with_findings']}")
    print(f"  functions with gaps:      {summary['functions_with_gaps']}")
    print("attention")
    for band in ("critical", "high", "medium", "low"):
        print(f"  {band}: {summary['counts'][band]}")
    print(f"work mode: {summary['work_mode']['name']}")
    print("top hotspots")
    for item in assessment["hotspots"][:10]:
        print(
            f"  {item['score']['priority']:>3} {item['score']['band']:<8} {item['id']} "
            f"{item['path']}:{item['line']} {item['qualname']}"
        )
    return 0


def _finding_rows(assessment: dict[str, Any], function_id: str) -> list[dict[str, Any]]:
    return [row for row in assessment.get("findings", []) if row.get("function_id") == function_id]


def _gap_rows(assessment: dict[str, Any], hotspot: dict[str, Any]) -> list[dict[str, Any]]:
    gap_ids = set(hotspot.get("gap_ids", []))
    return [
        row
        for row in assessment.get("gaps", [])
        if row.get("gap_id") in gap_ids or row.get("gap", {}).get("gap_id") in gap_ids
    ]


def _finding_description(row: dict[str, Any]) -> str:
    return f"{row.get('finding_id') or row.get('id')}: {row.get('id')}"


def _report_rows(analysis: dict[str, Any], assessment: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for hotspot in assessment.get("hotspots", []):
        findings = _finding_rows(assessment, hotspot["id"])
        gaps = _gap_rows(assessment, hotspot)
        deterministic = [_finding_description(row) for row in findings]
        semantic = list(hotspot.get("semantic_findings", []))
        gap_ids = [row.get("gap_id") or row.get("gap", {}).get("gap_id") for row in gaps]
        actions = []
        for gap in gaps:
            kind = gap.get("gap", {}).get("kind")
            actions.append(
                {
                    "error-path": "test this uncovered error path",
                    "branch": "test both sides of this branch",
                    "line": "add an assertion for this uncovered line",
                }.get(kind, "inspect this coverage gap")
            )
        if deterministic or semantic:
            actions.append("review the listed quality findings")
        if not actions:
            actions.append("inspect the function and add focused behavior tests")
        result.append(
            {
                "analysis_id": analysis["analysis_id"],
                "priority": hotspot["score"]["priority"],
                "band": hotspot["score"]["band"],
                "id": hotspot["id"],
                "finding_ids": [row.get("finding_id") for row in findings if row.get("finding_id")],
                "gap_ids": [item for item in gap_ids if item],
                "path": hotspot["path"],
                "line": hotspot["line"],
                "function": hotspot["qualname"],
                "why": [*deterministic, *semantic, *[row.get("gap", {}).get("label", "coverage gap") for row in gaps]],
                "deterministic_findings": deterministic,
                "semantic_findings": semantic,
                "suggested_action": "; ".join(dict.fromkeys(actions)),
            }
        )
    return result


def _render_markdown(rows: list[dict[str, Any]], analysis: dict[str, Any]) -> str:
    suite = analysis["suite"]
    suite_status = "passed" if suite["passed"] else f"failed (exit {suite['exit_code']})"
    coverage = analysis["coverage"]
    lines = [
        f"# CovLedger report — analysis {analysis['analysis_id']}",
        "",
        f"- Test suite: {suite_status}",
    ]
    if coverage.get("status") == "available":
        totals = coverage["totals"]
        lines.append(f"- Line coverage: {totals['line_percent']:.2f}%")
        lines.append(f"- Branch coverage: {totals['branch_percent']:.2f}%")
    else:
        lines.append("- Coverage: unavailable")
    lines.extend(["", "## Prioritized work", ""])
    if not rows:
        lines.append("No prioritized hotspots in the current analysis.")
    for row in rows:
        lines.extend(
            [
                f"### P{row['priority']} {row['band']} — {row['id']}",
                f"**{row['path']}:{row['line']}** — `{row['function']}`",
                f"- Finding IDs: {', '.join(row['finding_ids']) or 'none'}",
                f"- Gap IDs: {', '.join(row['gap_ids']) or 'none'}",
                f"- Deterministic findings: {', '.join(row['deterministic_findings']) or 'none'}",
                f"- Semantic findings: {', '.join(row['semantic_findings']) or 'none'}",
                f"- Why: {', '.join(row['why']) or 'priority score'}",
                f"- Suggested action: {row['suggested_action']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_csv(rows: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    columns = [
        "analysis_id",
        "priority",
        "band",
        "id",
        "finding_ids",
        "gap_ids",
        "path",
        "line",
        "function",
        "why",
        "deterministic_findings",
        "semantic_findings",
        "suggested_action",
    ]
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            row
            | {
                "finding_ids": "; ".join(row["finding_ids"]),
                "gap_ids": "; ".join(row["gap_ids"]),
                "why": "; ".join(row["why"]),
                "deterministic_findings": "; ".join(row["deterministic_findings"]),
                "semantic_findings": "; ".join(row["semantic_findings"]),
            }
        )
    return stream.getvalue()


def _handle_report(root: Path, args: argparse.Namespace) -> int:
    analysis, assessment = _current(root)
    rows = _report_rows(analysis, assessment)
    text = _render_markdown(rows, analysis) if args.format == "md" else _render_csv(rows)
    if args.output:
        output_path = Path(args.output).expanduser()
        if not output_path.is_absolute():
            output_path = Path.cwd() / output_path
        output_path.write_text(text, encoding="utf-8", newline="")
    else:
        sys.stdout.write(text)
    return 0


def _handle_findings(root: Path, args: argparse.Namespace) -> int:
    analysis, assessment = _current(root)
    matching: list[dict[str, Any]] = []
    for hotspot in assessment.get("hotspots", []):
        score = hotspot["score"]
        if score["priority"] < args.min_score:
            continue
        if args.band and score["band"] != args.band:
            continue
        if args.file and hotspot["path"] != args.file:
            continue
        gaps = _gap_rows(assessment, hotspot)
        findings = _finding_rows(assessment, hotspot["id"])
        semantic_findings = hotspot.get("semantic_findings", [])
        if args.kind and not any(row.get("gap", {}).get("kind") == args.kind for row in gaps):
            continue
        if args.rule and not any(row.get("id") == args.rule or row.get("finding_id") == args.rule for row in findings):
            if args.rule not in semantic_findings:
                continue
        if args.semantic and not semantic_findings:
            continue
        matching.append(
            {
                "function_id": hotspot["id"],
                "path": hotspot["path"],
                "line": hotspot["line"],
                "qualname": hotspot["qualname"],
                "priority_score": score["priority"],
                "band": score["band"],
                "score_breakdown": score,
                "finding_ids": hotspot["finding_ids"],
                "gap_ids": hotspot["gap_ids"],
                "semantic_findings": semantic_findings,
            }
        )
    limit = args.top if args.top is not None else args.limit
    matching = matching[:limit]
    result = {"schema_version": 1, "analysis_id": analysis["analysis_id"], "findings": matching}
    if args.json_output:
        _json(result)
        return 0
    print("P  BAND      ID             LOCATION                              WHY")
    for row in matching:
        why = []
        if row["gap_ids"]:
            why.append("coverage gap")
        if row["finding_ids"]:
            why.append("deterministic findings")
        if row["semantic_findings"]:
            why.append("semantic findings")
        print(
            f"{row['priority_score']:>2} {row['band']:<9} {row['function_id']:<14} "
            f"{row['path']}:{row['line']} {row['qualname']}  {' + '.join(why) or 'quality'}"
        )
    return 0


def _handle_next(root: Path, args: argparse.Namespace) -> int:
    result = next_query(root)
    if args.json_output:
        _json(result)
        return 0
    if result["status"] == "blocked":
        reason = result["reason"]
        if reason == "suite-failed":
            print("current test suite failed; no next coverage target selected")
        elif reason == "coverage-unavailable":
            print("coverage is unavailable; no next target can be selected")
        else:
            print(f"next blocked: {reason}")
        return 0
    candidate = result.get("candidate")
    if candidate is None:
        print("No unresolved coverage candidate.")
        return 0
    print(f"P{candidate['priority_score']} {candidate['band']} {candidate['function_id']}")
    print(f"{candidate['path']}:{candidate['line']}")
    print(f"function: {candidate['function']['qualname']}")
    print(f"why: {', '.join(candidate['why'])}")
    print(f"suggested action: {candidate['suggested_action']}")
    return 0


def _handle_inspect(root: Path, args: argparse.Namespace) -> int:
    analysis, assessment = _current(root)
    query_id = args.query_id
    hotspot = next((item for item in assessment.get("hotspots", []) if item.get("id") == query_id), None)
    highlight: dict[str, Any] | None = None
    path: str | None = None
    line: int | None = None
    if hotspot is not None:
        path, line = hotspot["path"], hotspot["line"]
    if hotspot is None:
        finding = next(
            (row for row in assessment.get("findings", []) if query_id in {row.get("finding_id"), row.get("id")}),
            None,
        )
        gap = next(
            (
                row
                for row in assessment.get("gaps", [])
                if query_id in {row.get("gap_id"), row.get("gap", {}).get("gap_id")}
            ),
            None,
        )
        if finding is not None:
            highlight = finding
            hotspot = next(
                (item for item in assessment.get("hotspots", []) if item["id"] == finding.get("function_id")),
                None,
            )
            path, line = finding["path"], finding.get("line")
        elif gap is not None:
            highlight = gap
            path, line = gap["path"], gap["line"]
            function = gap.get("function")
            if function:
                hotspot = next(
                    (
                        item
                        for item in assessment.get("hotspots", [])
                        if item["path"] == path and item["qualname"] == function.get("qualname")
                    ),
                    None,
                )
        else:
            raise ValueError(f"unknown identifier: {query_id}")

    assert path is not None and line is not None
    expected = analysis.get("scope", {}).get("files", {}).get(path, {}).get("sha256")
    if not expected:
        raise StaleAnalysisError([path])
    source = read_verified_source(root, path, expected)
    lines = source.splitlines()
    excerpts = [
        {"line": number, "text": lines[number - 1] if 0 < number <= len(lines) else ""}
        for number in range(max(1, line - 3), min(len(lines), line + 3) + 1)
    ]
    result = {
        "schema_version": 1,
        "analysis_id": analysis["analysis_id"],
        "query_id": query_id,
        "hotspot": hotspot,
        "highlight": highlight,
        "proofs": derive_proofs(assessment, hotspot) if hotspot is not None else [],
        "source_excerpt": excerpts,
    }
    if args.json_output:
        _json(result)
        return 0
    print(f"{query_id}\n{path}:{line}")
    if hotspot is not None:
        print(hotspot["qualname"])
        print(f"priority: {hotspot['score']['priority']} {hotspot['score']['band']}")
    for excerpt in excerpts:
        print(f"{excerpt['line']:>5}: {excerpt['text']}")
    if highlight is not None:
        print(f"highlight: {highlight}")
    return 0


def _handle_quality(root: Path, args: argparse.Namespace) -> int:
    if args.threshold is not None and not 0.0 <= args.threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    target = (root / args.target).resolve() if not Path(args.target).is_absolute() else Path(args.target).resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError("quality target must be inside the project root")
    report = quality_report(
        target,
        root=root,
        semantic=args.semantic,
        threshold=args.threshold,
        refresh=args.refresh,
        max_functions=args.max_functions if args.max_functions is not None else args.top,
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


def _handle_cache_path(root: Path, args: argparse.Namespace) -> int:
    del args
    print(cache_root(root))
    return 0


def _handle_cache_clear(root: Path, args: argparse.Namespace) -> int:
    del args
    clear_cache(root)
    print("CovLedger cache cleared; LedgerCore ownership binding preserved.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="covledger", description="Python test evidence and current cached analysis.")
    parser.add_argument("--version", action="version", version=f"covledger {__version__}")
    parser.add_argument("--root", default=".", help="project root (default: current directory)")
    sub = parser.add_subparsers(dest="command_name", required=True)

    init = sub.add_parser("init", help="initialize CovLedger project config and checkout cache")
    init.add_argument("--project-name")
    init.add_argument("--json", action="store_true", dest="json_output")

    run = sub.add_parser("run", help="run pytest and replace the current cached analysis")
    run.add_argument("command", nargs=argparse.REMAINDER, help="pytest command after --")
    run.add_argument("--json", action="store_true", dest="json_output")

    quality = sub.add_parser("quality", help="analyze current source with optional semantic judgments")
    quality.add_argument("target", nargs="?", default=".")
    quality.add_argument("--semantic", action="store_true")
    quality.add_argument("--threshold", type=float)
    quality.add_argument("--refresh", action="store_true")
    quality.add_argument("--max-functions", type=int)
    quality.add_argument("--top", type=int)
    quality.add_argument("--json", action="store_true", dest="json_output")

    overview = sub.add_parser("overview", help="show the fresh current-analysis overview")
    overview.add_argument("--json", action="store_true", dest="json_output")

    findings = sub.add_parser("findings", help="list ranked findings in the current analysis")
    findings.add_argument("--limit", type=int, default=20)
    findings.add_argument("--min-score", type=int, default=0)
    findings.add_argument("--band", choices=["critical", "high", "medium", "low"])
    findings.add_argument("--file")
    findings.add_argument("--kind")
    findings.add_argument("--rule")
    findings.add_argument("--semantic", action="store_true")
    findings.add_argument("--top", type=int)
    findings.add_argument("--json", action="store_true", dest="json_output")

    inspect = sub.add_parser("inspect", help="inspect a function, finding, or gap in current source")
    inspect.add_argument("query_id")
    inspect.add_argument("--json", action="store_true", dest="json_output")

    next_parser = sub.add_parser("next", help="select the next uncovered behavior from current analysis")
    next_parser.add_argument("--json", action="store_true", dest="json_output")

    report = sub.add_parser("report", help="export a report for the current analysis")
    report.add_argument("--format", choices=["md", "csv"], default="md")
    report.add_argument("--output", help="write to this explicitly requested file; defaults to stdout")

    cache = sub.add_parser("cache", help="inspect or clear CovLedger's disposable LedgerCore cache")
    cache_sub = cache.add_subparsers(dest="cache_action", required=True)
    cache_sub.add_parser("path", help="print LedgerCore's resolved cache mount")
    cache_sub.add_parser("clear", help="remove CovLedger cache children, preserving LedgerCore binding")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(args.root)
    try:
        if args.command_name == "init":
            return _handle_init(root, args)
        if args.command_name == "run":
            return _handle_run(root, args)
        if args.command_name == "quality":
            return _handle_quality(root, args)
        if args.command_name == "overview":
            return _handle_overview(root, args)
        if args.command_name == "findings":
            return _handle_findings(root, args)
        if args.command_name == "inspect":
            return _handle_inspect(root, args)
        if args.command_name == "next":
            return _handle_next(root, args)
        if args.command_name == "report":
            return _handle_report(root, args)
        if args.command_name == "cache" and args.cache_action == "path":
            return _handle_cache_path(root, args)
        if args.command_name == "cache" and args.cache_action == "clear":
            return _handle_cache_clear(root, args)
        raise ValueError(f"unsupported command: {args.command_name}")
    except (UnsupportedCommand, FileNotFoundError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
