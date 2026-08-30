"""In-game Lichess session: splash, color remap, offers, BACK-during-seek.

The game injects display, menu, and clock. This object owns Lichess-specific
wiring so ``main`` does not import :class:`LichessPlayer` to isinstance it.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from universalchess.board.logging import log
from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.i18n import t

from .match import (
    LichessChallengeOffer,
    epaper_is_flipped,
    lichess_challenge_terms_label,
)
from .player import LichessGameMode, LichessPlayer


def challenge_menu_entries(offer: LichessChallengeOffer):
    """Accept/Decline rows plus a non-selectable summary of the challenger's terms."""
    return [
        IconMenuEntry(
            key="terms",
            label=lichess_challenge_terms_label(offer),
            icon_name="lichess",
            selectable=False,
            font_size=12,
        ),
        IconMenuEntry(
            key="accept", label=t("lichess.offer.accept_challenge"), icon_name="play"
        ),
        IconMenuEntry(key="decline", label=t("lichess.offer.decline"), icon_name="cancel"),
    ]


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
        self._rewind_to_move_count = None
        self._splash_timer = None
        self._closed = False
        self._player1_color = "white"
        self._on_unfinished_game = None

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

    @property
    def awaiting_opponent(self) -> bool:
        """True when the wait is for the opponent to accept a challenge we sent.

        Every other start joins a game that exists (or a seek someone can take);
        an outgoing challenge does not become a game until the other player
        accepts, so the splash says so instead of claiming to be loading it.
        """
        config = self._remote._lichess_config
        return (
            config.mode == LichessGameMode.CHALLENGE
            and config.challenge_direction != "in"
        )

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
        rewind_to_move_count: Optional[Callable[[int], None]] = None,
        catch_up_moves: Optional[Callable] = None,
        player1_color: str = "white",
        on_unfinished_game: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Wire stream callbacks onto the remote player.

        ``player1_color`` is the side the Players color control names, which is
        the side the human took when setting the pieces up. It decides whether
        the assigned color turns the display around (:func:`epaper_is_flipped`).
        ``on_unfinished_game`` is called with the termination when the remote
        game ends (abort, resign, mate, timeout, draw), so the main loop can
        offer Lobby / Seek / Cancel with that reason in the header.
        ``catch_up_moves`` replays a multi-ply first snapshot (joining a game
        already in progress) onto the logical board and starts piece setup.
        """
        self._player_manager = player_manager
        self._game_display = game_display
        self._panel = panel
        self._info_overlay = info_overlay
        self._menu_manager = menu_manager
        self._beep = beep
        self._set_game_result = set_game_result
        self._splash_seconds = splash_seconds
        self._rewind_to_move_count = rewind_to_move_count
        self._player1_color = player1_color
        self._on_unfinished_game = on_unfinished_game
        if show_started_splash is None:
            from .lobby import show_lichess_started_splash

            show_started_splash = show_lichess_started_splash
        self._show_started_splash = show_started_splash
        self._remote.set_on_game_connected(self._on_connected)
        self._remote.set_game_over_callback(self._on_game_over)
        self._remote.set_takeback_offer_callback(self._on_takeback_offer)
        self._remote.set_draw_offer_callback(self._on_draw_offer)
        self._remote.set_challenge_offer_callback(self._on_challenge_offer)
        self._remote.set_remote_takeback_callback(self._on_remote_takeback)
        self._remote.set_history_catch_up_callback(catch_up_moves)
        self._remote.set_info_message_callback(self._on_info_message)

    def close(self) -> None:
        """Release the session when the game is torn down.

        Cancels the started-splash timer. A game that ends inside the splash
        delay -- an opponent aborting, or BACK into the back menu -- otherwise
        left it pending, and it then drew the game widgets over whatever screen
        had replaced the game. Cancelling alone loses to a timer already firing,
        so dismissal is refused after close as well.
        """
        self._closed = True
        timer = self._splash_timer
        self._splash_timer = None
        if timer is not None:
            timer.cancel()

    def dismiss_started_splash(self) -> None:
        """Show game widgets once the started splash is done."""
        if self._closed or not self._started_splash_held:
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
        """BACK during seek cancels; BACK after accept opens abort/leave/resign."""
        if not self.game_connected:
            log.info("[Lichess] Seek cancelled")
            from .lobby import show_lichess_cancelling_splash

            show_lichess_cancelling_splash(self._panel)
            stop_players()
            return_to_menu("Lichess cancel")
            return
        correspondence = self._remote.is_correspondence
        show_back_menu(
            is_two_player=False,
            allow_abort=not correspondence,
            allow_leave=correspondence,
        )

    def _on_connected(self) -> None:
        if self.game_connected:
            return
        self.game_connected = True
        # Physical White is always player 1, Black player 2. The stream only
        # decides which of those slots the Human occupies. Flip is display-only
        # so the pieces do not have to be rotated: the chess diagram is remapped
        # and the whole panel turns 180 (menus included) when the seated player
        # is at the far end. It follows the disagreement between the color that
        # was chosen and the one the match assigned, not the assigned color on
        # its own.
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
        if self._menu_manager is not None:
            self._menu_manager.cancel_selection("BACK")
        if self._game_display is not None:
            self._game_display.set_flip_board(
                epaper_is_flipped(self._player1_color, human_is_white)
            )
        if self._show_started_splash is not None:
            self._show_started_splash(self._panel, human_is_white)
        timer = threading.Timer(self._splash_seconds, self.dismiss_started_splash)
        timer.daemon = True
        self._splash_timer = timer
        timer.start()

    def _on_challenge_offer(self, offer, accept_fn, decline_fn) -> None:
        """Show the challenger's terms. Decline restores the seek splash."""
        log.info(
            "[Lichess] Incoming challenge %s",
            getattr(offer, "challenge_id", ""),
        )
        if self.game_connected or getattr(self._remote, "_game_id", None):
            decline_fn()
            return
        if self._beep is not None:
            self._beep()
        if self._menu_manager is None:
            decline_fn()
            return
        result = self._menu_manager.show_menu(challenge_menu_entries(offer))
        if self.game_connected or getattr(self._remote, "_game_id", None):
            decline_fn()
            return
        if hasattr(result, "key") and result.key == "accept":
            accept_fn()
            return
        decline_fn()
        self._restore_waiting_splash()

    def _restore_waiting_splash(self) -> None:
        """Put 'Waiting for game' back after show_menu cleared the panel."""
        if self.game_connected or self._panel is None:
            return
        from .lobby import show_lichess_waiting_splash
        from .match import LichessSeek

        cfg = self._remote._lichess_config
        seek = LichessSeek(
            time_minutes=int(cfg.time_minutes),
            increment_seconds=int(cfg.increment_seconds),
            color=str(cfg.color_preference or "random"),
            rated=bool(cfg.rated),
            rating_range=str(
                cfg.rating_range or getattr(self._remote, "_account_range", "") or ""
            ),
            account_id=str(cfg.account_id or ""),
            host_id=getattr(self._remote, "_host_id", "") or "",
        )
        show_lichess_waiting_splash(
            self._panel,
            self.waiting_mode,
            seek=seek,
            awaiting_opponent=self.awaiting_opponent,
        )

    def _on_remote_takeback(self, remaining_plies: int) -> None:
        """Pop the live game to the ply count Lichess now has."""
        log.info("[Lichess] Remote takeback to %s half-moves", remaining_plies)
        if self._rewind_to_move_count is not None:
            self._rewind_to_move_count(remaining_plies)

    def _on_takeback_offer(self, accept_fn, decline_fn) -> None:
        log.info("[Lichess] Takeback offer received")
        if self._beep is not None:
            self._beep()
        entries = [
            IconMenuEntry(key="accept", label=t("lichess.offer.accept_takeback"), icon_name="undo"),
            IconMenuEntry(key="decline", label=t("lichess.offer.decline"), icon_name="cancel"),
        ]
        result = self._menu_manager.show_menu(entries)
        if hasattr(result, "key") and result.key == "accept":
            accept_fn()
        else:
            decline_fn()
        self._restore_game_widgets()

    def _on_draw_offer(self, accept_fn, decline_fn) -> None:
        log.info("[Lichess] Draw offer received")
        if self._beep is not None:
            self._beep()
        entries = [
            IconMenuEntry(key="accept", label=t("lichess.offer.accept_draw"), icon_name="draw"),
            IconMenuEntry(key="decline", label=t("lichess.offer.decline"), icon_name="cancel"),
        ]
        result = self._menu_manager.show_menu(entries)
        if hasattr(result, "key") and result.key == "accept":
            accept_fn()
        else:
            decline_fn()
        self._restore_game_widgets()

    def _restore_game_widgets(self) -> None:
        """Put the board back after show_menu cleared the panel."""
        if self._started_splash_held:
            return
        if self._game_display is not None:
            self._game_display.show_game_widgets()

    def _on_game_over(self, result: str, termination: str, winner) -> None:
        log.info(
            f"[Lichess] Game over: result={result}, "
            f"termination={termination}, winner={winner}"
        )
        if self._game_display is not None:
            self._game_display.stop_clock()
        if self._set_game_result is not None:
            self._set_game_result(result, termination)
        if self._on_unfinished_game is not None and termination:
            self._on_unfinished_game(termination)

    def _on_info_message(self, message: str) -> None:
        if self._info_overlay is not None:
            self._info_overlay.show_message(message, duration_seconds=5.0)
