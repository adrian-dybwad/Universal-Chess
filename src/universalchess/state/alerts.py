"""In-play alert policy: which warning, if any, the current position raises.

Pure module (python-chess only): no settings IO, no display, no observers. Every
surface that shows an in-play warning resolves it here -- the e-paper AlertWidget
text and its LED flash, the three-color red highlight, and the web live-board
banner -- so a warning the player has switched off cannot survive in one of them.
Each surface previously re-derived the rule from the position itself, which is
how a queen could still be reddened and pointed at by the LEDs with nothing on
screen saying why.

Preferences are injected as a value object instead of being read from the config
here, mirroring ``state/time_control.build_time_control``: the rule stays a pure
function of (position, preferences) and is testable without a config file.
"""

from dataclasses import dataclass
from typing import Optional

import chess

# Alert kinds. These strings are also the AlertWidget's alert types and the
# ``alert`` value in the web game_state broadcast, so they are part of both
# surfaces' contracts and must not be renamed casually.
CHECK = "check"
QUEEN_THREAT = "queen"


@dataclass(frozen=True)
class AlertPreferences:
    """Which in-play warnings the player wants shown.

    CHECK deliberately has no flag. An unanswered check makes every other move
    illegal, so hiding it would let the player build an impossible position on
    the physical board rather than merely withholding advice.

    Attributes:
        queen_threat: Show the YOUR QUEEN warning when the side to move's own
            queen is attacked. Defaults to True -- the board's long-standing
            behavior -- so any caller with no settings to read (a widget built
            before settings load, a test) warns exactly as before.
    """

    queen_threat: bool = True

    @classmethod
    def from_game_settings(cls, settings) -> "AlertPreferences":
        """Read the preferences from a GameSettings-like object.

        A missing attribute resolves to the warning being enabled, which is the
        same answer the persisted default gives: the object may be a config
        written before the setting existed, or a partial test double, and
        neither should raise or silently disable a warning.

        Args:
            settings: Object exposing the ``alert_*`` game settings as attributes.

        Returns:
            The preferences those settings describe.
        """
        return cls(queen_threat=bool(getattr(settings, "alert_queen_threat", True)))


@dataclass(frozen=True)
class Alert:
    """A warning about the side-to-move's own piece being attacked.

    Attributes:
        kind: :data:`CHECK` or :data:`QUEEN_THREAT`.
        is_black_threatened: True when the threatened piece is Black's. Selects
            the alert's background/text colors on the e-paper.
        target_square: Square of the threatened piece (checked king or queen).
        attacker_square: A single attacking square, used as the LED flash source.
            The lowest-numbered attacker, so repeated renders of one position
            always flash from the same square.
        attacker_squares: Every attacking square, used by the red highlight.
    """

    kind: str
    is_black_threatened: bool
    target_square: int
    attacker_square: int
    attacker_squares: frozenset[int]


def _alert(kind: str, side: chess.Color, target_square: int, attackers) -> Alert:
    """Build an Alert for a threatened piece of ``side`` on ``target_square``."""
    attacker_squares = frozenset(attackers)
    return Alert(
        kind=kind,
        is_black_threatened=(side == chess.BLACK),
        target_square=target_square,
        attacker_square=min(attacker_squares),
        attacker_squares=attacker_squares,
    )


def find_check(board: chess.Board) -> Optional[Alert]:
    """The CHECK alert for the side to move, or None if it is not in check.

    Args:
        board: Position to inspect.

    Returns:
        The alert naming the checked king and its checkers, or None.
    """
    if not board.is_check():
        return None

    side_in_check = board.turn
    king_square = board.king(side_in_check)
    checkers = board.checkers()
    if king_square is None or not checkers:
        return None
    return _alert(CHECK, side_in_check, king_square, checkers)


def find_queen_threat(board: chess.Board) -> Optional[Alert]:
    """The QUEEN_THREAT alert for the side to move, or None if its queen is safe.

    Flags the SIDE-TO-MOVE's own queen -- the actionable "your queen is hanging,
    deal with it this move" warning -- deliberately paralleling :func:`find_check`
    so CHECK and YOUR QUEEN read consistently: both warn the player on move about
    their own royalty.

    A prior implementation flagged the OPPONENT's queen that the side to move
    could capture. That is an opportunity indicator, not a warning: with two
    queens simultaneously en prise it highlighted the enemy queen the mover could
    grab rather than the mover's own queen in danger, so the alert pointed at the
    wrong queen while it was "the wrong colour's" turn.

    Args:
        board: Position to inspect.

    Returns:
        The alert naming the threatened queen and its attackers, or None.
    """
    side_to_move = board.turn
    queens = board.pieces(chess.QUEEN, side_to_move)
    if not queens:
        return None

    queen_square = min(queens)
    attackers = board.attackers(not side_to_move, queen_square)
    if not attackers:
        return None
    return _alert(QUEEN_THREAT, side_to_move, queen_square, attackers)


def resolve_alert(board: chess.Board, preferences: AlertPreferences) -> Optional[Alert]:
    """The single warning to show for this position, or None for a quiet one.

    Only one alert is shown at a time and check outranks a queen threat: a player
    in check must answer it, so a simultaneously attacked queen is secondary.

    ``preferences`` is required rather than defaulted so each surface states which
    settings it is honoring; a surface that silently used defaults is exactly how
    a disabled warning used to survive in one place.

    Args:
        board: Position to inspect.
        preferences: Which warnings the player wants shown.

    Returns:
        The alert to show, or None when the position is quiet or the only
        applicable warning is disabled.
    """
    check = find_check(board)
    if check is not None:
        return check

    if not preferences.queen_threat:
        return None
    return find_queen_threat(board)
