"""Tests for the shared ``[display]`` settings reader.

The board process and the web process both read these settings, and until now
each had its own parser -- one of which carried a docstring promising it matched
the other, which is the shape of a bug rather than a guarantee. A stored value
that the two read differently means the panel behaves one way and the web card
reports another, with nothing in either file to show why.

There is one reader now, and these tests pin what it accepts.
"""

import pytest

from universalchess.board import display_settings


@pytest.fixture
def stored(monkeypatch):
    """Return a dict standing in for the ``[display]`` section on disk."""
    from universalchess.board.settings import Settings

    values = {}
    monkeypatch.setattr(
        Settings, "read",
        staticmethod(lambda section, key, default=None:
                     values.get(key, default) if section == "display" else default),
    )
    return values


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "on", "yes", " yes "])
def test_the_spellings_of_on_are_all_accepted(stored, value):
    """Every spelling the settings file may hold reads as on.

    Why: the value is written by the web UI, by hand over SSH, and by older
    versions of this product, so all of these appear in the wild. How a
    regression manifests: a board with ``high_contrast = yes`` silently runs
    without it, and nothing on screen explains why.
    """
    stored["high_contrast"] = value

    assert display_settings.read_flag("high_contrast") is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "", "nonsense"])
def test_anything_else_reads_as_off(stored, value):
    """Unrecognised text is off, not an error and not on.

    Why: this parses a hand-editable file, and an unreadable value must leave
    the panel in its shipped state rather than enabling an override nobody
    asked for. How a regression manifests: a typo turns a display option on.
    """
    stored["three_color"] = value

    assert display_settings.read_flag("three_color") is False


def test_an_absent_key_takes_the_shipped_default(stored):
    """A never-configured board gets the behaviour the product ships with.

    Why: update batching ships on and the contrast override ships off, and the
    difference is carried entirely by this argument -- the settings file on a
    fresh board mentions neither. How a regression manifests: a new board runs
    unbatched (visibly slower menu navigation) or with the override on.
    """
    assert display_settings.read_flag("batch_updates", default=True) is True
    assert display_settings.read_flag("high_contrast") is False


def test_the_waveform_key_is_returned_as_stored(stored):
    """The profile key is not resolved here, only read.

    Why: the same key is resolved differently per controller, so resolving it
    at read time would bake in one controller's answer. Surrounding whitespace
    is dropped because a hand-edited file has it. How a regression manifests: a
    key with a trailing space resolves to the fallback profile, so the panel
    quietly runs a waveform the user did not choose.
    """
    stored["waveform_profile"] = "  uc8151d_t5d \n"

    assert display_settings.read_waveform_profile_key() == "uc8151d_t5d"
    assert display_settings.read_selection() == ("uc8151d_t5d", False)


def test_both_processes_read_through_this_module():
    """The board's panel bring-up and the web's tuning card share one reader.

    Why: this is the duplication the module was written to remove. The two
    copies were byte-identical, which is exactly why they could drift without
    anyone noticing. How a regression manifests: a second copy appears and the
    two surfaces disagree about a stored value.
    """
    from universalchess.app import display_boot
    import universalchess.web.app as webapp

    assert display_boot.read_flag is display_settings.read_flag
    assert "display_settings.read_flag" in _source_of(webapp)


def _source_of(module):
    from pathlib import Path

    return Path(module.__file__).read_text()
