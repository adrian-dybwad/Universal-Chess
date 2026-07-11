#!/usr/bin/env python3
"""Build a COLORWAY chess sprite sheet (chesssprites_<id>.png) from a vector set.

The board renderer selects the COLORWAY drawing path purely from a sheet's
dimensions and alpha channel (see
universalchess.epaper.chess_board.detect_sheet_layout). A COLORWAY sheet is an
RGBA PNG, 96x32 = 6 columns x 2 rows of 16px tiles:

    columns 0..5 = piece type in the order  K Q B N R P
    row 0 (y=0..15)  = the BLACK colourway
    row 1 (y=16..31) = the WHITE colourway

Within each tile the alpha channel is the silhouette (mask) and the opaque RGB
is the ink: the renderer mattes the square to white under the mask, then stamps
black where the art is opaque and dark. White pieces therefore read as a white
body with a black outline; black pieces as a solid black body.

This script rasterises a source SVG that packs the twelve pieces in a grid, then
downsamples each cell to a 16px tile. It targets the classic Cburnett/Wikimedia
sprite (270x90: 6 columns K Q B N R P, 45px cells, white on row 0 / black on
row 1), which is the default layout below, but the grid geometry is
configurable for other packed vector sets.

Requires cairosvg for rasterisation (``pip install cairosvg``).

The source SVG is not shipped in the repository (only the generated sheet is);
download the Cburnett/Wikimedia set from
https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces to regenerate.

Usage:
    python scripts/make-svg-sprite-sheet.py \
        --svg Chess_Pieces_Sprite.svg \
        --output src/universalchess/resources/chesssprites_cburnett.png
"""

import argparse
import io
import sys
from pathlib import Path

import cairosvg
from PIL import Image, ImageChops, ImageFilter

TILE = 16

# Column order the COLORWAY layout expects (King, Queen, Bishop, Knight, Rook,
# Pawn). The Cburnett SVG is authored in exactly this order.
COLUMN_ORDER = ("K", "Q", "B", "N", "R", "P")


def darken_silhouette_edge(tile: Image.Image) -> Image.Image:
    """Force a solid 1px dark ring just inside the piece silhouette edge.

    Downsampling the detailed vector strokes to 16px thins and breaks the
    outline, so a white piece (light body, thin outline) reads as a nearly
    invisible white-on-white blob on a light square. Stamping the silhouette
    boundary black in the RGB channel guarantees a closed dark outline the
    renderer will ink, without changing the silhouette (alpha) or the body.

    For a black piece the boundary is already dark, so this is a no-op there.
    Operates in place on an RGBA tile and returns it.
    """
    alpha = tile.getchannel("A")
    mask = alpha.point(lambda a: 255 if a >= 128 else 0)  # 'L' silhouette
    # The 1px ring inside the edge = mask minus its erosion. MinFilter erodes
    # the white (opaque) region; the difference is the boundary pixels.
    edge = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(3)))
    tile.paste((0, 0, 0, 255), (0, 0), edge)  # black ink where the ring is set
    return tile


def rasterise(svg_path: Path, sheet_width: int, sheet_height: int) -> Image.Image:
    """Render the whole SVG to a supersampled RGBA image."""
    png_bytes = cairosvg.svg2png(
        url=str(svg_path),
        output_width=sheet_width,
        output_height=sheet_height,
    )
    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def build_sheet(svg_path: Path, columns: int, cell: int, scale: int,
                white_row: int, black_row: int, outline: bool) -> Image.Image:
    """Compose the 96x32 RGBA COLORWAY sheet from the packed SVG grid.

    The source cells are rendered at ``cell * scale`` px and downsampled with
    LANCZOS so the vector art antialiases into the 16px tile before the renderer
    thresholds it. The white and black source rows are placed into the COLORWAY
    rows the renderer reads (black in row 0, white in row 1). When ``outline``
    is set each tile gets a crisp 1px dark silhouette edge so white pieces stay
    legible on light squares (see ``darken_silhouette_edge``).
    """
    if columns != len(COLUMN_ORDER):
        raise ValueError(
            f"expected {len(COLUMN_ORDER)} columns for order "
            f"{''.join(COLUMN_ORDER)}, got {columns}"
        )
    big = rasterise(svg_path, columns * cell * scale, 2 * cell * scale)
    src_cell = cell * scale

    sheet = Image.new("RGBA", (columns * TILE, 2 * TILE), (0, 0, 0, 0))
    for column in range(columns):
        cx = column * src_cell
        white = big.crop((cx, white_row * src_cell,
                          cx + src_cell, (white_row + 1) * src_cell))
        black = big.crop((cx, black_row * src_cell,
                          cx + src_cell, (black_row + 1) * src_cell))
        white = white.resize((TILE, TILE), Image.LANCZOS)
        black = black.resize((TILE, TILE), Image.LANCZOS)
        if outline:
            darken_silhouette_edge(white)
            darken_silhouette_edge(black)
        sheet.paste(black, (column * TILE, 0))          # row 0 = black colourway
        sheet.paste(white, (column * TILE, TILE))       # row 1 = white colourway
    return sheet


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--svg", type=Path, required=True,
                        help="Input SVG packing the twelve pieces in a grid.")
    parser.add_argument("--output", type=Path, required=True,
                        help="Output PNG path (e.g. resources/chesssprites_cburnett.png).")
    parser.add_argument("--columns", type=int, default=len(COLUMN_ORDER),
                        help=f"Columns in the source grid. Default {len(COLUMN_ORDER)} "
                             f"({''.join(COLUMN_ORDER)}).")
    parser.add_argument("--cell", type=int, default=45,
                        help="Source cell size in SVG user units. Default 45.")
    parser.add_argument("--scale", type=int, default=8,
                        help="Supersample factor before downsampling to 16px. Default 8.")
    parser.add_argument("--white-row", type=int, default=0,
                        help="Grid row holding the white pieces. Default 0.")
    parser.add_argument("--black-row", type=int, default=1,
                        help="Grid row holding the black pieces. Default 1.")
    parser.add_argument("--no-outline", action="store_true",
                        help="Skip the crisp 1px dark silhouette outline "
                             "(white pieces get muddy on light squares).")
    args = parser.parse_args(argv)

    if not args.svg.exists():
        print(f"error: SVG not found: {args.svg}", file=sys.stderr)
        return 1

    sheet = build_sheet(args.svg, args.columns, args.cell, args.scale,
                        args.white_row, args.black_row, not args.no_outline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.output, format="PNG")
    print(f"wrote {args.output} ({sheet.width}x{sheet.height}, RGBA COLORWAY sheet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
