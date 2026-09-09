"""Tests for IconButtonWidget sprite-preview icons, radio markers, and frames.

Background / why these tests exist
----------------------------------
The Board > Sprites radio list needs two new IconButtonWidget capabilities:

1. A radio indicator icon (radio_checked / radio_empty) used as the trailing
   marker showing which sheet is active.
2. The ability to render an arbitrary preview image (a sheet's black king) as
   the button's main icon, composited through a transparency mask so it sits on
   the menu background instead of painting a white box.

These run on non-RPi hosts, so IconButtonWidget is imported without executing
the epaper package __init__ (which would touch hardware), mirroring
test_star_icon.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

Image = None  # bound to the real PIL.Image by the autouse fixture below


@pytest.fixture(autouse=True)
def _real_pil_and_fresh_epaper():
    """Restore real PIL and force epaper modules to rebind it.

    Other test modules replace PIL with a MagicMock in sys.modules; any epaper
    module imported under that mock keeps mock PIL bound. Restore a real PIL,
    rebind the test-global Image, and drop cached epaper modules so they re-bind
    real PIL when re-imported below (otherwise widget.render works on mocks and
    composites nothing).

    Every sys.modules entry this file removes or replaces -- including the stub
    packages ``_import_icon_button_widget`` installs -- is put back afterwards.
    Leaving them out corrupts later tests in a way that is hard to trace: a
    ``universalchess.epaper`` package popped here is re-imported by whoever needs
    it next, but the ``universalchess`` package object still holds the *original*
    epaper module as its ``epaper`` attribute, and that one still holds the
    original submodules. From then on ``import universalchess.epaper.x as m``
    (which walks parent attributes) and ``from universalchess.epaper.x import y``
    (which reads sys.modules) see two different modules, so patching one has no
    effect on the other.
    """
    global Image
    pil_names = ("PIL.ImageFont", "PIL.ImageDraw", "PIL.Image", "PIL")
    epaper_names = [m for m in sys.modules if m.startswith("universalchess.epaper")]
    saved = {name: sys.modules[name] for name in (*pil_names, *epaper_names) if name in sys.modules}

    for name in pil_names:
        sys.modules.pop(name, None)
    import PIL.Image as real_image
    import PIL.ImageDraw  # noqa: F401  (ensure real submodule is registered)
    import PIL.ImageFont  # noqa: F401
    Image = real_image
    for name in epaper_names:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name in [m for m in sys.modules if m.startswith("universalchess.epaper")]:
            del sys.modules[name]
        for name in (*pil_names, *epaper_names):
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def _import_icon_button_widget():
    """Import IconButtonWidget without importing the epaper package __init__."""
    epaper_pkg = types.ModuleType("universalchess.epaper")
    epaper_dir = Path(__file__).resolve().parents[1] / "epaper"
    epaper_pkg.__path__ = [str(epaper_dir)]
    sys.modules["universalchess.epaper"] = epaper_pkg

    framework_pkg = types.ModuleType("universalchess.epaper.framework")
    framework_dir = epaper_dir / "framework"
    framework_pkg.__path__ = [str(framework_dir)]
    sys.modules["universalchess.epaper.framework"] = framework_pkg

    from universalchess.epaper.icon_button import IconButtonWidget  # type: ignore

    return IconButtonWidget


def test_radio_checked_draws_outer_and_inner_circle():
    """radio_checked draws the ring plus a filled centre dot.

    Why: the filled radio marks the active sheet. A checked radio must render two
    ellipses (outer ring + inner dot); an empty one renders only the ring. If the
    fill were dropped the active sheet would be indistinguishable from the rest.
    """
    IconButtonWidget = _import_icon_button_widget()
    widget = IconButtonWidget(
        0, 0, 160, 60,
        update_callback=lambda *a, **k: None,
        key="sprite:default", label="default", icon_name="positions",
    )

    checked = MagicMock()
    widget._draw_radio_icon(checked, x=20, y=20, size=36, line_color=0, checked=True)
    assert checked.ellipse.call_count >= 2

    empty = MagicMock()
    widget._draw_radio_icon(empty, x=20, y=20, size=36, line_color=0, checked=False)
    assert empty.ellipse.call_count == 1


def test_render_with_icon_image_composites_preview_onto_button():
    """A button given icon_image+mask paints the preview (not a white box).

    Why: the sprite rows render the black king as their icon. The mask must let
    only the king's black pixels through; rendering must place black pixels in
    the left icon region. Without the image path (or mask), the icon area would
    stay blank/white and this assertion fails.
    """
    IconButtonWidget = _import_icon_button_widget()

    # 16x16 preview: a solid black blob with a matching opaque mask.
    preview = Image.new("1", (16, 16), 1)  # white
    mask = Image.new("1", (16, 16), 0)     # transparent
    pp = preview.load()
    mp = mask.load()
    for y in range(4, 12):
        for x in range(4, 12):
            pp[x, y] = 0    # black king pixels
            mp[x, y] = 255  # opaque there

    widget = IconButtonWidget(
        0, 0, 160, 60,
        update_callback=lambda *a, **k: None,
        key="sprite:default", label="default", icon_name="positions",
        icon_size=36, icon_image=preview, icon_mask=mask,
        trailing_icon_name="radio_checked",
    )

    sprite = Image.new("1", (160, 60), 1)  # white canvas
    widget.render(sprite)

    # The left icon region must contain black pixels from the composited king.
    icon_region = sprite.crop((0, 0, 60, 60))
    assert 0 in set(icon_region.getdata()), "preview king was not composited into the icon area"


def test_empty_vertical_label_skips_text_and_centers_the_icon():
    """A blank label must not reserve a text slot under the icon.

    Why this test exists: the main-menu Positions/Settings pair is icon-only.
    Vertical layout still reserved font_size+2 plus a 4px gap for an empty
    string, so the glyph sat in the top half of the half-width cell.

    How a regression manifests: _get_cached_text_widget is called (text path
    still runs) or icon_y is start_y + icon_size/2 instead of the content
    midpoint (the 4px text gap still lifts the glyph).
    """
    IconButtonWidget = _import_icon_button_widget()
    width, height = 64, 56
    icon_size = 32
    widget = IconButtonWidget(
        0, 0, width, height,
        update_callback=lambda *a, **k: None,
        key="Positions", label="", icon_name="positions",
        icon_size=icon_size, layout="vertical", font_size=13,
    )
    sprite = Image.new("1", (width, height), 1)
    content_top = widget.margin + widget.border_width + widget.padding
    content_height = (
        height - 2 * (widget.margin + widget.border_width + widget.padding)
    )
    with patch.object(widget, "_get_cached_text_widget") as mock_text, patch.object(
        widget, "_draw_main_icon"
    ) as mock_icon:
        widget.render(sprite)
    mock_text.assert_not_called()
    mock_icon.assert_called()
    icon_y = mock_icon.call_args[0][2]
    assert icon_y == content_top + content_height // 2


# PIL mode "1" stores bits; Image.new(..., 1) and load() yield 0/1, while
# ImageDraw/getpixel often yield 0/255. Treat either non-zero as white.
_WHITE = {1, 255}


def _frame_corners(sprite, margin):
    """Pixels at the four corners of the margin-inset rectangle.

    A stroked border paints all four black. Content (logo, label) is centered
    and does not reach those corners on a PLAY-sized cell.
    """
    right = sprite.width - 1 - margin
    bottom = sprite.height - 1 - margin
    return (
        sprite.getpixel((margin, margin)),
        sprite.getpixel((right, margin)),
        sprite.getpixel((margin, bottom)),
        sprite.getpixel((right, bottom)),
    )


def _play_sized_button(IconButtonWidget, *, border_width, selected=False):
    """The root-menu PLAY cell: 128x140, 80px logo, 32px label, 4px margin.

    140 is the height IconMenuWidget assigns when PLAY (ratio 2.4) shares the
    280px menu with Lichess, Centaur, and the Positions/Settings pair at 0.8.
    """
    return IconButtonWidget(
        0, 0, 128, 140,
        update_callback=lambda *a, **k: None,
        key="Universal",
        label="PLAY",
        icon_name="universal_logo",
        selected=selected,
        icon_size=80,
        layout="vertical",
        font_size=32,
        bold=True,
        border_width=border_width,
    )


def test_zero_border_width_does_not_stroke_the_frame():
    """border_width 0 must not draw the rectangle other rows use as a button.

    Why this test exists: the renderer used to stroke anyway (selected always
    outlined). A catalog entry that sets width 0 must actually be frameless.

    How a regression manifests: the four corners of the margin-inset
    rectangle are black, so a width-0 button is boxed again.
    """
    IconButtonWidget = _import_icon_button_widget()
    bordered = _play_sized_button(IconButtonWidget, border_width=2)
    bordered_sprite = Image.new("1", (128, 140), 1)
    bordered.render(bordered_sprite)
    assert _frame_corners(bordered_sprite, bordered.margin) == (0, 0, 0, 0)

    borderless = _play_sized_button(IconButtonWidget, border_width=0)
    borderless_sprite = Image.new("1", (128, 140), 1)
    borderless.render(borderless_sprite)
    assert all(
        pixel in _WHITE for pixel in _frame_corners(borderless_sprite, borderless.margin)
    ), "a width-0 button grew a frame at the margin"


def test_zero_border_width_selected_does_not_stroke_the_frame():
    """A selected borderless button must not grow a 1px outline.

    Why this test exists: the selected path always stroked `outline=0` after
    the dither fill, independent of border_width. A width-0 row that is
    selected by default would still show a box.

    How a regression manifests: the left edge at x=margin is solid black
    (the outline) instead of the Bayer dither, which has white pixels in
    that column at shade 12.
    """
    IconButtonWidget = _import_icon_button_widget()
    widget = _play_sized_button(IconButtonWidget, border_width=0, selected=True)
    sprite = Image.new("1", (128, 140), 1)
    widget.render(sprite)
    x = widget.margin
    left_edge = [
        sprite.getpixel((x, y))
        for y in range(widget.margin, widget.height - widget.margin)
    ]
    assert any(pixel in _WHITE for pixel in left_edge), (
        "selected outline reintroduced a solid left stroke"
    )
