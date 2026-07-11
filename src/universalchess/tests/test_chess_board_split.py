"""Tests for the SPLIT sprite-sheet layout (board pattern drawn in code + pieces).

Why these tests exist
---------------------
The board can render from two sheet structures, chosen purely from the sheet's
pixel dimensions:

- LEGACY (208x32): 13 columns, pieces baked onto light/dark square tiles.
- SPLIT (192x32): 12 columns; row 0 = INK glyph, row 1 = MASK silhouette. The
  renderer draws squares in code (white light, 50% dither dark), clears the
  square under the mask to white, then stamps the ink on top.

These tests pin: (1) the dimension-based layout trigger, (2) that SPLIT draws
dithered dark / white light squares and composites pieces via matte+ink (the
dither is erased under the piece so it reads cleanly), (3) that flip and the
incremental-render filters still map to the same cells, (4) that the red overlay
takes its silhouette from the MASK row, (5) that the piece preview and figurine
move glyphs are layout-aware, and (6) that LEGACY sheets are classified and
rendered unchanged.
"""

import sys
import pytest
from unittest.mock import MagicMock
from concurrent.futures import Future

for _mod in ("spidev", "RPi", "RPi.GPIO", "gpiozero"):
    sys.modules.setdefault(_mod, MagicMock())

Image = ImageDraw = ImageChops = None  # bound by the autouse fixture


_PIL_MODULE_NAMES = (
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageChops",
    "PIL.ImageFont", "PIL.ImageFilter",
)


@pytest.fixture(autouse=True)
def _real_pil():
    """Bind real PIL into the modules under test, then fully restore on teardown.

    Other test modules replace PIL with a MagicMock in sys.modules and never
    restore it, which leaves already-imported modules bound to the mock. This
    fixture temporarily installs real PIL and rebinds it in the render modules so
    crop/paste/dither run for real here.

    Crucially it snapshots the prior sys.modules entries and the rebinding
    modules' attributes and restores them on teardown. Without that, popping PIL
    would leak a duplicate PIL module to later-collected test modules -- breaking
    ones that patch Image.open or expect a mocked PIL (e.g. the chromecast and
    clock suites), which is exactly the leakage this heals for itself.
    """
    import universalchess.epaper.chess_board as cb
    import universalchess.epaper.move_render as mr
    import universalchess.resources as rl

    saved_modules = {name: sys.modules.get(name) for name in _PIL_MODULE_NAMES}
    saved_attrs = [
        (cb, "Image", cb.Image), (cb, "ImageDraw", cb.ImageDraw),
        (cb, "ImageChops", cb.ImageChops), (cb, "ImageFilter", cb.ImageFilter),
        (mr, "Image", mr.Image), (rl, "Image", rl.Image),
    ]

    for name in _PIL_MODULE_NAMES:
        sys.modules.pop(name, None)
    import PIL.Image as real_image
    import PIL.ImageDraw as real_draw
    import PIL.ImageChops as real_chops
    import PIL.ImageFilter as real_filter

    cb.Image, cb.ImageDraw, cb.ImageChops, cb.ImageFilter = (
        real_image, real_draw, real_chops, real_filter,
    )
    mr.Image = real_image
    rl.Image = real_image

    global Image, ImageDraw, ImageChops
    Image, ImageDraw, ImageChops = real_image, real_draw, real_chops
    try:
        yield
    finally:
        for obj, attr, value in saved_attrs:
            setattr(obj, attr, value)
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


TILE = 16


# ---------------------------------------------------------------------------
# Synthetic sheets
# ---------------------------------------------------------------------------

