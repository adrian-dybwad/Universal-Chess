"""No test module may write ``sys.modules`` directly.

Why this test exists
--------------------
``sys.modules`` is shared by every test in the run, and a direct write to it is
never undone. Two shapes of this broke tests in other files, depending only on
the order files ran in:

* At import time. Pytest imports every test module during collection, so
  ``sys.modules["dbus"] = ...`` at module level decides that entry for the
  whole session. Thirty-three files did this, overriding the stubs
  ``universalchess/conftest.py`` installs; their bare ``dbus`` lacked
  ``dbus.exceptions``, so a later fresh import of ``board_app`` failed with
  "cannot import name 'BleManager'".
* Inside a fixture or test. Popping ``PIL`` and importing it again leaves two
  ``PIL.Image`` modules: images made by one fail the other's ``isinstance``
  check (``paste`` then raises "cannot determine region size"), and the new
  module's encoder registry is empty (``save`` raises ``KeyError: 'JPEG'``).

Hardware stubs belong in ``conftest.py``. A test that needs a different module
for its own duration uses ``monkeypatch.setitem(sys.modules, ...)`` or
``patch.dict(sys.modules, ...)``, which restore the entry afterwards and are
not flagged here.

How a regression manifests
--------------------------
The assertion lists ``path:line`` for each direct ``sys.modules`` assignment,
``del``, or call to a mutating method, so the offending file is named directly
rather than surfacing as an unrelated failure in another file.
"""

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MUTATING_METHODS = {"update", "setdefault", "pop", "popitem", "clear"}


def _is_sys_modules(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _is_sys_modules_item(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and _is_sys_modules(node.value)


def _mutates_sys_modules(node: ast.AST) -> bool:
    """Whether ``node`` writes ``sys.modules`` without a restoring helper."""
    if isinstance(node, ast.Assign):
        return any(_is_sys_modules_item(target) for target in node.targets)
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        return _is_sys_modules_item(node.target)
    if isinstance(node, ast.Delete):
        return any(_is_sys_modules_item(target) for target in node.targets)
    if isinstance(node, ast.Call):
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in MUTATING_METHODS
            and _is_sys_modules(func.value)
        )
    return False


def _test_modules():
    return sorted(PACKAGE_ROOT.rglob("test_*.py"))


def test_test_modules_are_found():
    """The scan must cover the test tree, or the check below passes vacuously.

    Regression: an empty list, because PACKAGE_ROOT no longer points at the
    package that holds ``tests/``.
    """
    assert Path(__file__).resolve() in _test_modules()


def test_no_test_module_writes_sys_modules_directly():
    """No test may leave another test's modules different from what it found.

    Regression: the list names each ``path:line`` that writes ``sys.modules``
    other than through ``monkeypatch`` or ``patch.dict``.
    """
    offenders = []
    for path in _test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if _mutates_sys_modules(node):
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}")

    assert offenders == [], "\n".join(offenders)
