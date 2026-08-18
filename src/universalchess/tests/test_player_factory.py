"""Tests for turning a player slot's settings into a player.

This mapping was a closure inside the 750-line game builder, so nothing could
check it: not that an unnamed engine carries its strength label into the PGN, not
that a derived novelty engine runs its policy on the shared Stockfish instead of
starting a second one, and not that an unreadable player type falls back to a
human rather than leaving the game with a side that can never move.
"""

import chess
import pytest

from universalchess.players import EnginePlayer, HandBrainPlayer, HumanPlayer
from universalchess.players.factory import build_player
from universalchess.players.hand_brain import HandBrainMode
from universalchess.players.policy_engine import PolicyEnginePlayer
from universalchess.players.settings import (
    PLAYER1_SECTION,
    PLAYER2_SECTION,
    PlayerSettings,
)
from universalchess.services.derived_engines.spec import SPECS

# A derived novelty engine: its moves come from a policy over the shared
# Stockfish's analysis rather than from an engine process of its own.
DERIVED_ENGINE = "worstfish"


def _slot(section=PLAYER1_SECTION, **overrides) -> PlayerSettings:
    """Settings for one player slot, defaulting to a nameless human."""
    return PlayerSettings(section=section, **overrides)


@pytest.fixture
def recorded_configs(monkeypatch):
    """Capture the config each player class is built with, without building one.

    The values that matter here -- think time, ponder, strength section -- reach the
    engine through its config and are not readable from the player afterwards, so
    the config is the observable output of the factory's decisions.
    """
    captured = {}

    def _recorder(kind):
        def _build(config=None, *args):
            captured[kind] = config
            return f"{kind}-player"
        return _build

    for kind in ("EnginePlayer", "HandBrainPlayer", "HumanPlayer"):
        monkeypatch.setattr(f"universalchess.players.{kind}", _recorder(kind))
    return captured


class TestSlotNumber:
    def test_the_section_names_are_the_ones_already_on_disk(self):
        # These name sections in every board's centaur.ini. Changing either -- or
        # swapping them, which the symbolic tests below could not see -- silently
        # loses the operator's saved players, or hands player one's settings to
        # player two. The literals are the contract, so they are asserted as text.
        assert PLAYER1_SECTION == "PlayerOne"
        assert PLAYER2_SECTION == "PlayerTwo"

    @pytest.mark.parametrize(
        ("section", "expected"), [(PLAYER1_SECTION, 1), (PLAYER2_SECTION, 2)]
    )
    def test_the_slot_comes_from_the_section_it_was_loaded_from(self, section, expected):
        # The slot number is only recoverable from the section name, and it decides
        # the default name and the board's Name row. It used to be derived at each
        # call site from a section constant declared in a different module.
        assert _slot(section).slot == expected

    def test_an_unrecognised_section_reads_as_the_second_slot(self):
        # A section left behind by a rename or a hand-edited config must still yield
        # a usable slot number: raising here would happen on the game thread while
        # the game is being built, leaving the board on a game screen with no game.
        assert _slot("PlayerThree").slot == 2


class TestHumanPlayers:
    def test_a_named_human_keeps_their_name(self):
        # The name reaches the PGN and the game card, so a slot with a name set
        # must not be overwritten by the slot default.
        player = build_player(_slot(type="human", name="Adrian"), chess.WHITE)

        assert isinstance(player, HumanPlayer)
        assert player.name == "Adrian"
        assert player.color == chess.WHITE

    @pytest.mark.parametrize(
        ("section", "expected"),
        [(PLAYER1_SECTION, "Player 1"), (PLAYER2_SECTION, "Player 2")],
    )
    def test_a_nameless_human_is_named_after_their_slot(self, section, expected):
        # The default is per-slot, derived from the section the settings came from.
        # A single shared default would put two "Player 1"s in the same PGN.
        player = build_player(_slot(section, type="human"), chess.BLACK)

        assert player.name == expected

    def test_an_unreadable_player_type_falls_back_to_a_human(self):
        # A type left behind by a downgrade or a hand-edited config must still
        # produce a player: returning nothing would leave the game with a side
        # that can never move, which reads on the board as a game frozen at move 1.
        player = build_player(_slot(PLAYER2_SECTION, type="martian"), chess.WHITE)

        assert isinstance(player, HumanPlayer)
        assert player.name == "Player 2"
        assert player.color == chess.WHITE


