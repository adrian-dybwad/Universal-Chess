"""PlayerType is a move-source tag. It does not name providers.

Why these tests exist
---------------------
``PlayerType.LICHESS`` put a plugin name on the base class. The game only
needs to know whether a move is constructed on the board, computed locally,
or arrives from outside. A board-reset rebuild is a capability on the player
that is still attached to an external game, not a type named Lichess.

How a regression manifests
--------------------------
``LICHESS`` reappears on the enum, ``LichessPlayer.player_type`` is not
``REMOTE``, or Human/Engine report that they cannot continue in place.
"""

from universalchess.players.base import PlayerType
from universalchess.players.human import HumanPlayer
from universalchess.players.lichess import LichessPlayer
from universalchess.players.manager import PlayerManager


def test_player_type_does_not_name_providers():
    """The enum is HUMAN, ENGINE, REMOTE — not Lichess or any other plugin.

    Failure: LICHESS (or another provider) is a member, so core still lists
    who exists.
    """
    assert {member.name for member in PlayerType} == {"HUMAN", "ENGINE", "REMOTE"}


def test_lichess_player_is_remote():
    """Lichess is one remote source, not its own PlayerType.

    Failure: player_type is still LICHESS (or HUMAN/ENGINE).
    """
    assert LichessPlayer().player_type is PlayerType.REMOTE


def test_human_does_not_require_rebuild_on_new_game():
    """A local human continues in place after a board-reset new game.

    Failure: Human reports True and the controller skips the in-place move
    request the way it must for an attached remote game.
    """
    assert HumanPlayer().requires_rebuild_on_new_game is False


def test_lichess_requires_rebuild_on_new_game():
    """A Lichess player is still attached to the remote game after a local reset.

    Failure: False, so EVENT_NEW_GAME requests a move against the abandoned
    stream while the board is at start.
    """
    assert LichessPlayer().requires_rebuild_on_new_game is True


def test_player_manager_rebuild_follows_either_side():
    """The manager is true when either slot cannot continue in place.

    Failure: Human vs Lichess reports False (only checking White) or Human vs
    Human reports True.
    """
    local = PlayerManager(HumanPlayer(), HumanPlayer())
    assert local.requires_rebuild_on_new_game is False
    remote_black = PlayerManager(HumanPlayer(), LichessPlayer())
    assert remote_black.requires_rebuild_on_new_game is True
    remote_white = PlayerManager(LichessPlayer(), HumanPlayer())
    assert remote_white.requires_rebuild_on_new_game is True
