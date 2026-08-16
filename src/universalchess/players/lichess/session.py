"""In-game Lichess session: splash, color remap, offers, BACK-during-seek.

The game injects display, menu, and clock. This object owns Lichess-specific
wiring so ``main`` does not import :class:`LichessPlayer` to isinstance it.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from universalchess.board.logging import log
from universalchess.epaper.icon_menu import IconMenuEntry

from .player import LichessPlayer


class LichessPlaySession:
    """Board-side session for one Human vs Lichess game."""

    def __init__(self, remote: LichessPlayer):
        self._remote = remote
        self.game_connected = False
        self._started_splash_held = True
        self._player_manager = None
        self._game_display = None
        self._panel = None
        self._info_overlay = None
        self._menu_manager = None
        self._beep = None
        self._set_game_result = None
        self._splash_seconds = 5.0
        self._show_started_splash = None

    @classmethod
    def from_players(cls, white_player, black_player) -> Optional["LichessPlaySession"]:
        """Return a session when either slot is a Lichess player, else None."""
        for player in (white_player, black_player):
            if isinstance(player, LichessPlayer):
                return cls(player)
        return None

    @property
    def waiting_mode(self):
        """Seek vs join vs challenge, for the waiting splash."""
        return self._remote._lichess_config.mode

    def attach(
        self,
        *,
        player_manager,
        game_display,
        panel,
        info_overlay,
        menu_manager,
        beep: Callable,
        set_game_result: Callable,
        splash_seconds: float,
        show_started_splash: Optional[Callable] = None,
    ) -> None:
        """Wire stream callbacks onto the remote player."""
        self._player_manager = player_manager
        self._game_display = game_display
        self._panel = panel
        self._info_overlay = info_overlay
        self._menu_manager = menu_manager
        self._beep = beep
        self._set_game_result = set_game_result
        self._splash_seconds = splash_seconds
        if show_started_splash is None:
            from .lobby import show_lichess_started_splash

            show_started_splash = show_lichess_started_splash
        self._show_started_splash = show_started_splash
        self._remote.set_on_game_connected(self._on_connected)
        self._remote.set_game_over_callback(self._on_game_over)
        self._remote.set_takeback_offer_callback(self._on_takeback_offer)
        self._remote.set_draw_offer_callback(self._on_draw_offer)
        self._remote.set_info_message_callback(self._on_info_message)

    def dismiss_started_splash(self) -> None:
        """Show game widgets once the started splash is done."""
        if not self._started_splash_held:
            return
        self._started_splash_held = False
        if self._game_display is not None:
            self._game_display.show_game_widgets()
        if self._panel is not None and self._info_overlay is not None:
            self._panel.add_widget(self._info_overlay)

    def on_back(
        self,
        *,
        stop_players: Callable[[], None],
        return_to_menu: Callable[[str], None],
        show_back_menu: Callable,
    ) -> None:
        """BACK during seek cancels; BACK after accept opens abort/resign."""
        if not self.game_connected:
            log.info("[Lichess] Seek cancelled")
            stop_players()
            return_to_menu("Lichess cancel")
            return
        show_back_menu(is_two_player=False, allow_abort=True)

    def _on_connected(self) -> None:
        if self.game_connected:
            return
        self.game_connected = True
        human_is_white = (
            True if self._remote.player_is_white is None else self._remote.player_is_white
        )
        if self._player_manager is not None:
            human = None
            remote = None
            for player in (
                self._player_manager.white_player,
                self._player_manager.black_player,
            ):
                if player is self._remote:
                    remote = player
                else:
                    human = player
            if human is not None and remote is not None:
                if human_is_white:
                    self._player_manager.reassign_slots(human, remote)
                else:
                    self._player_manager.reassign_slots(remote, human)
        if self._game_display is not None:
            self._game_display.set_flip_board(not human_is_white)
        if self._show_started_splash is not None:
            self._show_started_splash(self._panel, human_is_white)
        timer = threading.Timer(self._splash_seconds, self.dismiss_started_splash)
        timer.daemon = True
        timer.start()

    def _on_takeback_offer(self, accept_fn, decline_fn) -> None:
        log.info("[Lichess] Takeback offer received")
        if self._beep is not None:
            self._beep()
        entries = [
            IconMenuEntry(key="accept", label="Accept\nTakeback", icon_name="undo"),
            IconMenuEntry(key="decline", label="Decline", icon_name="cancel"),
        ]
        result = self._menu_manager.show_menu(entries)
        if hasattr(result, "key") and result.key == "accept":
            accept_fn()
        else:
            decline_fn()

    def _on_draw_offer(self, accept_fn, decline_fn) -> None:
        log.info("[Lichess] Draw offer received")
        if self._beep is not None:
            self._beep()
        entries = [
            IconMenuEntry(key="accept", label="Accept\nDraw", icon_name="draw"),
            IconMenuEntry(key="decline", label="Decline", icon_name="cancel"),
        ]
        result = self._menu_manager.show_menu(entries)
        if hasattr(result, "key") and result.key == "accept":
            accept_fn()
        else:
            decline_fn()

    def _on_game_over(self, result: str, termination: str, winner) -> None:
        log.info(
            f"[Lichess] Game over: result={result}, "
            f"termination={termination}, winner={winner}"
        )
        if self._game_display is not None:
            self._game_display.stop_clock()
        if self._set_game_result is not None:
            self._set_game_result(result, termination)

    def _on_info_message(self, message: str) -> None:
        if self._info_overlay is not None:
            self._info_overlay.show_message(message, duration_seconds=5.0)
