"""Tests for the startup sequence (``app/bootstrap.py``) and the entry point.

Startup used to be a run of top-level statements in ``main.py``: importing the
entry point audited the OS logs, loaded resources, probed both e-paper
controllers and handshook with the board controller. Nothing that lived in that
file could be imported by a test, which is why almost none of it was tested, and
a stray ``import universalchess.main`` anywhere in the tree would boot the
product as a side effect.

The order of those steps is not incidental -- each step exists in that position
because the next one depends on it -- so the move from statements to a function
has to preserve it. That is what these tests hold.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from universalchess.app import bootstrap, display_boot, startup_splash

SRC = Path(__file__).resolve().parents[2]


@pytest.fixture
def record_boot_steps(monkeypatch):
    """Replace every step of the bring-up with a recorder, and return the log."""
    from universalchess.board import board, boot_report, init_callback

    steps = []
    splash = MagicMock(name="splash")
    manager = MagicMock(name="display_manager")

    monkeypatch.setattr(boot_report, "audit_previous_shutdown",
                        lambda: steps.append("audit"))
    monkeypatch.setattr(bootstrap, "initialize_resources",
                        lambda: steps.append("resources"))
    monkeypatch.setattr(display_boot, "init_display",
                        lambda: (steps.append("display"), (manager, splash))[1])
    monkeypatch.setattr(init_callback, "set_callback",
                        lambda cb: steps.append("init_callback"))
    monkeypatch.setattr(board, "init_board", lambda: steps.append("init_board"))
    monkeypatch.setattr(board, "display_manager", None, raising=False)
    monkeypatch.setattr(startup_splash, "_splash", None, raising=False)

    yield steps

    startup_splash.set_splash(None)


def test_the_bring_up_runs_the_steps_in_the_order_the_hardware_requires(record_boot_steps):
    """boot() audits, loads resources, lights the panel, then wakes the board.

    Why: every one of those positions is load-bearing. The audit reads evidence
    of a power cut and must run before the controller is initialised, because
    initialising it is what cuts the power. Resources must be injected before a
    widget exists, or the first widget built draws blanks for the session. The
    panel must be showing a splash before the controller handshake, which takes
    seconds, or the user watches a blank screen. The init callback must be
    registered before the board module is imported, or handshake retries have
    nowhere to report. How a regression manifests: a reordering here shows up as
    an unexplained blank panel or a missing warning, on hardware only.
    """
    bootstrap.boot()

    assert record_boot_steps == [
        "audit", "resources", "display", "init_callback", "init_board",
    ]


def test_the_bring_up_hands_the_display_manager_to_the_board(record_boot_steps):
    """The Manager built here becomes the board module's display manager.

    Why: everything downstream draws through ``board.display_manager``. It was
    assigned by a top-level statement; if the move dropped it, the board would
    run with a live panel that nothing ever draws on. How a regression
    manifests: the panel keeps the splash forever.
    """
    from universalchess.board import board

    result = bootstrap.boot()

    assert result.display_manager is not None
    assert board.display_manager is result.display_manager


def test_the_bring_up_publishes_the_splash_for_the_rest_of_startup(record_boot_steps):
    """The splash is registered where the application module can report to it.

    Why: the application module reports its own slow imports (bluetooth, GLib,
    chess, PIL) while it is being imported, and cannot be handed anything at
    that point. It reads the splash from the shared handle instead. How a
    regression manifests: startup shows "Starting..." frozen for the whole load
    instead of naming what it is waiting on.
    """
    result = bootstrap.boot()

    assert startup_splash.current() is result.splash


def test_a_board_with_no_panel_still_boots(record_boot_steps, monkeypatch):
    """A panel that never initialises leaves the manager unset, not an exception.

    Why: display bring-up returns None when neither controller can drive the
    panel (a dead or absent panel), and the board must still run headless --
    play continues on the squares and the web UI. How a regression manifests:
    startup raises here, so a display fault takes the whole board down.
    """
    monkeypatch.setattr(display_boot, "init_display", lambda: (None, None))

    result = bootstrap.boot()

    assert result.display_manager is None
    assert result.splash is None
    assert startup_splash.current() is None


def test_startup_notes_are_silent_without_a_splash():
    """Progress notes are a no-op when no splash exists.

    Why: the application module reports progress unconditionally at import time,
    including in tests and tooling where nothing has brought a panel up. How a
    regression manifests: importing the module raises AttributeError on None,
    which would put the import-time side effects straight back.
    """
    startup_splash.set_splash(None)

    startup_splash.note("splash.loading")  # must not raise


def test_the_application_module_can_be_imported_without_hardware():
    """A test can import the application module, on any machine.

    Why: this is what the split bought. The module holds the game loop, the menu
    handlers and the board-command routing, and until now no test could import
    it -- the import itself probed e-paper controllers and waited on the board
    controller, so it hung rather than failed. Every test that needed something
    in it had to duplicate the logic or skip. How a regression manifests: an
    import-time side effect returns and this hangs or raises.
    """
    from universalchess.app import board_app

    assert callable(board_app.main)
    assert callable(board_app._start_game_mode)


def test_importing_the_entry_point_starts_nothing():
    """``import universalchess.main`` must not boot the board.

    Why: this is the regression the whole split exists to prevent. Importing the
    entry point used to run the audit, load resources, probe both e-paper
    controllers and handshake with the controller, which is why a test could not
    import it (it hung waiting for hardware) and why one careless import
    anywhere would start the product.

    Run in a subprocess because import side effects happen once per process, so
    an in-process check would pass on nothing but a cached module. The
    subprocess deliberately has no hardware stubs: if the entry point pulled in
    the board, the SPI import would fail and the exit code would be non-zero.
    How a regression manifests: this fails, hangs, or reports a hardware module
    among the ones loaded.
    """
    probe = (
        "import sys; import universalchess.main as m; "
        "assert callable(m.main); "
        "loaded = [n for n in sys.modules "
        "if n in ('spidev', 'universalchess.board.board', "
        "'universalchess.app.board_app')]; "
        "print(loaded)"
    )
    result = subprocess.run(  # noqa: S603 - runs this interpreter with a fixed probe
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60, cwd=SRC,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout
