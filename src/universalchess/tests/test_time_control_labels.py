"""Tests for the two Time Control labels the board's Settings rows show.

The rows answer different questions and must not answer the same one: the
Preset row says which preset is selected, and the Time Control row says what
the resulting clock actually does. Both were computed inside the application
module, reading the settings singleton, so neither could be tested.

They take the game settings now, which is all they ever needed.
"""

import pytest

from universalchess.menus import time_control_presets as labels
from universalchess.players.settings import GameSettings


def _game(**overrides):
    """Game settings with the clock fields set, defaulting to a plain 5-minute."""
    game = GameSettings("game")
    game.time_control_preset = ""
    game.time_control = 5
    for key, value in overrides.items():
        setattr(game, key, value)
    return game


def test_the_preset_row_names_the_selected_preset():
    """A named preset is reported by its short name.

    Why: the Preset row is the master control -- selecting a preset defines the
    whole clock -- so it has to state which one is active. How a regression
    manifests: the row reads "Basic" while a preset governs the clock, so the
    base-minutes control looks like it is in charge when it is not.
    """
    preset = labels.preset_options()[0]["value"]

    assert labels.preset_label(_game(time_control_preset=preset)) == \
        labels.preset_options()[0]["label"]


@pytest.mark.parametrize("stored", ["", "not_a_preset"])
def test_an_absent_or_unknown_preset_reads_as_basic(stored):
    """No preset, and an unrecognised one, both read as Basic.

    Why: Basic means "no preset, use the base minutes", which is exactly how
    ``build_time_control`` treats an unrecognised key -- a value left by a
    downgrade or a hand-edited file. The label must agree with the clock rather
    than name a preset that no longer exists. How a regression manifests: the
    row shows a stale preset name while the clock runs the base minutes.
    """
    assert labels.preset_label(_game(time_control_preset=stored)) == "Basic"


def test_the_time_control_row_describes_the_resolved_clock():
    """The summary reflects the clock that will actually run.

    Why: this row exists to show increments, delays, stages and time odds --
    everything the base-minutes number cannot express. How a regression
    manifests: an incremented or staged clock is summarised as plain minutes,
    so the user cannot see the clock they configured.
    """
    from universalchess.state.time_control import build_time_control

    game = _game()

    assert labels.time_control_label(game) == build_time_control(game).describe()
