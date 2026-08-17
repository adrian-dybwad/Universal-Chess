"""In-game MenuManager overlays must receive keys while app_state is GAME.

Why these tests exist
---------------------
Lichess takeback/draw/challenge dialogs use MenuManager.show_menu during a
game. key_callback only delivered keys to MenuManager in MENU/SETTINGS, so
during GAME: TICK full-refreshed the panel instead of Accept, BACK opened
abort/resign, PLAY suspended the game. Accept was painted and could not be
chosen.

How a regression manifests
--------------------------
handle_if_active returns False while a widget is active, or lets PLAY fall
through (False) so the GAME branch would suspend.
"""

from unittest.mock import MagicMock

from universalchess.managers.menu import MenuManager


def test_handle_if_active_is_false_when_idle():
    """No overlay means GAME keys must keep going to the game.

    How the regression manifests: handle_if_active returns True with no widget,
    so TICK never full-refreshes and BACK never reaches abort.
    """
    manager = MenuManager()
    assert manager.handle_if_active("TICK") is False


def test_handle_if_active_queues_keys_while_loading():
    """TICK during the e-paper paint must not be lost.

    How the regression manifests: queue is empty after TICK while loading, so
    Accept is missed if the user presses OK before the menu is active.
    """
    manager = MenuManager()
    manager._menu_loading = True
    assert manager.handle_if_active("TICK") is True
    assert manager._key_queue == ["TICK"]


def test_handle_if_active_delivers_tick_to_the_widget():
    """TICK must select the focused row (Accept), not refresh the panel.

    How the regression manifests: handle_key is not called, so Accept never
    fires.
    """
    manager = MenuManager()
    widget = MagicMock()
    widget.handle_key.return_value = True
    manager._active_widget = widget
    assert manager.handle_if_active("TICK") is True
    widget.handle_key.assert_called_once_with("TICK")


def test_handle_if_active_consumes_play_while_overlay_is_shown():
    """PLAY is not a menu key; it must not suspend the game under the overlay.

    How the regression manifests: handle_if_active returns False for PLAY and
    the GAME branch calls _suspend_game while Accept/Decline is on screen.
    """
    manager = MenuManager()
    widget = MagicMock()
    widget.handle_key.return_value = False
    manager._active_widget = widget
    assert manager.handle_if_active("PLAY") is True
    widget.handle_key.assert_called_once_with("PLAY")
