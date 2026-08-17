"""
Pytest configuration and fixtures for Universal-Chess tests.

Lives on the package so the app dump (``tests/``) and player-plugin trees
(``players/*/tests``) share hardware stubs. Pytest loads this file for any
test under ``src/universalchess/``.

This module provides:
- Mock controller fixture for tests that need board functionality
- Automatic cleanup of board state between tests
- Hardware module stubs for running tests on non-Pi systems
"""

import sys
import types
from unittest.mock import MagicMock

# Stub hardware-specific modules BEFORE any universalchess imports.
# This allows tests to run on non-Raspberry Pi systems (CI, development machines).
# These modules are only used for actual hardware interaction and are mocked during tests anyway.

_hardware_modules = [
    "spidev",
    "RPi",
    "RPi.GPIO",
    "gpiozero",
    "lgpio",
    "smbus",
    "smbus2",
    "serial",
]

for module_name in _hardware_modules:
    if module_name not in sys.modules:
        # Create a mock module that won't fail on import
        mock_module = MagicMock()
        # For RPi, ensure RPi.GPIO is accessible
        if module_name == "RPi":
            mock_module.GPIO = MagicMock()
        sys.modules[module_name] = mock_module

import pytest


@pytest.fixture
def private_db_session(tmp_path, monkeypatch):
    """A database session, and a rebound ``models.engine``, private to one test.

    ``db.models`` builds a process-wide engine at import time. Under the test
    configuration that engine is an in-memory SQLite, whose pool holds one
    connection per thread and evicts the oldest once more than a handful exist.
    Any test that starts a thread which opens a connection can therefore evict
    the connection holding the schema, and because an in-memory database lives
    only as long as its connection, the tables vanish. The next test to use
    ``models.engine`` then fails with "no such table" purely because of what ran
    before it.

    Binding to a file in ``tmp_path`` removes the dependency entirely: the
    schema survives connection churn, and each test gets an empty database.
    ``models.engine`` is rebound as well, since request handlers such as
    ``generate_pgn_string`` open their own session from it.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from universalchess.db import models

    engine = create_engine(f"sqlite:///{tmp_path / 'universalchess-test.db'}")
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(models, "engine", engine)

    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def spanish_board(monkeypatch):
    """Run the board in Spanish, and leave no cached locale behind.

    Both localizers -- the string bundle and the menu catalog -- memoise the
    device language on first use, so a test that switched it without resetting
    would decide the language of every test that ran after it. Spanish is the
    language these tests read in because it differs from English in every string
    under test, which an untranslated fallback cannot fake.
    """
    from universalchess import i18n
    from universalchess.menus.catalog import loader

    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: "es"
    )
    i18n._active_locale = None
    i18n._bundles.clear()
    loader._active_locale = None
    i18n.refresh_active_language()
    loader.refresh_active_language()
    yield i18n
    i18n._active_locale = None
    i18n._bundles.clear()
    loader._active_locale = None


@pytest.fixture
def mock_controller():
    """
    Provide a mock SyncCentaur controller for tests.
    
    This fixture patches board.controller with a MagicMock that has
    all the methods a real controller would have. Use this for tests
    that call board functions (beep, ledsOff, getBoardState, etc.)
    without requiring actual hardware.
    
    Usage:
        def test_something(mock_controller):
            from universalchess.board import board
            board.beep(board.SOUND_GENERAL)  # Uses mock, doesn't crash
            mock_controller.beep.assert_called_once()
    """
    from universalchess.board import board
    
    # Create mock controller with all expected methods
    mock = MagicMock()
    mock.ready = True
    mock._piece_listener = None
    
    # Mock request_response to return valid-ish data
    mock.request_response.return_value = bytes([0] * 64)
    mock.request_response_low_priority.return_value = bytes([0] * 64)
    mock.get_next_key.return_value = None
    mock.wait_for_key_up.return_value = None
    mock.sleep.return_value = True
    
    # Patch the global controller
    original_controller = board.controller
    board.controller = mock
    
    yield mock
    
    # Restore original (likely None in tests)
    board.controller = original_controller
