#!/usr/bin/env python3
"""Build a SPLIT chess sprite sheet (chesssprites_<id>.bmp) from 12 piece PNGs.

The board renderer selects the SPLIT drawing path purely from a sheet's
dimensions (see universalchess.epaper.chess_board.detect_sheet_layout). A SPLIT
sheet is a 1-bit BMP, 192x32 = 12 columns x 2 rows of 16px tiles, with no
empty-square column:

    columns 0..11 = pieces in FEN order  P R N B Q K p r n b q k
    row 0 (y=0..15)  = INK  : the glyph pixels to stamp (black = ink)
    row 1 (y=16..31) = MASK : the silhouette matte, dilated 1px for a halo
                              (black = the piece's occupied area)

The renderer draws the board squares in code (white light squares, 50% dither
dark squares), clears the square under the MASK to white, then stamps the INK.
This script converts the itch.io "chess-pieces-16x16-one-bit" pack (or any set
of twelve 16x16 transparent PNGs) into that BMP in one command; drop the result
into a resources directory as ``chesssprites_<id>.bmp`` and it appears as a new
sprite style automatically.

Each piece is read from ``<pieces-dir>/<stem>.png``. Colour-prefixed stems are
used (not FEN case) so the twelve files do not collide on case-insensitive
filesystems.

Usage:
    python scripts/make-split-sprite-sheet.py --pieces-dir ./pieces \
        --output src/universalchess/resources/chesssprites_onebit.bmp
"""

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageFilter

TILE = 16
# Pieces in FEN letter order; the index is the sprite-sheet column.
PIECE_ORDER = ("P", "R", "N", "B", "Q", "K", "p", "r", "n", "b", "q", "k")

# Piece letter -> input PNG filename stem. Descriptive names avoid the
# uppercase/lowercase collision FEN letters would cause on case-insensitive
# filesystems (macOS, Windows).
FILE_STEMS = {
    "P": "white-pawn", "R": "white-rook", "N": "white-knight",
    "B": "white-bishop", "Q": "white-queen", "K": "white-king",
    "p": "black-pawn", "r": "black-rook", "n": "black-knight",
    "b": "black-bishop", "q": "black-queen", "k": "black-king",
}


def _load_piece(path: Path) -> Image.Image:
    """Load a piece PNG as RGBA, resizing to 16x16 (nearest) if needed."""
    img = Image.open(path).convert("RGBA")
    if img.size != (TILE, TILE):
        print(f"warning: {path.name} is {img.size}, resizing to {TILE}x{TILE}",
              file=sys.stderr)
        img = img.resize((TILE, TILE), Image.NEAREST)
    return img


def _ink_tile(piece: Image.Image, alpha_thresh: int, lum_thresh: int) -> Image.Image:
    """INK tile: black where the piece has opaque, dark pixels; else white.

    White pieces keep only their dark outline as ink (a hollow glyph), so the
    matte-then-ink compositing reads a white body with a black outline. Black
    pieces keep their whole body as ink.
    """
    tile = Image.new("1", (TILE, TILE), 1)  # 1 == white
    px = piece.load()
    out = tile.load()
    for y in range(TILE):
        for x in range(TILE):
            r, g, b, a = px[x, y]
            luminance = (r * 299 + g * 587 + b * 114) // 1000
            if a >= alpha_thresh and luminance < lum_thresh:
                out[x, y] = 0  # black ink
    return tile


def _mask_tile(piece: Image.Image, alpha_thresh: int, dilate: bool) -> Image.Image:
    """MASK tile: black over the piece silhouette (opaque area), 1px dilated.

    Dilating grows the silhouette by one pixel so a black piece stamped on a
    dark dithered square keeps a thin white halo and stays legible.
    """
    # Build the silhouette in 'L' with white == occupied so MaxFilter dilates it.
    silhouette = Image.new("L", (TILE, TILE), 0)
    px = piece.load()
    sil = silhouette.load()
    for y in range(TILE):
        for x in range(TILE):
            if px[x, y][3] >= alpha_thresh:
                sil[x, y] = 255
    if dilate:
        silhouette = silhouette.filter(ImageFilter.MaxFilter(3))

    tile = Image.new("1", (TILE, TILE), 1)  # 1 == white (transparent)
    sil = silhouette.load()
    out = tile.load()
    for y in range(TILE):
        for x in range(TILE):
            if sil[x, y] >= 128:
                out[x, y] = 0  # black == occupied
    return tile


def build_sheet(pieces_dir: Path, alpha_thresh: int, lum_thresh: int,
                dilate: bool) -> Image.Image:
    """Compose the 192x32 1-bit BMP from the twelve piece PNGs."""
    sheet = Image.new("1", (len(PIECE_ORDER) * TILE, 2 * TILE), 1)
    missing = []
    for column, letter in enumerate(PIECE_ORDER):
        path = pieces_dir / f"{FILE_STEMS[letter]}.png"
        if not path.exists():
            missing.append(path.name)
            continue
        piece = _load_piece(path)
        x = column * TILE
        sheet.paste(_ink_tile(piece, alpha_thresh, lum_thresh), (x, 0))
        sheet.paste(_mask_tile(piece, alpha_thresh, dilate), (x, TILE))
    if missing:
        raise FileNotFoundError(
            "missing piece PNG(s) in "
            f"{pieces_dir}: {', '.join(missing)}"
        )
    return sheet


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pieces-dir", type=Path, required=True,
                        help="Directory of the twelve 16x16 transparent piece PNGs "
                             f"(named {', '.join(s + '.png' for s in FILE_STEMS.values())}).")
    parser.add_argument("--output", type=Path, required=True,
                        help="Output BMP path (e.g. resources/chesssprites_onebit.bmp).")
    parser.add_argument("--alpha-threshold", type=int, default=128,
                        help="Minimum alpha (0-255) counted as opaque. Default 128.")
    parser.add_argument("--luminance-threshold", type=int, default=128,
                        help="Pixels darker than this (0-255) become ink. Default 128.")
    parser.add_argument("--no-dilate", action="store_true",
                        help="Do not grow the mask 1px (drops the black-piece halo).")
    args = parser.parse_args(argv)

    sheet = build_sheet(args.pieces_dir, args.alpha_threshold,
                        args.luminance_threshold, not args.no_dilate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.output, format="BMP")
    print(f"wrote {args.output} ({sheet.width}x{sheet.height}, 1-bit SPLIT sheet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
