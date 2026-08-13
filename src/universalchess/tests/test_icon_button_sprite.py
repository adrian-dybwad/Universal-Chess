"""Tests for IconButtonWidget sprite-preview icon image and radio indicator.

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
from unittest.mock import MagicMock

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
