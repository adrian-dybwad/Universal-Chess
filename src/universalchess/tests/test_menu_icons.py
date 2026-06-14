"""Tests that every catalog icon has a real e-paper implementation.

Menu catalog nodes reference icons by shared semantic ids. On the board each id
must be drawn by IconButtonWidget._draw_icon; an id with no branch falls through
to a generic square placeholder, which would silently ship an unrecognizable
menu icon. These tests render each registered icon and assert it is not the
placeholder, and specifically cover the three ids added for the catalog
(play/update/undo).
"""

from PIL import Image, ImageDraw

from universalchess.epaper.icon_button import IconButtonWidget
from universalchess.menus.catalog import load_catalog


_ICON_SIZE = 36
_CANVAS = 48


def _render(icon_name: str):
    """Render an icon onto a white canvas and return its pixel tuple."""
    img = Image.new("L", (_CANVAS, _CANVAS), 255)
    draw = ImageDraw.Draw(img)
    btn = IconButtonWidget(
        0, 0, _CANVAS, _CANVAS, lambda *a, **k: None,
        key="k", label="", icon_name=icon_name,
    )
    btn._draw_icon(draw, icon_name, _CANVAS // 2, _CANVAS // 2, _ICON_SIZE, selected=False)
    return tuple(img.getdata())


# Rendering of an unrecognized id - the generic placeholder square.
_PLACEHOLDER = _render("__definitely_not_an_icon__")


def test_new_catalog_icons_render_distinctly():
    """play/update/undo must each draw something other than the placeholder.

    These three ids were added so the catalog (and menus referencing them) draw
    a real glyph. If a dispatch branch is missing, the render equals the
    placeholder square and this fails.
    """
    for name in ("play", "update", "undo"):
        assert _render(name) != _PLACEHOLDER, f"icon '{name}' renders as placeholder"


# 'timer' intentionally shares the plain-square glyph with the placeholder: the
# unchecked timer IS a bare square and 'timer_checked' adds the checkmark. It is
# a handled id, so exclude it from the "differs from placeholder" check while
# still rendering it to confirm it does not raise.
_SQUARE_BY_DESIGN = {"timer"}


def test_every_registered_icon_has_board_implementation():
    """Every icon id in the registry must be drawn (not the placeholder).

    Guards the shared-registry contract on the board side: an id present in
    icons.json but missing a _draw_icon branch would render blank on the board.
    Iterating the whole registry catches that for any future icon addition. A
    missing branch shows up as a render equal to the placeholder square.
    """
    catalog = load_catalog()
    for icon_id in sorted(catalog.icon_ids()):
        rendered = _render(icon_id)
        if icon_id in _SQUARE_BY_DESIGN:
            continue
        assert rendered != _PLACEHOLDER, f"icon '{icon_id}' has no board implementation"