class TestEnginePlayers:
    def test_a_nameless_engine_is_labelled_with_its_strength(self, monkeypatch):
        # The label is what the game card and the PGN show, and it comes from the
        # engine's own strength schema rather than the raw settings value -- an
        # uncapped section reads as "Unlimited" there, not "Default". A factory
        # that used slot.elo directly would put the config key in the PGN.
        monkeypatch.setattr(
            "universalchess.services.uci_schema.strength_display_for_engine",
            lambda engine, elo: "Unlimited",
        )

        player = build_player(
            _slot(type="engine", engine="stockfish", elo="Default"),
            chess.BLACK,
            ponder=False,
        )

        assert player.name == "Stockfish (Unlimited)"

    def test_a_named_engine_keeps_the_operator_name(self):
        # An operator who renamed the slot wants that name in the PGN, not the
        # generated one.
        player = build_player(
            _slot(type="engine", engine="stockfish", name="The Opponent"),
            chess.WHITE,
            ponder=False,
        )

        assert player.name == "The Opponent"

    def test_the_strength_and_timing_reach_the_engine(self, recorded_configs):
        # think_time is stored as whole seconds and must arrive as the float the
        # engine config expects; ponder is a game setting, not a slot setting, so a
        # factory that read it from the slot would always leave it off. Neither is
        # readable from the player afterwards, and both silently change how the
        # engine spends its time.
        build_player(
            _slot(type="engine", engine="stockfish", elo="1400", think_time=9),
            chess.WHITE,
            ponder=True,
        )

        config = recorded_configs["EnginePlayer"]
        assert config.engine_name == "stockfish"
        assert config.elo_section == "1400"
        assert config.time_limit_seconds == 9.0
        assert config.ponder is True
        assert config.color == chess.WHITE

    def test_a_derived_engine_runs_its_policy_on_the_shared_engine(self):
        # Worstfish and Drawfish pick from the shared pooled Stockfish's analysis.
        # Building one as a plain EnginePlayer would start a second Stockfish, which
        # on a 512MB board ends the game with the OOM killer rather than a bad move.
        # PolicyEnginePlayer subclasses EnginePlayer, so the exact type is checked.
        player = build_player(
            _slot(type="engine", engine=DERIVED_ENGINE), chess.BLACK, ponder=False
        )

        assert type(player) is PolicyEnginePlayer
        assert player.engine_name == DERIVED_ENGINE

    def test_an_ordinary_engine_is_not_given_a_policy(self):
        # The other half of the branch: a normal engine must run as itself. Sending
        # every engine through the policy path would silently replace the chosen
        # engine's play with Stockfish's.
        player = build_player(
            _slot(type="engine", engine="stockfish"), chess.BLACK, ponder=False
        )

        assert type(player) is EnginePlayer
        assert "stockfish" not in SPECS


class TestHandBrainPlayers:
    @pytest.mark.parametrize(
        ("configured", "mode", "label"),
        [("normal", HandBrainMode.NORMAL, "N"), ("reverse", HandBrainMode.REVERSE, "R")],
    )
    def test_the_mode_reaches_the_player_and_its_label(
        self, configured, mode, label, recorded_configs
    ):
        # Normal and reverse decide who names the piece and who moves it, so a mode
        # that does not reach the player inverts the whole game. The label carries
        # it into the PGN, where it is the only record of which way round it was.
        build_player(
            _slot(type="hand_brain", engine="stockfish", hand_brain_mode=configured),
            chess.WHITE,
        )

        config = recorded_configs["HandBrainPlayer"]
        assert config.mode is mode
        assert config.name == f"H+B {label} (Stockfish)"
        assert config.engine_name == "stockfish"

    def test_a_named_hand_brain_pair_keeps_the_operator_name(self):
        player = build_player(
            _slot(type="hand_brain", engine="stockfish", name="Team Human"),
            chess.WHITE,
        )

        assert isinstance(player, HandBrainPlayer)
        assert player.name == "Team Human"
        assert player.mode is HandBrainMode.NORMAL


class TestLichessPlayers:
    def test_the_seek_and_join_are_handed_to_the_lichess_player(self, monkeypatch):
        # The seek describes the game to post and the join names an existing game to
        # attach to. Losing either is how a lobby "Seek New Game" could start a
        # local engine game instead, so both are asserted to arrive intact.
        seen = {}

        def _from_seek(seek, *, color, join):
            seen.update(seek=seek, color=color, join=join)
            return "lichess-player"

        monkeypatch.setattr(
            "universalchess.players.lichess.lichess_player_from_seek", _from_seek
        )
        seek = object()
        join = {"mode": "NEW"}

        player = build_player(
            _slot(type="lichess"), chess.BLACK, lichess_seek=seek, lichess_join=join
        )

        assert player == "lichess-player"
        assert seen == {"seek": seek, "color": chess.BLACK, "join": join}

    def test_a_lichess_slot_with_no_seek_is_still_built(self, monkeypatch):
        # Attaching to a game in progress posts no seek, so None is the normal case
        # rather than an error; raising here would break resume after a restart.
        monkeypatch.setattr(
            "universalchess.players.lichess.lichess_player_from_seek",
            lambda seek, *, color, join: (seek, join),
        )

        assert build_player(_slot(type="lichess"), chess.WHITE) == (None, None)
