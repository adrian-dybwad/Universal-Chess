"""Tests for the overlays that see board keys before the menu or game does.

Four overlays can be on screen while something else is underneath: the help tip
for a focused menu entry, a dismissible error splash, the on-screen keyboard,
and the incoming-pairing confirmation. Each has to consume keys, or a press
meant to dismiss a message would also select the menu row behind it -- and a
pairing could be confirmed by a key the user meant for the game.

Their order was expressed only by the order of four ``if`` blocks in the middle
of the board's 200-line key handler, so it could not be read without reading the
handler and could not be tested at all.
"""

import pytest

from universalchess.app.modals import Modals


class _Overlay:
    """An overlay that records the keys it was given."""

    def __init__(self, handled=True):
        self.keys = []
        self._handled = handled

    def handle_key(self, key_id):
        self.keys.append(key_id)
        return self._handled


@pytest.fixture
def modals():
    """A fresh, empty set of overlays."""
    return Modals()


def test_no_overlay_leaves_the_key_to_the_menu(modals):
    """With nothing on screen, the key is not consumed.

    Why: every key would otherwise be swallowed and the board would stop
    responding entirely. How a regression manifests: the menu does not move.
    """
    assert modals.handle_key("UP") is False


def test_an_overlay_consumes_the_key(modals):
    """A key that reaches an overlay does not reach what is behind it.

    Why: the press that dismisses an error splash must not also activate the
    menu row underneath, which is what the user is looking at once the splash
    is gone. How a regression manifests: dismissing a message performs an
    action the user never chose.
    """
    splash = _Overlay()
    modals.error_splash.show(splash)

    assert modals.handle_key("BACK") is True
    assert splash.keys == ["BACK"]


def test_help_is_offered_the_key_before_every_other_overlay(modals):
    """The help dialog is first in line.

    Why: help is opened from a menu entry that may itself be a keyboard or a
    pairing prompt, so it is drawn on top and must be dismissed before the
    thing underneath resumes. How a regression manifests: the key that should
    turn the help page types a character into the password keyboard behind it.
    """
    help_dialog, keyboard = _Overlay(), _Overlay()
    modals.keyboard.show(keyboard)
    modals.help_dialog.show(help_dialog)

    modals.handle_key("DOWN")

    assert help_dialog.keys == ["DOWN"]
    assert keyboard.keys == []


def test_a_key_the_keyboard_does_not_handle_falls_through(modals):
    """The keyboard is the one overlay that lets a key pass.

    Why: it handles the character and navigation keys and deliberately ignores
    the rest, so a key it does not use still reaches the handler underneath.
    Every other overlay is modal and consumes everything. How a regression
    manifests: keys the keyboard ignores are swallowed, so there is no way out
    of password entry.
    """
    keyboard = _Overlay(handled=False)
    modals.keyboard.show(keyboard)

    assert modals.handle_key("PLAY") is False
    assert keyboard.keys == ["PLAY"]


def test_the_pairing_confirmation_consumes_everything(modals):
    """No key escapes the pairing prompt.

    Why: a key leaking past it reaches the menu or the game, so a phone pairing
    with the board could be confirmed or rejected by a press the user meant for
    something else. How a regression manifests: pressing a key during pairing
    both answers the prompt and acts on the screen behind it.
    """
    confirm = _Overlay(handled=False)
    modals.pairing_confirm.show(confirm)

    assert modals.handle_key("TICK") is True
    assert confirm.keys == ["TICK"]


def test_hiding_an_overlay_returns_keys_to_what_is_behind_it(modals):
    """An overlay that is taken down stops consuming keys.

    Why: a render that failed partway used to leave the reference set, and every
    key after it went to a widget that was no longer on screen -- a board that
    ignores its buttons with nothing shown to explain why. How a regression
    manifests: the board stops responding after a splash is dismissed.
    """
    splash = _Overlay()
    modals.error_splash.show(splash)
    modals.error_splash.hide()

    assert modals.handle_key("BACK") is False
    assert splash.keys == []


def test_an_overlay_reports_whether_it_is_showing(modals):
    """Whether an overlay is on screen can be asked without sending it a key.

    Why: the pairing confirmation is also consulted outside the key handler, to
    decide whether a second incoming request can be shown. How a regression
    manifests: two pairing prompts stack up and the board draws one over the
    other.
    """
    assert modals.pairing_confirm.showing is False

    modals.pairing_confirm.show(_Overlay())

    assert modals.pairing_confirm.showing is True
    assert modals.pairing_confirm.widget is not None


def test_the_priority_order_is_stated_once(modals):
    """The order overlays are offered keys in is a single, readable list.

    Why: it used to be the order of four ``if`` blocks inside a 200-line key
    handler, where inserting a fifth overlay in the wrong place was invisible in
    review. How a regression manifests: an overlay is added to the class but not
    to the order, so it never receives a key and cannot be dismissed.
    """
    assert [modal.name for modal in modals.by_priority] == [
        "help_dialog",
        "error_splash",
        "keyboard",
        "pairing_confirm",
    ]
