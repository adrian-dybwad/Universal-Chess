"""Tests for the shared settings-reset behavior.

The e-paper Reset Settings menu (after its confirmation) and the web Reset
control (/api/system/reset -> board IPC) both run reset_all_settings. These tests
pin that one shared function so the two surfaces reset identically: the three
config sections are cleared and the in-memory settings are reloaded.
"""

import universalchess.menus.reset_menu as reset_menu
from universalchess.menus.reset_menu import reset_all_settings, handle_reset_settings


class _Log:
    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Board:
    """Board stub recording error beeps (the on-board failure feedback)."""

    SOUND_WRONG_MOVE = "wrong"

    def __init__(self):
        self.beeps = []

    def beep(self, sound, event_type=None):
        self.beeps.append((sound, event_type))


def test_reset_all_settings_clears_three_sections_then_reloads(monkeypatch):
    """reset_all_settings must clear game+player sections, then reload settings.

    Why this test exists: this is the single reset path shared by board and web;
    the exact sections cleared and the reload-after-clear order define what
    "reset" means. The reload must happen after the clears so the in-memory
    settings drop to defaults.

    How a regression manifests: a section is missed (stale settings survive the
    reset) or the reload runs before/without the clears (settings reload the old
    values), both of which change the recorded order below.
    """
    events = []
    monkeypatch.setattr(reset_menu, "clear_section", lambda section: events.append(("clear", section)))

    reset_all_settings(
        load_game_settings=lambda: events.append(("reload", None)),
        log=_Log(),
        board=_Board(),
        settings_section="game",
        player1_section="PlayerOne",
        player2_section="PlayerTwo",
    )

    assert events == [
        ("clear", "game"),
        ("clear", "PlayerOne"),
        ("clear", "PlayerTwo"),
        ("reload", None),
    ]


def test_reset_all_settings_beeps_error_and_does_not_raise(monkeypatch):
    """A failure mid-reset must beep the error tone and be swallowed.

    Why this test exists: the on-board reset beeps SOUND_WRONG_MOVE on failure
    rather than crashing the menu loop; the shared function must keep that
    behavior so a partial reset never takes down the caller (board or web).

    How a regression manifests: the exception propagates (no beep recorded, or
    the call raises), crashing the menu/endpoint instead of signalling the error.
    """
    def _boom(section):
        raise OSError("disk full")

    monkeypatch.setattr(reset_menu, "clear_section", _boom)
    board = _Board()

    reset_all_settings(
        load_game_settings=lambda: None,
        log=_Log(),
        board=board,
        settings_section="game",
        player1_section="PlayerOne",
        player2_section="PlayerTwo",
    )

    assert board.beeps == [("wrong", "error")]


def test_handle_reset_settings_resets_only_on_confirm(monkeypatch):
    """The menu must run the reset only when the user confirms.

    Why this test exists: handle_reset_settings now delegates to
    reset_all_settings; this guards that the confirmation gate is preserved so a
    cancel does not wipe settings.

    How a regression manifests: choosing 'cancel' still clears sections (reset
    fires unconditionally), or 'confirm' no longer triggers the reset.
    """
    cleared = []
    monkeypatch.setattr(reset_menu, "clear_section", lambda section: cleared.append(section))

    def _run(result_key):
        cleared.clear()
        handle_reset_settings(
            show_menu=lambda entries: result_key,
            load_game_settings=lambda: None,
            log=_Log(),
            board=_Board(),
            settings_section="game",
            player1_section="PlayerOne",
            player2_section="PlayerTwo",
        )
        return list(cleared)

    assert _run("cancel") == []
    assert _run("confirm") == ["game", "PlayerOne", "PlayerTwo"]
