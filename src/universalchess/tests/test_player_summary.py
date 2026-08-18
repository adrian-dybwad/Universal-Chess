"""Tests for the player summaries the board draws under Settings and Players.

The same question -- "what is this player, in one line?" -- was answered by two
functions that disagreed. The Settings > Players row capitalised the engine id
(``stockfish`` -> ``Stockfish``), while the per-player row on the Players menu
showed it raw, so one screen said "Stockfish vs Human" and the next said
"stockfish (White)" about the same engine. Neither was right: engines carry a
display name (``Stockfish``, ``CT800``), which capitalising an id cannot produce.

There is one summary now, and it uses that display name.
"""

import pytest

from universalchess.menus import settings_menu


def _player(**overrides):
    """A player settings dict, defaulting to a white human."""
    player = {
        "type": "human",
        "engine": "stockfish",
        "hand_brain_mode": "normal",
        "color": "white",
    }
    player.update(overrides)
    return player


def test_an_engine_player_is_named_by_its_display_name():
    """An engine is shown as its catalog display name, not its id.

    Why: the id is a filesystem-safe token (``ct800``), and both surfaces used
    to derive their label from it -- one raw, one capitalised, giving ``ct800``
    and ``Ct800``, neither of which is the engine's name. How a regression
    manifests: the board shows an id or a mis-capitalised one where the engine
    manager, the web and the PGN all show the real name.
    """
    summary = settings_menu.player_summary(_player(type="engine"), with_color=False)

    assert summary == "Stockfish"


def test_the_players_row_and_the_player_row_agree_about_the_engine():
    """Both summaries name the engine identically.

    Why: this is the divergence the shared helper removes -- two screens, one
    engine, two spellings. How a regression manifests: a second copy of the
    branching appears and the two rows disagree again.
    """
    engine = _player(type="engine")
    human = _player(type="human", color="black")

    row = settings_menu.player_summary(engine, with_color=False)
    combined = settings_menu._get_players_summary(engine, human)

    assert combined.startswith(f"{row}\nvs ")


@pytest.mark.parametrize("mode,expected", [("normal", "H+B N"), ("reverse", "H+B R")])
def test_hand_brain_shows_its_mode(mode, expected):
    """Hand+Brain is distinguished by mode, in one character.

    Why: normal and reverse are different games, and the row is one line wide,
    so the mode has to survive the abbreviation. How a regression manifests:
    both modes read the same and the user cannot tell which is configured.
    """
    player = _player(type="hand_brain", hand_brain_mode=mode)

    assert settings_menu.player_summary(player, with_color=False) == expected


def test_only_the_first_player_carries_a_colour():
    """Player 1's summary names its colour; Player 2's does not.

    Why: Player 2 always plays the opposite colour, so stating it twice is
    noise, and the Players row summarises both players in one line where
    colours do not fit at all. How a regression manifests: the Players row
    grows "(White)" fragments, or Player 1 loses the only place its colour is
    shown.
    """
    player = _player(color="black")

    assert settings_menu.player_summary(player, with_color=True) == "Human (Black)"
    assert settings_menu.player_summary(player, with_color=False) == "Human"
    assert "(" not in settings_menu._get_players_summary(player, _player())


def test_an_unknown_engine_id_is_shown_as_itself():
    """An engine with no catalog entry is named by its id, not invented.

    Why: a custom engine the operator removed, or a stale setting, still has to
    render something the user can recognise and act on. Fabricating a prettier
    name would hide which id is actually configured. How a regression
    manifests: the row shows a name that matches no engine on the device.
    """
    player = _player(type="engine", engine="not_an_engine")

    assert settings_menu.player_summary(player, with_color=False) == "not_an_engine"
