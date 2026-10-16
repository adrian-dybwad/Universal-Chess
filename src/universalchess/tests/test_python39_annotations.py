"""No module uses ``X | Y`` in an annotation that Python 3.9 evaluates.

Why this test exists:
    Python 3.9 (Debian Bullseye) is the oldest interpreter the package supports,
    and CI tests on it, but development machines run 3.11 or newer, where
    ``str | None`` is valid. ``services/power.py`` gained ``log_text: str | None``
    in a function signature; 3.9 evaluates signature annotations when the
    ``def`` runs, so importing the module raised ``TypeError: unsupported
    operand type(s) for |`` and every test importing it failed to collect on
    the 3.9 job for weeks while every local run passed. This scans the source
    tree, so the failure appears on whatever interpreter the developer has.

How a regression manifests:
    ``offenders`` lists ``path:line`` for each such annotation. Fix it with
    ``Optional[...]`` / ``Union[...]``, or add ``from __future__ import
    annotations`` to that module, which stops annotations being evaluated.
"""

import ast
from pathlib import Path
from typing import Iterator, List

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_DIRS = {"node_modules", "web-app", "__pycache__"}


def _defers_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


class _EvaluatedAnnotations(ast.NodeVisitor):
    """Collects the annotations Python evaluates without ``__future__.annotations``.

    Signatures are evaluated whenever their ``def`` runs, and variable
    annotations in a module or class body when that body runs. A variable
    annotation directly inside a function body is never evaluated, so it is
    skipped; that is the only exemption.
    """

    def __init__(self):
        self.found: List[ast.expr] = []
        self._in_function_body = False

    def _visit_function(self, node) -> None:
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None and arg.annotation is not None:
                self.found.append(arg.annotation)
        if node.returns is not None:
            self.found.append(node.returns)
        self._visit_scope(node, in_function_body=True)

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, in_function_body=False)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self._in_function_body:
            self.found.append(node.annotation)
        self.generic_visit(node)

    def _visit_scope(self, node: ast.AST, in_function_body: bool) -> None:
        enclosing = self._in_function_body
        self._in_function_body = in_function_body
        self.generic_visit(node)
        self._in_function_body = enclosing


def _uses_union_operator(annotation: ast.expr) -> bool:
    return any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
        for node in ast.walk(annotation)
    )


def _source_files() -> Iterator[Path]:
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if not EXCLUDED_DIRS.intersection(path.relative_to(PACKAGE_ROOT).parts):
            yield path


def _offending_lines(source: str) -> List[int]:
    """Lines of ``source`` holding an evaluated annotation that uses ``|``."""
    tree = ast.parse(source)
    if _defers_annotations(tree):
        return []
    collector = _EvaluatedAnnotations()
    collector.visit(tree)
    return [a.lineno for a in collector.found if _uses_union_operator(a)]


@pytest.mark.parametrize(("source", "expected_lines"), [
    pytest.param("def f(x: str | None): ...\n", [1], id="parameter"),
    pytest.param("def f() -> int | None: ...\n", [1], id="return"),
    pytest.param("x: int | None = None\n", [1], id="module_variable"),
    pytest.param("class C:\n    x: int | None = None\n", [2], id="class_variable"),
    pytest.param("def f():\n    def g(x: int | None): ...\n", [2], id="nested_signature"),
    pytest.param("def f():\n    x: int | None = None\n", [], id="local_variable_not_evaluated"),
    pytest.param("from __future__ import annotations\ndef f(x: str | None): ...\n", [],
                 id="future_import_defers"),
    pytest.param("def f(x: Optional[str]): ...\n", [], id="optional"),
])
def test_the_scan_flags_exactly_the_annotations_python39_evaluates(source, expected_lines):
    """The scanner flags evaluated ``|`` annotations and nothing else.

    Why: the repository-wide check is only as good as this classification. A
    missed case lets a 3.9 import failure through; a false positive blocks
    valid code such as a local variable annotation. How a regression
    manifests: the returned line list differs for the snippet named in the id.
    """
    assert _offending_lines(source) == expected_lines


def test_no_annotation_python39_evaluates_uses_the_union_operator():
    """Every package module imports on Python 3.9 as far as annotations go.

    How a regression manifests: ``offenders`` names ``path:line`` of the
    annotation that would raise TypeError when 3.9 imports that module.
    """
    offenders = [
        f"{path.relative_to(PACKAGE_ROOT)}:{line}"
        for path in _source_files()
        for line in _offending_lines(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_scan_reaches_the_package_sources():
    """Guards the scan itself: an empty file list would pass the check above vacuously.

    How a regression manifests: a changed PACKAGE_ROOT or exclusion drops the
    application modules, and ``services/power.py`` is not among the scanned files.
    """
    scanned = {path.relative_to(PACKAGE_ROOT).as_posix() for path in _source_files()}

    assert "services/power.py" in scanned
    assert "epaper/framework/manager.py" in scanned
