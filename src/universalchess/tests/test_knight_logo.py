"""Knight logo accessors: square head vs full portrait piece.

These guard the two-variant split introduced so the splash can show the full
knight piece while square placements (menu/icon buttons, web icons) use the head
crop. The shipped bitmaps under ``resources/`` are used directly so the test also
catches a missing or mis-shaped asset.
"""

import pathlib

import pytest

from universalchess.resources import ResourceLoader

RES_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "resources")


@pytest.fixture
def loader() -> ResourceLoader:
    return ResourceLoader(RES_DIR)


@pytest.mark.parametrize("size", [80, 36, 24, 20])
def test_head_logo_is_square_bilevel_with_ink_mask(loader: ResourceLoader, size: int) -> None:
    # Why: icon/menu buttons request the head at these exact sizes; a regression
    # to non-square output (e.g. reintroducing the full piece here) would show as
    # a mismatched logo size != (size, size). The mask must mark ink so the
    # e-paper background shows through the knight's body.
    logo, mask = loader.get_knight_logo(size)
    assert logo is not None and mask is not None
    assert logo.size == (size, size)
    assert mask.size == (size, size)
    assert logo.mode == "1" and mask.mode == "1"
    # Some ink present -> mask has opaque (255) pixels; a blank/all-white logo
    # (the failure when thresholding drops everything) would have none.
    assert mask.getextrema()[1] == 255


@pytest.mark.parametrize("height", [140, 100])
def test_full_logo_is_portrait_and_height_exact(loader: ResourceLoader, height: int) -> None:
    # Why: the splash reserves a tall band and centers the logo on its true
    # width; if the full piece were ever square/landscape the centering and band
    # height would be wrong. Height must be exact (drives layout) and width must
    # be strictly smaller (portrait) and derived from the source aspect.
    logo, mask = loader.get_knight_logo_full(height)
    assert logo is not None and mask is not None
    assert logo.height == height
    assert logo.width < height  # portrait piece, never squashed to square
    assert logo.size == mask.size
    assert logo.mode == "1" and mask.mode == "1"
    assert mask.getextrema()[1] == 255


def test_full_and_head_are_distinct_crops(loader: ResourceLoader) -> None:
    # Why: both must resolve to their own bitmap. If they accidentally shared one
    # source, the aspect ratios would match; the head is square (1.0) while the
    # full piece is clearly portrait (< 0.75), so their aspects must differ.
    head, _ = loader.get_knight_logo(120)
    full, _ = loader.get_knight_logo_full(240)
    head_aspect = head.width / head.height
    full_aspect = full.width / full.height
    assert head_aspect == pytest.approx(1.0)
    assert full_aspect < 0.75
