"""Locate the application module's source, for tests that read it structurally.

Several tests assert wiring that only exists as code: which names a
``_build_*_context`` function registers, which menu rows have a dispatch branch,
which command strings are passed to ``sudo``. Those are statements about the
source, and reading it with ``ast`` states them directly.

The module has moved once already (out of ``main.py``, so that importing the
entry point stopped booting the board) and will move again as it is broken up.
One constant here means that costs one edit rather than one per test file.
"""

import ast
from pathlib import Path

import universalchess

BOARD_APP_PY = Path(universalchess.__file__).resolve().parent / "app" / "board_app.py"


def function_node(name: str) -> ast.FunctionDef:
    """Return the AST of the named top-level function in the application module."""
    for node in ast.parse(BOARD_APP_PY.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {BOARD_APP_PY}")
