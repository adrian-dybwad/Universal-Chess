"""No test module may replace ``sys.modules`` entries when it is imported.

Why this test exists
--------------------
Pytest imports every test module during collection, before any test runs. A
module that assigns ``sys.modules["dbus"] = ...`` at import time therefore
replaces that module for the whole session, including tests in other files
that ran fine on their own. Eight files did this, copied from one another,
overriding the stubs ``universalchess/conftest.py`` already installs. Their
bare ``dbus`` module lacked ``dbus.exceptions``, so any later test that
imported ``board_app`` fresh failed at setup with "cannot import name
'BleManager'" -- but only when such a file happened to be collected first,
which is why the failures depended on the order files were listed in.

Hardware stubs belong in ``conftest.py``; a test that needs a different module
for its own duration uses ``patch.dict(sys.modules, ...)`` or ``monkeypatch``,
which restore it afterwards.

How a regression manifests
--------------------------
The assertion lists ``path:line`` for each import-time ``sys.modules``
assignment, ``update`` or ``setdefault``, so the offending file is named
directly rather than surfacing as an unrelated setup error elsewhere.
"""

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MUTATING_METHODS = {"update", "setdefault"}


def _is_sys_modules(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _mutates_sys_modules(node: ast.AST) -> bool:
    """Whether ``node`` writes ``sys.modules[...]`` or calls a mutating method on it."""
    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return any(
            isinstance(target, ast.Subscript) and _is_sys_modules(target.value)
            for target in targets
        )
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        func = node.value.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in MUTATING_METHODS
            and _is_sys_modules(func.value)
        )
    return False


def _import_time_statements(body):
    """Yield every statement that runs when the module is imported.

    Module-level control flow and class bodies execute at import; function
    bodies (tests, fixtures, helpers) do not, so they are not entered.
    """
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield statement
        for field in ("body", "orelse", "finalbody"):
            yield from _import_time_statements(getattr(statement, field, []))
        for handler in getattr(statement, "handlers", []):
            yield from _import_time_statements(handler.body)


def _test_modules():
    return sorted(PACKAGE_ROOT.rglob("test_*.py"))


def test_test_modules_are_found():
    """The scan must cover the test tree, or the check below passes vacuously.

    Regression: an empty list, because PACKAGE_ROOT no longer points at the
    package that holds ``tests/``.
    """
    assert Path(__file__).resolve() in _test_modules()


def test_no_test_module_replaces_sys_modules_at_import():
    """Collection must not swap modules out from under other test files.

    Regression: the list names each ``path:line`` that writes ``sys.modules``
    outside a function body.
    """
    offenders = []
    for path in _test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for statement in _import_time_statements(tree.body):
            if _mutates_sys_modules(statement):
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{statement.lineno}")

    assert offenders == []
