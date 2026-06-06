"""Tests for chess sprite-sheet discovery and selectable loading in ResourceLoader.

Background / why these tests exist
----------------------------------
The display menu lets the user pick a chess sprite set among all resources named
``chesssprites_<id>.bmp`` (``chesssprites_default.bmp`` is the default). These
tests pin the two pieces of behavior the menu and board widget rely on:

1. list_chess_sprite_sheets() discovers every ``chesssprites_*`` sheet across the
   user and system resource directories, returns their identifiers (the part
   after ``chesssprites_`` and before ``.bmp``), de-duplicates user/system
   overrides, and always lists ``default`` first.
2. get_chess_sprites(name) loads the matching sheet (by filename), converts it to
   1-bit mode, and caches per-name so different selections return different
   images and a missing sheet yields None.

Note on PIL: several other test modules replace PIL with a MagicMock at import
time and never restore it, which also breaks PIL's lazy BMP plugin registration
for the rest of the session. These tests therefore avoid BMP disk encoding: the
discovery tests touch empty files (filenames are all that matter), and the
loader tests stub get_image() - the filesystem/decode boundary - returning real
in-memory images built with Image.new (which does not need plugins).
"""

import os
import sys
from pathlib import Path

import pytest

import universalchess.resources as resources_mod
from universalchess.resources import ResourceLoader

Image = None  # bound to the real PIL.Image by the autouse fixture below


@pytest.fixture(autouse=True)
def _real_pil():
    """Bind the real PIL.Image (for Image.new) regardless of mock pollution.

    Other test modules replace PIL with a MagicMock in sys.modules at import
    time; whichever import of resources.py happened under that mock leaves
    resources_mod.Image bound to the mock for the rest of the session. Restore a
    real PIL and rebind it both here and in resources so get_chess_piece_preview
    (which calls Image.new for its mask) works.
    """
    global Image
    for name in ("PIL.ImageFont", "PIL.ImageDraw", "PIL.Image", "PIL"):
        sys.modules.pop(name, None)
    import PIL.Image as real_image
    Image = real_image
    resources_mod.Image = real_image
    yield


def _touch_sheet(directory: str, identifier: str) -> None:
    """Create an empty chesssprites_<identifier>.bmp (content irrelevant to discovery)."""
    Path(directory, f"chesssprites_{identifier}.bmp").touch()


# ---------------------------------------------------------------------------
# Discovery: list_chess_sprite_sheets()
# ---------------------------------------------------------------------------

def test_list_sheets_discovers_ids_default_first_and_dedupes(tmp_path):
    """list_chess_sprite_sheets must return de-duplicated ids with default first.

    Why: the cycle selector iterates this list; a missing/duplicated id would
    skip or repeat a sheet, and default must be the well-known first entry so a
    fresh install starts there.

    How the regression manifests: if non-matching files leaked in, identifiers
    were parsed wrong, user/system duplicates were not merged, or default were
    not pinned first, the asserted list would differ.
    """
    system_dir = tmp_path / "system"
    user_dir = tmp_path / "user"
    system_dir.mkdir()
    user_dir.mkdir()

    # System sheets plus decoys that must be ignored.
    _touch_sheet(str(system_dir), "default")
    _touch_sheet(str(system_dir), "fen")
    _touch_sheet(str(system_dir), "retro")
    Path(system_dir, "chesssprites.bmp").touch()  # no underscore id
    Path(system_dir, "knight_logo.bmp").touch()   # unrelated

    # User dir adds one new sheet and overrides an existing id (must de-dupe).
    _touch_sheet(str(user_dir), "custom")
    _touch_sheet(str(user_dir), "fen")

    loader = ResourceLoader(str(system_dir), str(user_dir))

    sheets = loader.list_chess_sprite_sheets()

    # default first, remaining ids alphabetical, each id present exactly once.
    assert sheets == ["default", "custom", "fen", "retro"]


def test_list_sheets_empty_when_none_present(tmp_path):
    """No chesssprites_ files -> empty list (selector has nothing to cycle).

    Why: guards against returning a bogus default id when the resource dir is
    missing the sheets entirely.
    """
    loader = ResourceLoader(str(tmp_path))
    assert loader.list_chess_sprite_sheets() == []


# ---------------------------------------------------------------------------
# Loading: get_chess_sprites(name)
# ---------------------------------------------------------------------------

def _loader_with_stubbed_images(images_by_filename):
    """Build a loader whose get_image() returns the provided in-memory images.

    images_by_filename maps a resource filename to a PIL Image (or None).
    Stubbing get_image isolates get_chess_sprites' filename routing / conversion
    / caching from real BMP decoding (which the PIL mock pollution breaks).
    """
    loader = ResourceLoader("/unused")
    loader.get_image = lambda filename: images_by_filename.get(filename)
    return loader


