"""Python source discovery and deterministic AST facts."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        ".covledger",
        "build",
        "dist",
        "node_modules",
    }
)


@dataclass(frozen=True, slots=True)
class FunctionFacts:
    path: str
    qualname: str
    line: int
    end_line: int
    source: str
    line_count: int
    parameter_count: int
    max_nesting: int
    return_count: int
    decision_count: int
    bare_except: bool
    broad_except: bool
    mutable_default: bool
    eval_exec_calls: tuple[str, ...]

    @property
    def exact_findings(self) -> tuple[str, ...]:
        findings: list[str] = []
        if self.line_count >= 50:
            findings.append("long-function")
        if self.parameter_count >= 6:
            findings.append("long-parameter-list")
        if self.max_nesting >= 4:
            findings.append("deep-nesting")
        if self.return_count >= 6:
            findings.append("many-return-paths")
        if self.decision_count >= 10:
            findings.append("many-decisions")
        if self.bare_except:
            findings.append("bare-except")
        if self.broad_except:
            findings.append("broad-except")
        if self.mutable_default:
            findings.append("mutable-default")
        if self.eval_exec_calls:
            findings.append("dynamic-code-execution")
        return tuple(findings)


def discover_python_files(target: Path) -> list[Path]:
    target = target.resolve()
    if not target.exists():
        raise ValueError(f"target does not exist: {target}")
    if target.is_file():
        if target.suffix != ".py":
            raise ValueError(f"target file is not Python: {target}")
        return [target]

    files: list[Path] = []
    for path in target.rglob("*.py"):
        relative = path.relative_to(target)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        # Product-quality mode intentionally ignores conventional tests by default.
        if relative.parts and (relative.parts[0] == "tests" or path.name.startswith("test_")):
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.as_posix())


class _MetricVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.depth = 0
        self.max_depth = 0
        self.return_count = 0
        self.decision_count = 0
        self.bare_except = False
        self.broad_except = False
        self.eval_exec: set[str] = set()

    def _nested(self, node: ast.AST) -> None:
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        self.generic_visit(node)
        self.depth -= 1

    def visit_If(self, node: ast.If) -> None:
        self.decision_count += 1
        self._nested(node)

    def visit_For(self, node: ast.For) -> None:
        self.decision_count += 1
        self._nested(node)

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self.decision_count += 1
        self._nested(node)

    def visit_Try(self, node: ast.Try) -> None:
        self._nested(node)

    def visit_With(self, node: ast.With) -> None:
        self._nested(node)

    visit_AsyncWith = visit_With

    def visit_Match(self, node: ast.Match) -> None:
        self.decision_count += len(node.cases)
        self._nested(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.decision_count += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.decision_count += max(len(node.values) - 1, 1)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self.return_count += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.bare_except = True
        elif isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
            self.broad_except = True
        self._nested(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
            self.eval_exec.add(node.func.id)
        self.generic_visit(node)

    # Nested definitions are separate units; their internals must not inflate
    # the enclosing function's metrics.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None


class _FunctionCollector(ast.NodeVisitor):
    def __init__(self, path: Path, source: str, display_path: str) -> None:
        self.path = path
        self.source_lines = source.splitlines(keepends=True)
        self.display_path = display_path
        self.scope: list[str] = []
        self.functions: list[FunctionFacts] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        for child in node.body:
            self.visit(child)
        self.scope.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        decorators = [item.lineno for item in node.decorator_list]
        start = min([node.lineno, *decorators])
        end = node.end_lineno or node.lineno
        text = "".join(self.source_lines[start - 1 : end])
        metrics = _MetricVisitor()
        for child in node.body:
            metrics.visit(child)
        args = node.args
        parameter_count = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
        parameter_count += int(args.vararg is not None) + int(args.kwarg is not None)
        defaults: Iterable[ast.expr] = [*args.defaults, *(value for value in args.kw_defaults if value is not None)]
        mutable_default = any(isinstance(value, (ast.List, ast.Dict, ast.Set)) for value in defaults)
        self.functions.append(
            FunctionFacts(
                path=self.display_path,
                qualname=".".join(self.scope),
                line=start,
                end_line=end,
                source=text,
                line_count=end - start + 1,
                parameter_count=parameter_count,
                max_nesting=metrics.max_depth,
                return_count=metrics.return_count,
                decision_count=metrics.decision_count,
                bare_except=metrics.bare_except,
                broad_except=metrics.broad_except,
                mutable_default=mutable_default,
                eval_exec_calls=tuple(sorted(metrics.eval_exec)),
            )
        )
        for child in node.body:
            self.visit(child)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)


def extract_functions(path: Path, *, root: Path | None = None) -> list[FunctionFacts]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    display_path = path.resolve().relative_to(root.resolve()).as_posix() if root else path.as_posix()
    collector = _FunctionCollector(path, source, display_path)
    for node in tree.body:
        collector.visit(node)
    return collector.functions
