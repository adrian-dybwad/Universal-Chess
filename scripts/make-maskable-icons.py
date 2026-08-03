#!/usr/bin/env python3
"""Derive Android adaptive-icon ("maskable") PWA icons from the full-bleed logo.

Android may crop a maskable icon to any shape the launcher chooses, and only the
central circle of 80% of the icon's width is guaranteed to survive. The regular
icon (``icon-512.png``) is drawn edge to edge -- the horse's ears reach y=15 and
its chin y=495 on a 512px canvas -- so tagging it ``purpose: "maskable"`` costs
the ears and chin on a circular launcher.

This script produces a separate maskable variant per size instead of shrinking
the regular icon: the artwork is isolated from its background, scaled until its
furthest pixel sits inside the safe circle, and re-centred on a canvas filled
with the original background colour so the icon stays full-bleed under any mask.

The scale is derived from the true furthest content pixel, not the bounding
box's corner, because the logo is a rounded silhouette that never reaches its
own bounding-box corners; using the corner would shrink the artwork by a further
quarter for no gain in safety.

Usage:
    python scripts/make-maskable-icons.py
    python scripts/make-maskable-icons.py --source path/to/icon-512.png \
        --output-dir path/to/icons --sizes 192 512
"""

import argparse
import math
import sys
from pathlib import Path

from PIL import Image

# Fraction of the canvas guaranteed visible under any adaptive-icon mask. The
# worst-case mask is the circle inscribed in this central square, so content
# must stay within a radius of half this fraction from the centre.
SAFE_ZONE_FRACTION = 0.8

# Shrink a further 1% so that rounding the scaled dimensions to whole pixels,
# and the antialiased edge the resample introduces, cannot push a stray pixel
# past the safe radius that manifest.test.ts measures.
ROUNDING_MARGIN = 0.99

# Per-channel sum-of-absolute-difference above which a pixel is artwork rather
# than background. Matches the tolerance manifest.test.ts measures with, so the
# geometry this script guarantees is the geometry the test verifies.
BACKGROUND_TOLERANCE = 30

DEFAULT_SIZES = (192, 512)
DEFAULT_SOURCE = Path("src/universalchess/web-app/public/icons/icon-512.png")


def _content_geometry(image: Image.Image) -> tuple[tuple[int, int, int, int], float]:
    """Locate the artwork within a full-bleed icon.

    Returns the artwork's bounding box and the distance from the box's centre to
    its furthest artwork pixel. The pixel at (0, 0) is the background reference:
    a full-bleed icon's corner is background by construction. Fully transparent
    pixels count as background whatever their colour.

    Raises ValueError when the image is entirely background, because a scale
    factor cannot be derived from artwork that does not exist -- silently
    emitting a blank icon would ship an invisible home-screen entry.
    """
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    bg_r, bg_g, bg_b, _ = pixels[0, 0]

    min_x, min_y, max_x, max_y = width, height, -1, -1
    content: list[tuple[int, int]] = []
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a == 0:
                continue
            if abs(r - bg_r) + abs(g - bg_g) + abs(b - bg_b) <= BACKGROUND_TOLERANCE:
                continue
            content.append((x, y))
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)

    if not content:
        raise ValueError("source icon contains no artwork distinguishable from its background")

    centre_x = (min_x + max_x) / 2
    centre_y = (min_y + max_y) / 2
    radius = max(math.hypot(x - centre_x, y - centre_y) for x, y in content)
    return (min_x, min_y, max_x + 1, max_y + 1), radius


def make_maskable(source: Image.Image, size: int) -> Image.Image:
    """Render `source`'s artwork centred inside the maskable safe zone at `size`."""
    box, radius = _content_geometry(source)
    background = source.convert("RGBA").getpixel((0, 0))

    scale = (SAFE_ZONE_FRACTION / 2 * size / radius) * ROUNDING_MARGIN
    artwork = source.convert("RGBA").crop(box)
    scaled = artwork.resize(
        (max(1, round(artwork.width * scale)), max(1, round(artwork.height * scale))),
        Image.LANCZOS,
    )

    canvas = Image.new("RGBA", (size, size), background)
    canvas.alpha_composite(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help=f"full-bleed source icon (default: {DEFAULT_SOURCE})")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="output directory (default: the source's directory)")
    parser.add_argument("--sizes", type=int, nargs="+", default=list(DEFAULT_SIZES),
                        help=f"square output sizes (default: {' '.join(map(str, DEFAULT_SIZES))})")
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"error: source icon not found: {args.source}", file=sys.stderr)
        return 1

    output_dir = args.output_dir or args.source.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(args.source) as source:
        _, radius = _content_geometry(source)
        print(f"{args.source.name}: artwork reaches {radius:.1f}px from its centre")
        for size in args.sizes:
            icon = make_maskable(source, size)
            destination = output_dir / f"icon-{size}-maskable.png"
            icon.save(destination, "PNG", optimize=True)
            print(f"wrote {destination} ({size}x{size})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
