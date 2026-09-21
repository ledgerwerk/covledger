"""Source-identity-aware coverage diffs."""

from __future__ import annotations

from typing import Any


def _totals(run: dict[str, Any]) -> dict[str, Any] | None:
    coverage = run.get("coverage", {})
    return coverage.get("totals")


def diff_runs(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_files = old.get("coverage", {}).get("files", {})
    new_files = new.get("coverage", {}).get("files", {})
    paths = sorted(set(old_files) | set(new_files))
    files: dict[str, Any] = {}
    source_changed = False
    for path in paths:
        before = old_files.get(path, {"source_sha256": None, "missing_lines": [], "missing_branches": []})
        after = new_files.get(path, {"source_sha256": None, "missing_lines": [], "missing_branches": []})
        before_hash = before.get("source_sha256")
        after_hash = after.get("source_sha256")
        if before_hash != after_hash:
            source_changed = True
            files[path] = {
                "source_changed": True,
                "before": {
                    "source_sha256": before_hash,
                    "missing_lines": list(before.get("missing_lines", [])),
                    "missing_branches": list(before.get("missing_branches", [])),
                },
                "after": {
                    "source_sha256": after_hash,
                    "missing_lines": list(after.get("missing_lines", [])),
                    "missing_branches": list(after.get("missing_branches", [])),
                },
            }
            continue
        before_lines = set(before.get("missing_lines", []))
        after_lines = set(after.get("missing_lines", []))
        before_branches = {tuple(item) for item in before.get("missing_branches", [])}
        after_branches = {tuple(item) for item in after.get("missing_branches", [])}
        item = {
            "source_changed": False,
            "newly_covered_lines": sorted(before_lines - after_lines),
            "newly_uncovered_lines": sorted(after_lines - before_lines),
            "newly_covered_branches": [list(value) for value in sorted(before_branches - after_branches)],
            "newly_uncovered_branches": [list(value) for value in sorted(after_branches - before_branches)],
        }
        if any(
            item[key]
            for key in (
                "newly_covered_lines",
                "newly_uncovered_lines",
                "newly_covered_branches",
                "newly_uncovered_branches",
            )
        ):
            files[path] = item
    old_totals = _totals(old) or {}
    new_totals = _totals(new) or {}
    return {
        "schema_version": 1,
        "older": old.get("run_id"),
        "newer": new.get("run_id"),
        "source_changed": source_changed,
        "suite_passed": new.get("suite_passed", new.get("suite", {}).get("passed")),
        "line_percent_before": old_totals.get("line_percent"),
        "line_percent_after": new_totals.get("line_percent"),
        "branch_percent_before": old_totals.get("branch_percent"),
        "branch_percent_after": new_totals.get("branch_percent"),
        "files": files,
    }
