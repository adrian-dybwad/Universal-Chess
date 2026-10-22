"""Seven-segment clock digit sheet and e-paper renderer.

Why these tests exist
---------------------
Chess clock times on the e-paper used the proportional TrueType face, which
does not match the fixed-pitch seven-segment look of a physical chess clock.
The board now draws digit/colon times from ``clock_digits.bmp`` (16×24 cells,
trimmed to 12 px with a 2 px gap). These tests lock the sheet geometry, the
trim/advance contract, and that non-digit strings (FLAG, ``N h M m``) are
rejected so the widget can fall back to TrueType.

How a regression manifests
--------------------------
Wrong sheet size or empty cells: digits missing or clipped on the panel.
Wrong trim/gap: times overflow the right column or look spaced like Centaur
did not. Accepting ``FLAG`` as drawable: blank or garbage where the word
should appear.
"""

from __future__ import annotations

import pathlib

import pytest
from PIL import Image

from universalchess.epaper import clock_digits
from universalchess.resources import ResourceLoader

RES_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "resources")


@pytest.fixture
def sheet() -> Image.Image:
    loader = ResourceLoader(RES_DIR)
    image = loader.get_image("clock_digits.bmp")
    assert image is not None, "clock_digits.bmp must ship under resources/"
    return image.convert("1")


def test_sheet_is_thirteen_sixteen_by_twenty_four_cells(sheet: Image.Image) -> None:
    # Why: Centaur-compatible layout — 13 cells of 16×24 (digits 0–9 + colon).
    # A wrong size breaks cell indexing and pastes garbage into the clock.
    assert sheet.size == (clock_digits.SHEET_WIDTH, clock_digits.SHEET_HEIGHT)
    assert sheet.mode == "1"
    assert clock_digits.SHEET_WIDTH == clock_digits.CELL_COUNT * clock_digits.CELL_WIDTH


def test_every_digit_and_colon_cell_has_ink(sheet: Image.Image) -> None:
    # Why: a blank cell (failed generator, wrong polarity) draws an empty
    # slot on the clock. Each of cells 0–10 must contain black (0) pixels.
    for index in range(11):
        glyph = clock_digits.glyph_for_index(sheet, index)
        assert any(p == 0 for p in glyph.getdata()), f"cell {index} has no ink"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0", True),
        ("4:55", True),
        ("1:05:23", True),
        ("FLAG", False),
        ("10 h 0 m", False),
        ("1 day 2 h", False),
        ("", False),
    ],
)
def test_is_digit_clock_string(text: str, expected: bool) -> None:
    # Why: only pure digit/colon strings can be stamped from the sheet; FLAG
    # and long-form remainders must keep using TrueType. Accepting FLAG would
    # leave a blank time; rejecting "4:55" would lose the seven-segment look.
    assert clock_digits.is_digit_clock_string(text) is expected


def test_measure_uses_twelve_px_glyphs_and_two_px_gaps() -> None:
    # Why: the right-aligned clock column budgets ~64 px; the advance must stay
    # 12+2 so "M:SS" (4 glyphs → 54 px) fits and "H:MM:SS" is predictable.
    # Failure: wrong width pushes times into the name column or leaves a gap.
    assert clock_digits.measure("4:55") == 4 * 12 + 3 * 2
    assert clock_digits.measure("3:38") == 54
    # "1:05:23" is seven glyphs (digit, colon, digit, digit, colon, digit, digit).
    assert clock_digits.measure("1:05:23") == 7 * 12 + 6 * 2
    assert clock_digits.measure("0") == 12