def _split_sheet():
    """A 192x32 SPLIT sheet: every piece's INK = left 8px black, MASK = full tile.

    A full-tile mask lets tests assert the matte erases the square entirely under
    the piece (right half becomes pure white, not dither), and the left-half ink
    proves the glyph is stamped. Both rows are identical per column, which is all
    the compositing math needs to be exercised.
    """
    sheet = Image.new("1", (12 * TILE, 2 * TILE), 255)  # 255 == white
    px = sheet.load()
    for col in range(12):
        x0 = col * TILE
        for y in range(TILE):                 # row 0 INK: left half black
            for x in range(x0, x0 + TILE // 2):
                px[x, y] = 0
        for y in range(TILE, 2 * TILE):        # row 1 MASK: full tile black
            for x in range(x0, x0 + TILE):
                px[x, y] = 0
    return sheet


def _legacy_sheet():
    """A 208x32 all-white LEGACY sheet (13 cols x 2 rows)."""
    return Image.new("1", (13 * TILE, 2 * TILE), 255)


def _colorway_sheet():
    """A 96x32 RGBA COLORWAY sheet: 6 type columns (K Q B N R P), 2 colourways.

    Every column carries the same synthetic art (column order is exercised
    separately via piece_type_column): full-tile opaque alpha (silhouette), row 0
    = black colourway (RGB all black), row 1 = white colourway (left half black
    outline, right half white). This lets tests assert that a black piece fills
    black (matte erasing any dither) and a white piece reads white body + black
    outline, from the alpha mask + RGB ink.
    """
    sheet = Image.new("RGBA", (6 * TILE, 2 * TILE), (0, 0, 0, 0))
    px = sheet.load()
    for col in range(6):
        x0 = col * TILE
        for y in range(TILE):                      # row 0 = black colourway
            for x in range(x0, x0 + TILE):
                px[x, y] = (0, 0, 0, 255)
        for y in range(TILE, 2 * TILE):            # row 1 = white colourway
            for x in range(x0, x0 + TILE):
                v = 0 if (x - x0) < TILE // 2 else 255
                px[x, y] = (v, v, v, 255)
    return sheet


def _widget(fen, sheet, flip=False):
    from universalchess.state.chess_game import reset_chess_game
    from universalchess.epaper.chess_board import ChessBoardWidget
    w = ChessBoardWidget(0, 0, MagicMock(return_value=Future()),
                         reset_chess_game(), flip=flip, sprites=sheet)
    w.fen = fen
    return w


def _pixels(region):
    return list(region.getdata())


def _all_white(sprite, box):
    return set(_pixels(sprite.crop(box))) == {255}


def _has_black(sprite, box):
    return 0 in set(_pixels(sprite.crop(box)))


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
KNIGHT_A1_FEN = "8/8/8/8/8/8/8/N7 w - - 0 1"
TWO_KINGS_FEN = "k7/8/8/8/8/8/8/7K w - - 0 1"
BLACK_KING_B8_FEN = "1k6/8/8/8/8/8/8/8 w - - 0 1"
# Fool's-mate check: white king e1 checked by the black queen on h4.
CHECK_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"


# ---------------------------------------------------------------------------
# detect_sheet_layout
# ---------------------------------------------------------------------------

class TestDetectSheetLayout:
    """The drawing path is chosen from the sheet's dimensions alone."""

    def test_legacy_208x32(self):
        # 13 columns (empty-square + 12 pieces) is the historical baked sheet.
        # Regression: mis-detecting it as SPLIT would draw squares over the art.
        from universalchess.epaper.chess_board import detect_sheet_layout, SheetMode
        layout = detect_sheet_layout(208, 32)
        assert layout.mode is SheetMode.LEGACY
        assert (layout.columns, layout.rows, layout.tile) == (13, 2, TILE)
        assert layout.is_split is False

    def test_split_192x32(self):
        # Exactly 12 columns over 2 rows is the split ink/mask sheet.
        # Regression: mis-detecting it as LEGACY would crop a non-existent
        # empty-square column and shift every piece one column left.
        from universalchess.epaper.chess_board import detect_sheet_layout, SheetMode
        layout = detect_sheet_layout(192, 32)
        assert layout.mode is SheetMode.SPLIT
        assert (layout.columns, layout.rows) == (12, 2)
        assert layout.is_split is True

    def test_single_row_192x16_is_not_split(self):
        # A split sheet needs the mask row; one 16px row cannot be split.
        # Regression: treating it as split would crop the mask past the image.
        from universalchess.epaper.chess_board import detect_sheet_layout, SheetMode
        assert detect_sheet_layout(192, 16).mode is SheetMode.LEGACY

    def test_piece_column_x_differs_by_mode(self):
        # LEGACY reserves column 0 for the empty square (pawn at 16); SPLIT has
        # no empty column (pawn at 0). A wrong offset misaligns every piece.
        from universalchess.epaper.chess_board import detect_sheet_layout
        legacy = detect_sheet_layout(208, 32)
        split = detect_sheet_layout(192, 32)
        assert legacy.piece_column_x("P") == 16
        assert legacy.piece_column_x("k") == 192
        assert split.piece_column_x("P") == 0
        assert split.piece_column_x("k") == 176
        assert split.piece_column_x(" ") == -1


# ---------------------------------------------------------------------------
# Split render: squares
# ---------------------------------------------------------------------------

class TestSplitSquares:
    """Empty squares are drawn in code: white light, 50% dither dark."""

    def test_empty_dark_square_is_dithered(self):
        # b-file rank-6 square (e2 area idx17: rank2,file1) is empty and dark, so
        # the cell must contain a mix of black and white (the 50% dither).
        # Regression: a solid fill (no dither) would make dark squares flat.
        w = _widget(START_FEN, _split_sheet())
        sprite = Image.new("1", (128, 128), 255)
        w.render(sprite)
        cell = (16, 32, 32, 48)  # rank2,file1 -> dark, empty
        data = set(_pixels(sprite.crop(cell)))
        assert 0 in data and 255 in data, "empty dark square should be dithered"

    def test_empty_light_square_is_white(self):
        # rank2,file0 (idx16) is empty and light -> pure white interior.
        # (x=0 is the board border, so sample the interior.) Regression: leaking
        # dither onto light squares would show stray black pixels.
        w = _widget(START_FEN, _split_sheet())
        sprite = Image.new("1", (128, 128), 255)
        w.render(sprite)
        assert _all_white(sprite, (1, 33, 16, 48))


class TestSplitCompositing:
    """Pieces are composited via matte (erase square) + ink (stamp glyph)."""

    def test_piece_on_dark_square_erases_dither_and_inks(self):
        # b8 knight (idx1) is on a dark square. The full-tile mask must matte the
        # whole cell white (erasing the dither) and the left-half ink must stamp
        # black. So: left half black, right half pure white.
        # Regression: skipping the matte would leave dither showing through the
        # piece; skipping the ink would drop the glyph entirely.
        w = _widget(START_FEN, _split_sheet())
        sprite = Image.new("1", (128, 128), 255)
        w.render(sprite)
        # y=0 is the border; sample rows 1..15.
        assert set(_pixels(sprite.crop((16, 1, 24, 16)))) == {0}, "ink left half"
        assert set(_pixels(sprite.crop((24, 1, 32, 16)))) == {255}, "matte cleared dither"

    def test_piece_on_light_square_inks_over_white(self):
        # a8 rook (idx0) on a light square: left-half ink black, right-half white.
        w = _widget(START_FEN, _split_sheet())
        sprite = Image.new("1", (128, 128), 255)
        w.render(sprite)
        assert _has_black(sprite, (1, 1, 8, 16)), "ink present"
        assert set(_pixels(sprite.crop((8, 1, 16, 16)))) == {255}, "no ink right half"


class TestSplitFlipAndFilters:
    """flip and incremental-render filters map to the same cells in SPLIT mode."""

    def test_flip_maps_a1_knight_to_opposite_corner(self):
        # a1 knight renders at (0,112) unflipped and (112,0) flipped. "Piece
        # present" is detected by a fully-white right half (matte erased dither);
        # an empty dark cell keeps its dither. Regression: a flip mapping bug
        # would place the piece on the wrong corner.
        unflipped = Image.new("1", (128, 128), 255)
        _widget(KNIGHT_A1_FEN, _split_sheet()).render(unflipped)
        assert set(_pixels(unflipped.crop((8, 113, 16, 127)))) == {255}, "piece at (0,112)"
        assert _has_black(unflipped, (120, 1, 127, 16)), "opposite corner empty->dither"

        flipped = Image.new("1", (128, 128), 255)
        _widget(KNIGHT_A1_FEN, _split_sheet(), flip=True).render(flipped)
        assert set(_pixels(flipped.crop((120, 1, 128, 15)))) == {255}, "piece at (112,0)"
        assert _has_black(flipped, (1, 113, 8, 127)), "opposite corner empty->dither"

    def test_render_only_file_limits_to_that_file(self):
        # With render_only_file=0 only the a-file squares draw. a8 rook (0,0) and
        # a2 pawn (0,96) render; b8 (16,0) is skipped and stays white.
        # Regression: ignoring the filter would render the whole board.
        w = _widget(START_FEN, _split_sheet())
        w.set_render_only_file(0)
        sprite = Image.new("1", (128, 128), 255)
        w.render(sprite)
        assert _has_black(sprite, (1, 1, 8, 16)), "a8 rendered"
        assert _has_black(sprite, (1, 97, 8, 112)), "a2 rendered"
        assert _all_white(sprite, (17, 1, 31, 15)), "b8 not rendered"


# ---------------------------------------------------------------------------
# Red overlay
# ---------------------------------------------------------------------------

class TestSplitRenderRed:
    """The red overlay reddens the piece silhouette from the MASK row in SPLIT."""

    def test_red_uses_mask_row_silhouette(self):
        # In check, the king cell reddens. The full-tile mask means the king cell
        # interior (not just the outline) goes red. With a LEGACY all-white sheet
        # the interior would stay clear, so red interior pixels prove the mask row
        # (row 1) is the silhouette source in SPLIT mode.
        w = _widget(CHECK_FEN, _split_sheet())
        sprite = Image.new("1", (128, 128), 255)
        w.render_red(sprite)
        # King e1 -> cell (64,112); sample its interior, avoiding the outline.
        interior = sprite.crop((65, 113, 79, 127))
        assert 0 in set(_pixels(interior)), "king silhouette reddened via mask row"


# ---------------------------------------------------------------------------
# Preview + figurine glyphs
# ---------------------------------------------------------------------------

class TestSplitPreview:
    """get_chess_piece_preview composes a real tile for SPLIT sheets."""

    def test_split_preview_composes_tile(self):
        from universalchess.resources import ResourceLoader
        loader = ResourceLoader("/unused")
        loader.get_chess_sprites = lambda name="default": _split_sheet()

        image, mask = loader.get_chess_piece_preview("onebit", "k", on_dark_square=True)
        assert image is not None and mask is None
        assert image.size == (TILE, TILE)
        assert image.mode == "1"
        # Composited tile carries ink (black) pixels; the matte keeps some white.
        data = set(image.getdata())
        assert 0 in data and 255 in data

    def test_legacy_preview_unchanged(self):
        # A 208x48 sheet with the black king baked solid on its dark-square row
        # must still crop that opaque tile (regression guard for the legacy path).
        from universalchess.resources import ResourceLoader
        sheet = Image.new("1", (208, 48), 255)
        px = sheet.load()
        for y in range(16, 32):
            for x in range(192, 208):
                px[x, y] = 0
        loader = ResourceLoader("/unused")
        loader.get_chess_sprites = lambda name="default": sheet
        image, mask = loader.get_chess_piece_preview("default", "k")
        assert image is not None and mask is None
        assert set(image.getdata()) == {0}


class TestSplitFigurineGlyph:
    """move_render figurine glyphs use the ink row and layout-aware columns."""

    def test_split_glyph_reads_ink_column(self):
        from universalchess.epaper import move_render
        # SPLIT knight column is index 2 -> x=32; put black there in row 0 ink.
        sheet = Image.new("1", (192, 32), 255)
        ImageDraw.Draw(sheet).rectangle([32, 0, 47, 15], fill=0)
        glyph = move_render._piece_glyph_image(sheet, "N", TILE)
        assert glyph is not None
        assert 0 in set(glyph.getdata()), "ink present at split N column"

    def test_legacy_glyph_reads_offset_column(self):
        from universalchess.epaper import move_render
        # LEGACY knight column is x=48 (empty-square column shifts it by one).
        sheet = Image.new("1", (208, 32), 255)
        ImageDraw.Draw(sheet).rectangle([48, 0, 63, 15], fill=0)
        glyph = move_render._piece_glyph_image(sheet, "N", TILE)
        assert glyph is not None
        assert 0 in set(glyph.getdata()), "ink present at legacy N column"


# ---------------------------------------------------------------------------
# Legacy backward-compat
# ---------------------------------------------------------------------------

class TestLegacyClassification:
    """A 208x32 sheet takes the LEGACY path unchanged."""

    def test_widget_classifies_legacy(self):
        from universalchess.epaper.chess_board import SheetMode
        w = _widget(START_FEN, _legacy_sheet())
        assert w._layout is not None
        assert w._layout.mode is SheetMode.LEGACY

    def test_legacy_render_draws_border_only_for_blank_sheet(self):
        # An all-white legacy sheet renders only the board outline (no per-square
        # dither), matching prior behavior. Regression: accidentally taking the
        # split path would draw dither on dark squares.
        w = _widget(START_FEN, _legacy_sheet())
        sprite = Image.new("1", (128, 128), 255)
        w.render(sprite)
        # Interior of a dark square stays white (no code-drawn dither in legacy).
        assert _all_white(sprite, (17, 1, 31, 15))


# ---------------------------------------------------------------------------
# COLORWAY layout (RGBA PNG: alpha = mask, RGB = ink, rows = colourways)
# ---------------------------------------------------------------------------

class TestColorwayLayout:
    """RGBA 6-column sheets are detected as COLORWAY; type-column order is pinned."""

    def test_detect_requires_alpha_and_six_columns(self):
        # 96x32 WITH alpha is COLORWAY; without alpha it cannot supply a mask, so
        # it falls back to LEGACY. Regression: detecting COLORWAY without alpha
        # would later crash trying to read a non-existent alpha channel.
        from universalchess.epaper.chess_board import detect_sheet_layout, SheetMode
        assert detect_sheet_layout(96, 32, has_alpha=True).mode is SheetMode.COLORWAY
        assert detect_sheet_layout(96, 32, has_alpha=False).mode is SheetMode.LEGACY

    def test_colorway_does_not_shadow_split(self):
        # A 12-column sheet stays SPLIT even if it has alpha (only 6 columns is
        # COLORWAY). Regression: over-broad alpha detection would hijack SPLIT.
        from universalchess.epaper.chess_board import detect_sheet_layout, SheetMode
        assert detect_sheet_layout(192, 32, has_alpha=True).mode is SheetMode.SPLIT

    def test_piece_type_column_order_is_kqbnrp(self):
        # The itch.io pack ships columns in K Q B N R P order, colour-agnostic.
        # A wrong mapping would draw the wrong piece for every square.
        from universalchess.epaper.chess_board import detect_sheet_layout
        layout = detect_sheet_layout(96, 32, has_alpha=True)
        assert layout.piece_type_column("K") == 0
        assert layout.piece_type_column("k") == 0     # colour-agnostic
        assert layout.piece_type_column("Q") == 16
        assert layout.piece_type_column("B") == 32
        assert layout.piece_type_column("N") == 48
        assert layout.piece_type_column("R") == 64
        assert layout.piece_type_column("P") == 80
        assert layout.piece_type_column(" ") == -1


class TestColorwayCompositing:
    """COLORWAY pieces composite from alpha (mask) + RGB (ink), colourway by row."""

    def test_black_and_white_kings_use_their_colourway_rows(self):
        # Black king a8 (light cell 0,0): black colourway is fully dark+opaque ->
        # a solid black tile. White king h1 (light cell 112,112): white colourway
        # is left-half black outline, right-half white -> split tile.
        # Regression: reading the wrong row would swap the two kings' looks.
        w = _widget(TWO_KINGS_FEN, _colorway_sheet())
        sprite = Image.new("1", (128, 128), 255)
        w.render(sprite)
        assert set(_pixels(sprite.crop((1, 1, 16, 16)))) == {0}, "black king solid"
        assert set(_pixels(sprite.crop((113, 113, 120, 127)))) == {0}, "white king outline"
        assert set(_pixels(sprite.crop((120, 113, 127, 127)))) == {255}, "white king body"

    def test_matte_erases_dither_under_black_piece_on_dark_square(self):
        # Black king on the dark b8 cell (16,0): the alpha matte must clear the
        # dither so the solid-black colourway reads as a clean black tile, not
        # black-over-dither. Regression: skipping the matte leaves dither showing.
        w = _widget(BLACK_KING_B8_FEN, _colorway_sheet())
        sprite = Image.new("1", (128, 128), 255)
        w.render(sprite)
        assert set(_pixels(sprite.crop((16, 1, 32, 16)))) == {0}


class TestColorwayRenderRed:
    """The red overlay derives the COLORWAY silhouette from the alpha channel."""

    def test_red_uses_alpha_silhouette(self):
        # In check, the white king cell reddens. The full-opaque alpha means the
        # king cell interior goes red (not just the outline), proving the alpha
        # channel is the silhouette source in COLORWAY mode.
        w = _widget(CHECK_FEN, _colorway_sheet())
        sprite = Image.new("1", (128, 128), 255)
        w.render_red(sprite)
        assert 0 in set(_pixels(sprite.crop((65, 113, 79, 127)))), "king reddened via alpha"


class TestColorwayPreview:
    """get_chess_piece_preview composes a real tile for COLORWAY sheets."""

    def test_colorway_preview_composes_white_king_tile(self):
        from universalchess.resources import ResourceLoader
        loader = ResourceLoader("/unused")
        loader.get_chess_sprites = lambda name="default": _colorway_sheet()

        image, mask = loader.get_chess_piece_preview("onebit", "K", on_dark_square=True)
        assert image is not None and mask is None
        assert image.size == (TILE, TILE)
        assert image.mode == "1"
        # White king = black outline + white body -> both black and white present.
        data = set(image.getdata())
        assert 0 in data and 255 in data


class TestColorwayFigurine:
    """move_render figurine glyphs compose from the COLORWAY sheet."""

    def test_white_piece_glyph_is_hollow_black_piece_is_filled(self):
        from universalchess.epaper import move_render
        sheet = _colorway_sheet()
        # White king: outline (black) + body (white) -> both values present.
        white = move_render._piece_glyph_image(sheet, "K", TILE)
        assert white is not None
        wdata = set(white.getdata())
        assert 0 in wdata and 255 in wdata, "white piece figurine is hollow"
        # Black king: solid black colourway -> ink fills the tile.
        black = move_render._piece_glyph_image(sheet, "k", TILE)
        assert black is not None
        assert set(black.getdata()) == {0}, "black piece figurine is filled"
