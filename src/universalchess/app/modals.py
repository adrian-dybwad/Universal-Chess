"""Overlays that see board keys before the menu or the game does.

Four things can be on screen with something else underneath: the help tip for a
focused menu entry, a dismissible error splash, the on-screen keyboard, and the
incoming-pairing confirmation. Each must consume the keys it is shown for, or a
press meant to dismiss a message also selects the menu row behind it, and a
pairing prompt can be answered by a key aimed at the game.

The order they are offered a key in used to be the order of four ``if`` blocks
inside the board's key handler, where it could only be read by reading the
handler. It is :attr:`Modals.by_priority` here, stated once.
"""

from typing import Any, Tuple


class Modal:
    """One overlay, and whether a key it does not use may pass beneath it.

    Every overlay is modal except the keyboard, which handles the character and
    navigation keys and deliberately ignores the rest so that a key it has no
    use for still reaches the handler underneath -- otherwise there would be no
    way out of password entry.
    """

    def __init__(self, name: str, passes_unhandled_keys: bool = False) -> None:
        """Create an overlay that is not showing.

        Args:
            name: What this overlay is, for reading the priority order.
            passes_unhandled_keys: Whether a key this overlay's widget reports
                as unhandled continues to the next overlay and beyond.
        """
        self.name = name
        self.passes_unhandled_keys = passes_unhandled_keys
        self._widget: Any = None

    @property
    def widget(self) -> Any:
        """The widget on screen, or None."""
        return self._widget

    @property
    def showing(self) -> bool:
        """Whether this overlay is currently on screen."""
        return self._widget is not None

    def show(self, widget: Any) -> None:
        """Put ``widget`` on screen and start routing keys to it."""
        self._widget = widget

    def hide(self) -> None:
        """Stop routing keys here.

        Called when the widget comes off the screen, including from the failure
        path of a render: a reference left behind sends every subsequent key to
        a widget nobody can see, which reads as a board that has stopped
        responding for no visible reason.
        """
        self._widget = None

    def offer(self, key_id: Any) -> bool:
        """Give this overlay a key.

        Args:
            key_id: The board key that was pressed.

        Returns:
            True when the key is consumed here and must not travel further.
        """
        widget = self._widget
        if widget is None:
            return False
        handled = widget.handle_key(key_id)
        if self.passes_unhandled_keys:
            return bool(handled)
        return True


class Modals:
    """The board's overlays, in the order they are offered a key.

    Help is first because it is opened from a menu entry that may itself be one
    of the others, so it is drawn on top and must be dismissed before what is
    underneath resumes.
    """

    def __init__(self) -> None:
        """Create the four overlays, none of them showing."""
        self.help_dialog = Modal("help_dialog")
        self.error_splash = Modal("error_splash")
        self.keyboard = Modal("keyboard", passes_unhandled_keys=True)
        self.pairing_confirm = Modal("pairing_confirm")

    @property
    def by_priority(self) -> Tuple[Modal, ...]:
        """The overlays, in the order a key is offered to them."""
        return (self.help_dialog, self.error_splash, self.keyboard,
                self.pairing_confirm)

    def handle_key(self, key_id: Any) -> bool:
        """Offer a key to each showing overlay in turn.

        Args:
            key_id: The board key that was pressed.

        Returns:
            True when an overlay consumed the key, so the menu and the game
            must not also act on it.
        """
        for modal in self.by_priority:
            if modal.offer(key_id):
                return True
        return False