def test_get_chess_sprites_loads_selected_sheet_as_1bit():
    """get_chess_sprites(name) loads chesssprites_<name>.bmp converted to mode '1'.

    Why: the board widget renders from a 1-bit sheet; selecting a name must load
    that specific file (not always the default).

    How the regression manifests: if the name were ignored, both calls would
    route to the same filename and return identical images.
    """
    loader = _loader_with_stubbed_images({
        "chesssprites_default.bmp": Image.new("L", (16, 16), 255),  # all white
        "chesssprites_fen.bmp": Image.new("L", (16, 16), 0),        # all black
    })

    default_img = loader.get_chess_sprites("default")
    fen_img = loader.get_chess_sprites("fen")

    assert default_img is not None and fen_img is not None
    assert default_img.mode == "1"
    assert fen_img.mode == "1"
    assert list(default_img.getdata()) != list(fen_img.getdata())


def test_get_chess_sprites_defaults_to_default_sheet():
    """get_chess_sprites() with no name routes to the 'default' sheet filename.

    Why: startup and callers without a stored selection rely on the default.
    """
    loader = _loader_with_stubbed_images({
        "chesssprites_default.bmp": Image.new("L", (16, 16), 255),
    })
    assert loader.get_chess_sprites() is not None


def test_get_chess_sprites_caches_per_name():
    """Repeated loads of the same name return the cached image; names don't collide.

    Why: rendering happens often; per-name caching avoids re-decoding. A per-name
    cache (not a single shared slot) is required so switching sheets does not
    return a stale cached image.
    """
    loader = _loader_with_stubbed_images({
        "chesssprites_retro.bmp": Image.new("L", (16, 16), 0),
        "chesssprites_default.bmp": Image.new("L", (16, 16), 255),
    })

    first = loader.get_chess_sprites("retro")
    second = loader.get_chess_sprites("retro")
    assert first is second  # same cached object
    assert loader.get_chess_sprites("default") is not first


def test_get_chess_sprites_missing_returns_none():
    """Unknown sheet id returns None rather than raising.

    Why: a stale stored selection (sheet later deleted) must degrade gracefully;
    callers fall back to the default.
    """
    loader = _loader_with_stubbed_images({
        "chesssprites_default.bmp": Image.new("L", (16, 16), 255),
    })
    assert loader.get_chess_sprites("does_not_exist") is None


# ---------------------------------------------------------------------------
# Per-piece preview: get_chess_piece_preview(name, piece)
# ---------------------------------------------------------------------------

def _sheet_with_dark_square_king():
    """Build a 1-bit sprite sheet distinguishing the two king rows.

    The 16px sheet has the light-square pieces in row 0 (y=0..15) and the
    dark-square pieces in row 1 (y=16..31). Here the black king column (x=192)
    is left white in row 0 and filled solid black in row 1, so a crop from the
    wrong row is detectable.
    """
    sheet = Image.new("1", (208, 48), 1)  # mode '1': 1 == white
    px = sheet.load()
    for yy in range(16, 32):              # dark-square row
        for xx in range(192, 208):        # black king column
            px[xx, yy] = 0  # black
    return sheet


def test_get_chess_piece_preview_uses_dark_square_row_as_full_tile():
    """Preview crops the black king from the dark-square row and shows the full tile.

    Why this test exists: the Board > Sprites radio list shows each sheet's black
    king on its dark square. The preview must crop the king column (x=192) from
    row 1 (the dark-square version, y=16) and present the whole 16x16 cell as an
    opaque tile (square + king), not mask down to the king shape.

    How the regression manifests: cropping row 0 returns the light-square cell
    (all white here) instead of the dark tile (all black); a non-opaque mask
    would punch holes in the tile.
    """
    loader = ResourceLoader("/unused")
    loader.get_chess_sprites = lambda name="default": _sheet_with_dark_square_king()

    image, mask = loader.get_chess_piece_preview("default", "k")

    assert image is not None
    assert image.size == (16, 16)
    # Dark-square king cell is solid black here -> tile is all black.
    assert set(image.getdata()) == {0}
    # Full tile is shown opaquely (no transparency punched into the square).
    assert mask is None


def test_get_chess_piece_preview_missing_sheet_returns_none_pair():
    """A missing sheet yields (None, None) so the menu falls back to a drawn icon.

    Why: a stale/removed sheet selection must not raise while building the menu.
    """
    loader = ResourceLoader("/unused")
    loader.get_chess_sprites = lambda name="default": None

    assert loader.get_chess_piece_preview("ghost", "k") == (None, None)


def test_get_chess_piece_preview_unknown_piece_returns_none_pair():
    """An unknown piece letter yields (None, None) rather than a bad crop.

    Why: guards the piece->column lookup so callers can request only valid pieces.
    """
    loader = ResourceLoader("/unused")
    loader.get_chess_sprites = lambda name="default": _sheet_with_dark_square_king()

    assert loader.get_chess_piece_preview("default", "Z") == (None, None)
