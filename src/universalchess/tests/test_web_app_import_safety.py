"""Guards that importing the web app never reads the runtime e-paper snapshot.

Why these tests exist:
    A deploy to a live board left ``universal-chess-web`` dead at import with
    ``PIL.UnidentifiedImageError: cannot identify image file
    '/opt/universalchess/web/static/epaper.jpg'``. The board rewrites that file
    in place on every panel refresh, so the module-level ``Image.open`` of it
    could observe a half-written file, and an exception raised at import kills
    the whole Flask process before it binds its port. Only systemd's
    ``Restart=always`` brought the web interface back.

    The import-time read was also redundant. ``_render_classic_cast_frame`` is
    the sole consumer of the ``sc``/``moddate`` globals and already reloads the
    snapshot whenever its mtime changed or ``sc`` is None, catching
    ``UnidentifiedImageError``/``OSError`` and falling back to a blank frame.
    Every other reader of the snapshot (``_read_epaper_snapshot_bytes``) carried
    a partial-write guard; the import-time read was the only unguarded one.

How a regression manifests:
    Any module-scope read of the snapshot returns the startup crash: the web
    process exits 1 whenever it happens to import while the board is refreshing
    the panel. The window is not small -- importing takes roughly 70 seconds on
    the board's single ARMv6 core, and the panel is rewritten continuously.
    ``test_module_scope_code_does_not_read_the_epaper_snapshot`` fails and names
    the offending line.

The invariant is asserted against ``web/app.py``'s AST rather than by importing
the module, because the module cannot be imported inside this suite without
stubbing ``Image.open`` (see ``test_screen_feed``). With that stub in place an
import-time decode cannot fail, so importing can never observe this defect --
which is why the crash reached a board undetected.
"""

import ast
from pathlib import Path

import pytest

_APP_SOURCE_PATH = Path(__file__).resolve().parents[1] / "web" / "app.py"

# The names ``web/app.py`` uses for the runtime-rewritten snapshot path.
_SNAPSHOT_PATH_NAMES = frozenset({"EPAPER_STATIC_JPG", "epaper_path"})

# Calls that read a file's contents or metadata. ``stat`` is included with
# ``open`` because the module-scope block read both, and both race with the
# board's in-place rewrite; excluding it would let half the defect return.
_FILE_READER_NAMES = frozenset({"open", "stat", "getmtime", "getsize"})

# The exact block removed from web/app.py, kept as a detector fixture. The reads
# sit inside a module-level ``if``, so this also pins that the walk descends into
# nested module-scope statements instead of only scanning ``tree.body``.
_REMOVED_IMPORT_TIME_BLOCK = """
import os
from PIL import Image
from universalchess.paths import EPAPER_STATIC_JPG

moddate = -1
sc = None
epaper_path = EPAPER_STATIC_JPG
if os.path.isfile(epaper_path):
    sc = Image.open(epaper_path)
    moddate = os.stat(epaper_path)[8]
"""

# The pattern that must stay allowed: the same reads inside a function, where a
# failure is caught per render. Flagging these would make the invariant
# unsatisfiable and force the guard to be deleted rather than honoured.
_PERMITTED_LAZY_READ_IN_FUNCTION = """
import os
from PIL import Image
from universalchess.paths import EPAPER_STATIC_JPG

sc = None
moddate = -1

def _render_classic_cast_frame():
    global sc, moddate
    newmoddate = os.stat(EPAPER_STATIC_JPG)[8]
    if newmoddate != moddate or sc is None:
        with Image.open(EPAPER_STATIC_JPG) as snapshot:
            sc = snapshot.convert("RGB").copy()
        moddate = newmoddate
    return sc
"""


def _module_scope_nodes(tree):
    """Yield nodes reachable from module scope, never entering a def or class.

    Reads inside a function body run per request or per rendered frame, where the
    caller handles failure; only code reachable at import can abort the process.
    The walk therefore stops at every scope boundary rather than using
    ``ast.walk``, which would flag the guarded lazy reload as well.
    """
    scope_boundaries = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Lambda,
    )
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, scope_boundaries):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _snapshot_reads_at_import(source):
    """Return ``"<line>: <source>"`` for each module-scope snapshot read."""
    tree = ast.parse(source)
    source_lines = source.splitlines()
    offenders = []
    for node in _module_scope_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called not in _FILE_READER_NAMES:
            continue
        if any(
            isinstance(inner, ast.Name) and inner.id in _SNAPSHOT_PATH_NAMES
            for inner in ast.walk(node)
        ):
            offenders.append(f"{node.lineno}: {source_lines[node.lineno - 1].strip()}")
    return sorted(offenders)


def test_module_scope_code_does_not_read_the_epaper_snapshot():
    """The shipped web app must not touch the snapshot at import.

    Regression: the web process exits 1 at startup with UnidentifiedImageError
    whenever the import races the board's in-place rewrite of epaper.jpg, and
    only systemd's Restart=always recovers it. The assertion message names the
    line that reintroduced the read.
    """
    offenders = _snapshot_reads_at_import(_APP_SOURCE_PATH.read_text())
    assert offenders == [], (
        "web/app.py reads the runtime-rewritten e-paper snapshot at import; "
        f"move the read into the render path: {offenders}"
    )


@pytest.mark.parametrize(
    "reader",
    ["Image.open(epaper_path)", "os.stat(epaper_path)"],
    ids=["image-open", "os-stat"],
)
def test_detector_flags_the_removed_import_time_block(reader):
    """The detector must actually catch the code that caused the outage.

    An AST guard that matches nothing is indistinguishable from a passing
    invariant, so the exact removed block is fed back through it. Regression (a
    detector narrowed until it is vacuous): the offending reader is not reported
    and test_module_scope_code_does_not_read_the_epaper_snapshot silently
    becomes meaningless.
    """
    offenders = _snapshot_reads_at_import(_REMOVED_IMPORT_TIME_BLOCK)
    assert any(reader in offender for offender in offenders), (reader, offenders)


def test_detector_allows_the_guarded_lazy_read_inside_a_function():
    """Reads inside the render path are the fix, not the defect.

    Regression: a detector using ast.walk instead of a scope-stopping walk flags
    _render_classic_cast_frame's guarded reload, making the invariant impossible
    to satisfy and inviting deletion of the whole guard.
    """
    assert _snapshot_reads_at_import(_PERMITTED_LAZY_READ_IN_FUNCTION) == []
