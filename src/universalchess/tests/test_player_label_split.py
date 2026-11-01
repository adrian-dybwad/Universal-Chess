"""Tests for separate player name / Elo on PlayersState and clock line spacing.

Why: packing ``alice(1500)`` into the name and splitting it later for the
e-paper clock was a round-trip that truncated Elo. Names and ratings are
stored apart; the clock only lays out the lines.
"""

from universalchess.epaper.chess_clock import evenly_spaced_line_ys
from universalchess.state.players import (
    format_player_label,
    normalize_rating,
    reset_players_state,
)


def test_players_state_keeps_name_and_rating_apart():
    # Lichess identity must not be stored as a joined label. How a regression
    # manifests: white_name is alice(1500) and white_rating is empty.
    state = reset_players_state()
    state.set_player_names("alice", "bob", white_rating="1500", black_rating=1400)
    assert state.white_name == "alice"
    assert state.black_name == "bob"
    assert state.white_rating == "1500"
    assert state.black_rating == "1400"


def test_set_player_names_without_ratings_clears_elo():
    # A local / engine game must not keep a leftover Lichess Elo. How a
    # regression manifests: white_rating stays 1500 after set_player_names
    # with only Human / Stockfish names.
    state = reset_players_state()
    state.set_player_names("alice", "bob", white_rating="1500", black_rating="1400")
    state.set_player_names("Human", "Stockfish")
    assert state.white_name == "Human"
    assert state.black_name == "Stockfish"
    assert state.white_rating == ""
    assert state.black_rating == ""


def test_normalize_rating_and_display_label():
    # Display edges may still join for one string; storage uses normalize.
    # How a regression manifests: None becomes the string "None", or LiveBoard
    # shows alice() for an empty rating.
    assert normalize_rating(None) == ""
    assert normalize_rating(" 1500 ") == "1500"
    assert format_player_label("alice", "1500") == "alice(1500)"
    assert format_player_label("alice", "") == "alice"
    assert format_player_label("alice", None) == "alice"


def test_evenly_spaced_line_ys_three_lines():
    # Three lines (color, user, Elo) must sit at equal steps across the section.
    # How a regression manifests: uneven gaps or the third line past the section.
    ys = evenly_spaced_line_ys(section_y=10, section_height=34, line_count=3)
    assert ys == [10, 10 + 34 // 3, 10 + 2 * (34 // 3)]
    assert ys[-1] + (34 // 3) <= 10 + 34


def test_evenly_spaced_line_ys_two_and_one():
    # Name-only and color-only layouts still need defined positions. How a
    # regression manifests: ZeroDivisionError or a single line not at the top.
    assert evenly_spaced_line_ys(0, 30, 1) == [0]
    assert evenly_spaced_line_ys(5, 30, 2) == [5, 5 + 30 // 2]
    assert evenly_spaced_line_ys(0, 40, 0) == []
