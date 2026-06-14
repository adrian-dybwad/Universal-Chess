"""Regression test for Lichess token entry (Accounts -> Lichess).

Background / why this test exists
---------------------------------
``ensure_token`` invokes the injected ``keyboard_factory`` to open the on-screen
keyboard. The app's real factories name their third parameter ``max_len`` and the
documented contract is positional ``(update_fn, title, max_length)``. ``ensure_token``
once called the factory with a keyword (``max_length=64``), which raised
``TypeError: ... unexpected keyword argument 'max_length'`` and tore down the main
loop (blanking the board) the moment a user opened Accounts -> Lichess. This test
pins that ``ensure_token`` calls the factory using the positional contract so a
parameter-name drift cannot break it again.
"""

import logging

from universalchess.menus import ensure_token


log = logging.getLogger("test")


class _FakeKeyboard:
    def __init__(self, returned_text):
        self._returned_text = returned_text
        self.text = ""

    def wait_for_input(self, timeout):
        return self._returned_text


class _FakeDisplayManager:
    def update(self, *args, **kwargs):
        return None

    def clear_widgets(self, addStatusBar=True):
        return None

    def add_widget(self, widget):
        # No render promise in the fake; ensure_token tolerates a falsy return.
        return None


class _FakeBoard:
    SOUND_GENERAL = "general"

    def __init__(self):
        self.display_manager = _FakeDisplayManager()
        self.beeps = []

    def beep(self, sound):
        self.beeps.append(sound)


def _app_style_factory(captured):
    """Mirror the app's factory signature exactly: third param named ``max_len``.

    A keyword call of ``max_length=`` (the original bug) would raise TypeError
    against this signature; a positional call works. That makes this factory a
    faithful regression guard.
    """

    def factory(update_fn, title, max_len):
        captured["title"] = title
        captured["max_len"] = max_len
        return _FakeKeyboard("tok_abcdef")

    return factory


def test_ensure_token_calls_factory_positionally_and_saves():
    """Opening Accounts -> Lichess must not crash and must persist the token.

    Why: guards the TypeError that ended the main loop and blanked the board.
    How the regression manifests: a keyword call against the ``max_len`` factory
    raises TypeError here instead of returning the entered token.
    """
    captured = {}
    saved = {}
    board = _FakeBoard()

    result = ensure_token(
        menu_manager=None,
        keyboard_factory=_app_style_factory(captured),
        get_token=lambda: "",
        set_token=lambda t: saved.__setitem__("token", t),
        log=log,
        board=board,
        set_active_keyboard=lambda w: None,
        clear_active_keyboard=lambda: None,
    )

    assert result == "tok_abcdef", "entered token must be returned"
    assert saved.get("token") == "tok_abcdef", "entered token must be persisted"
    assert captured["title"] == "Lichess Token", "factory must receive the title"
    assert captured["max_len"] == 64, "factory must receive the 64-char limit positionally"
    assert board.beeps == [board.SOUND_GENERAL], "a confirmation beep fires on save"


def test_ensure_token_cancelled_does_not_save():
    """Cancelling token entry (no input) must not persist or beep.

    Why: distinguishes a real save from a cancel so a cancel cannot wipe or
    overwrite the stored token.
    How the regression manifests: set_token is called with None, or a beep fires,
    when wait_for_input returns None.
    """
    saved = {}
    board = _FakeBoard()

    def factory(update_fn, title, max_len):
        return _FakeKeyboard(None)

    result = ensure_token(
        menu_manager=None,
        keyboard_factory=factory,
        get_token=lambda: "existing",
        set_token=lambda t: saved.__setitem__("token", t),
        log=log,
        board=board,
        set_active_keyboard=lambda w: None,
        clear_active_keyboard=lambda: None,
    )

    assert result is None, "cancel returns None"
    assert "token" not in saved, "cancel must not persist a token"
    assert board.beeps == [], "no confirmation beep on cancel"


def test_ensure_token_registers_and_clears_active_keyboard():
    """The token keyboard must be registered active, then cleared afterward.

    Why this test exists: board key presses and piece placements are only routed
    to the keyboard while it is the registered "active keyboard widget". Without
    registration the Lichess token keyboard is dead - BACK/TICK and typing never
    reach it (the reported "Back does not go back in Lichess" bug). The clear must
    run in a finally so a cancel or exception cannot leave a stale active keyboard
    swallowing later menu input.

    How the regression manifests: set_active_keyboard is never called with the
    keyboard, or clear_active_keyboard is not the last action.
    """
    events = []
    board = _FakeBoard()
    kb_holder = {}

    def factory(update_fn, title, max_len):
        kb = _FakeKeyboard("tok")
        kb_holder["kb"] = kb
        return kb

    ensure_token(
        menu_manager=None,
        keyboard_factory=factory,
        get_token=lambda: "",
        set_token=lambda t: None,
        log=log,
        board=board,
        set_active_keyboard=lambda w: events.append(("set", w)),
        clear_active_keyboard=lambda: events.append(("clear",)),
    )

    assert ("set", kb_holder["kb"]) in events, "keyboard must be registered as active"
    assert events[-1] == ("clear",), "active keyboard must be cleared last (finally)"
    assert events.index(("set", kb_holder["kb"])) < events.index(("clear",)), (
        "must register before clearing"
    )
