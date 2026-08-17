"""Tests for the Display > Text Size scaling helper.

Guards the single source of truth mapping a text-size name to pixel font sizes,
shared by the coach panel and move list. The key invariant is that
``medium`` is the identity factor, so adopting the setting cannot silently change
the existing (unscaled) e-paper layouts.
"""

import pytest

from universalchess.epaper.text_scale import (
    DEFAULT_TEXT_SIZE,
    TEXT_SIZES,
    normalize_text_size,
    read_text_size,
    scale_font,
)


def test_default_is_medium_and_medium_is_a_valid_size():
    # The whole feature defaults to medium; if this drifts, widgets constructed
    # without an explicit size would silently render at a different scale.
    assert DEFAULT_TEXT_SIZE == "medium"
    assert "medium" in TEXT_SIZES
    assert TEXT_SIZES == ("small", "medium", "large")


def test_medium_is_identity_scale():
    # Regression guard: medium must not change any base size, otherwise every
    # existing layout (coach body 12, move font 13, line 15) would shift when the
    # setting is read even though the user never picked a non-default size.
    for base in (10, 12, 13, 15, 20):
        assert scale_font(base, "medium") == base


@pytest.mark.parametrize(
    "size, base, expected",
    [
        # Coach body base 12 -> small shrinks, large grows (round(12*0.8)=10,
        # round(12*1.25)=15). Move font base 13 -> 10 / 16. Line height base 15 ->
        # 12 / 19. Concrete values guard the chosen 0.8/1.25 factors and rounding.
        ("small", 12, 10),
        ("large", 12, 15),
        ("small", 13, 10),
        ("large", 13, 16),
        ("small", 15, 12),
        ("large", 15, 19),
    ],
)
def test_small_and_large_scale_expected_pixels(size, base, expected):
    assert scale_font(base, size) == expected


def test_small_is_smaller_and_large_is_larger_than_medium():
    # Ordering invariant independent of exact factors: the three sizes must be
    # strictly ordered so the UI options are meaningful.
    small = scale_font(20, "small")
    medium = scale_font(20, "medium")
    large = scale_font(20, "large")
    assert small < medium < large


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Large", "large"),   # case-insensitive (web form / config casing)
        (" small ", "small"),  # whitespace-tolerant
        ("", "medium"),        # blank -> default
        (None, "medium"),      # missing value -> default
        ("gigantic", "medium"),  # unknown -> default, never raises
    ],
)
def test_normalize_text_size_coerces_to_valid_name(raw, expected):
    assert normalize_text_size(raw) == expected


def test_scale_font_uses_medium_for_unknown_size():
    # An unrecognized size must fall back to identity, not raise a KeyError, so a
    # corrupted config value degrades to the safe default instead of crashing the
    # renderer.
    assert scale_font(13, "bogus") == 13


def test_scale_font_never_returns_non_positive():
    # A tiny base at the smallest factor must still be renderable (>=1); a 0px or
    # negative font would raise deep in PIL when the widget draws.
    assert scale_font(1, "small") >= 1


def test_read_text_size_normalizes_the_persisted_setting(monkeypatch):
    """Menus and widgets must see the same live setting, including dirty values.

    Why: IconMenuWidget and DisplayManager both need the current size when they
    are built, not a stale constructor default. Failure: a stored "Large" or
    blank is not normalized, so scaling is skipped or raises.
    """
    monkeypatch.setattr(
        "universalchess.board.settings.Settings.read",
        lambda section, key, default="": "Large",
    )
    assert read_text_size() == "large"

    monkeypatch.setattr(
        "universalchess.board.settings.Settings.read",
        lambda section, key, default="": "",
    )
    assert read_text_size() == DEFAULT_TEXT_SIZE