def test_draw_pastes_trimmed_glyphs_with_gaps(sheet: Image.Image) -> None:
    # Why: each cell is stored 16 wide but only the left 12 columns are ink;
    # a 2 px gap separates glyphs. Pasting the full 16 would overlap neighbours.
    # Failure: overlapping digits, or a measured width that does not match ink.
    canvas = Image.new("1", (80, 30), 255)
    width = clock_digits.draw(canvas, 2, 3, "4:55", sheet)
    assert width == clock_digits.measure("4:55")

    pixels = canvas.load()
    # Ink must appear inside the first glyph's 12×24 box, not in the 2 px gap
    # after it (x = 2+12 .. 2+12+1).
    assert any(
        pixels[x, y] == 0
        for x in range(2, 2 + 12)
        for y in range(3, 3 + 24)
    )
    gap_x0 = 2 + 12
    assert all(pixels[gap_x0, y] == 255 for y in range(3, 3 + 24))
    assert all(pixels[gap_x0 + 1, y] == 255 for y in range(3, 3 + 24))


def test_draw_scales_when_target_height_differs(sheet: Image.Image) -> None:
    # Why: the compact timed clock section is shorter than 24 px; digits must
    # nearest-neighbour scale so they still fit without clipping the separator.
    # Failure: drawing at full 24 overflows the section, or scale ignores height.
    canvas = Image.new("1", (80, 20), 255)
    width = clock_digits.draw(canvas, 0, 0, "1:00", sheet, height=16)
    assert width == clock_digits.measure("1:00", height=16)
    # Scaled glyph height is 16; no ink should land on row 16+ of this canvas
    # (canvas is only 20 tall — assert the draw stayed within 0..15).
    pixels = canvas.load()
    assert any(pixels[x, y] == 0 for x in range(width) for y in range(16))
    assert all(pixels[x, y] == 255 for x in range(width) for y in range(16, 20))


def test_unknown_character_is_rejected_before_draw(sheet: Image.Image) -> None:
    # Why: draw must not invent a cell index for letters; callers check
    # is_digit_clock_string first. Drawing "FLAG" would IndexError or paste
    # the wrong cell.
    with pytest.raises(ValueError):
        clock_digits.draw(Image.new("1", (40, 24), 255), 0, 0, "FLAG", sheet)


def test_chess_clock_widget_stamps_seven_segment_times() -> None:
    # Why: the timed clock must use the bitmap sheet for M:SS remainders, not
    # TrueType. Without a loader the widget falls back silently; this wires the
    # bundled resources and checks ink appears in the right-hand time column.
    # Failure: empty right column (still on TrueType with a blank default font)
    # or ink only on the left (labels/circles only).
    from unittest.mock import MagicMock

    from universalchess.epaper.chess_clock import ChessClockWidget
    from universalchess.resources import (
        ResourceLoader,
        get_resource_loader,
        set_resource_loader,
    )
    from universalchess.state.chess_clock import get_chess_clock, reset_chess_clock
    from universalchess.state.chess_game import get_chess_game, reset_chess_game
    from universalchess.state.players import reset_players_state

    clock_digits.reset_sheet_cache()
    previous = get_resource_loader()
    set_resource_loader(ResourceLoader(RES_DIR))
    reset_chess_clock()
    reset_chess_game()
    reset_players_state()
    try:
        clock = get_chess_clock()
        clock.set_timed_mode(True)
        clock.set_times(295, 218)  # 4:55 and 3:38
        get_chess_game().push_uci("e2e4")

        widget = ChessClockWidget(
            0, 144, 128, 72, update_callback=MagicMock(), timed_mode=True
        )
        img = Image.new("1", (widget.width, widget.height), 255)
        widget.render(img)
        widget.stop()

        pixels = img.load()
        # Right-hand time column (x >= width-64): must have seven-segment ink.
        right_ink = sum(
            1
            for y in range(widget.height)
            for x in range(widget.width - 64, widget.width)
            if pixels[x, y] == 0
        )
        assert right_ink > 50, (
            f"expected seven-segment ink in time column, got {right_ink}"
        )
    finally:
        clock_digits.reset_sheet_cache()
        set_resource_loader(previous)
        reset_chess_clock()
        reset_chess_game()
        reset_players_state()
