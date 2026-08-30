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


def test_check_icon_is_not_the_placeholder_or_the_x():
    """Correspondence confirm uses a tick, not a boxed checkbox or the X.

    Why: the Confirm Move pair is check beside cancel. A missing ``check``
    branch would draw the placeholder square, or reuse cancel and both
    buttons would look the same. How a regression manifests: check equals
    the placeholder or the cancel X.
    """
    check = _render("check")
    assert check != _PLACEHOLDER, "'check' must draw a tick, not the placeholder"
    assert check != _render("cancel"), "check and cancel must be distinct glyphs"


# Every registered id now draws a distinct glyph (no id shares the placeholder
# square by design): 'timer'/'timer_checked' draw a real stopwatch.
_SQUARE_BY_DESIGN: set[str] = set()


def test_timer_icons_are_real_stopwatch_glyphs():
    """timer and timer_checked must be real, distinct glyphs -- not checkboxes.

    Why this test exists: the time-control menus mark rows with 'timer' /
    'timer_checked'. These used to render as a bare square and a checked square
    (indistinguishable from a checkbox), which read as generic checkboxes rather
    than a clock. Both must now draw a real stopwatch, and the checked variant
    must differ from the unchecked one so the selected row is visible. How a
    regression manifests: if 'timer' reverts to the placeholder square it equals
    _PLACEHOLDER again; if the check is dropped the two variants render
    identically.
    """
    timer = _render("timer")
    timer_checked = _render("timer_checked")
    assert timer != _PLACEHOLDER, "'timer' must draw a real stopwatch, not the placeholder square"
    assert timer_checked != _PLACEHOLDER, "'timer_checked' must draw a real glyph"
    assert timer != timer_checked, "checked/unchecked stopwatch must be visually distinct"


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
