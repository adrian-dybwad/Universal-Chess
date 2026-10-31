"""Tests for IconButtonWidget sprite-preview icons, radio markers, and frames.

Background / why these tests exist
----------------------------------
The Board > Sprites radio list needs two new IconButtonWidget capabilities:

1. A radio indicator icon (radio_checked / radio_empty) used as the trailing
   marker showing which sheet is active.
2. The ability to render an arbitrary preview image (a sheet's black king) as
   the button's main icon, composited through a transparency mask so it sits on
   the menu background instead of painting a white box.

"""

from unittest.mock import MagicMock, patch

from PIL import Image

from universalchess.epaper.icon_button import IconButtonWidget


def test_radio_checked_draws_outer_and_inner_circle():
    """radio_checked draws the ring plus a filled centre dot.

    Why: the filled radio marks the active sheet. A checked radio must render two
    ellipses (outer ring + inner dot); an empty one renders only the ring. If the
    fill were dropped the active sheet would be indistinguishable from the rest.
    """
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


def _square_knight_logo(size: int, ink_origin: int, ink_size: int):
    """Black filled square on a white field, with an opaque mask over the ink."""
    logo = Image.new("1", (size, size), 1)
    mask = Image.new("1", (size, size), 0)
    for y in range(ink_origin, ink_origin + ink_size):
        for x in range(ink_origin, ink_origin + ink_size):
            logo.putpixel((x, y), 0)
            mask.putpixel((x, y), 255)
    return logo, mask


def test_selected_universal_logo_keeps_black_ink_and_white_halo():
    """Selected PLAY must not invert the knight; a 1px white ring holds the dither off.

    Why this test exists: inverting the bitmap on the dithered fill turned the
    horse into a white blob, and without a halo black dither meets the black
    silhouette so the profile disappears.

    How a regression manifests: the interior of a known-black square logo is
    white (inverted), or a pixel on the 1px ring around it is black (dither
    touching the body).
    """
    from universalchess.epaper.icon_button import set_knight_logo

    size, ink_origin, ink_size = 32, 12, 8
    set_knight_logo(size, *_square_knight_logo(size, ink_origin, ink_size))

    width = height = 80
    widget = IconButtonWidget(
        0, 0, width, height,
        update_callback=lambda *a, **k: None,
        key="Universal",
        label="",
        icon_name="universal_logo",
        selected=True,
        icon_size=size,
        layout="vertical",
    )
    sprite = Image.new("1", (width, height), 1)
    widget.render(sprite)

    paste = (
        widget.margin + widget.border_width + widget.padding
        + (width - 2 * (widget.margin + widget.border_width + widget.padding)) // 2
        - size // 2
    )
    interior = (paste + ink_origin, paste + ink_origin)
    assert sprite.getpixel(interior) == 0, "selected knight was inverted to white"

    ring = (paste + ink_origin - 1, paste + ink_origin)
    assert sprite.getpixel(ring) in _WHITE, "dither meets the silhouette; white halo missing"


def test_horizontal_checkbox_with_description_stays_inside_border():
    """Engine-list rows: checkbox must not climb through the button border.

    Why this test exists: Chess Engines draws each engine as a horizontal
    IconButton with a checkbox icon plus a teaser description under the
    name. Description wrapping used to claim vertical space first, leaving
    an icon_label band shorter than icon_size; the 36px checkbox was still
    centered in that band and painted through the top border and over the
    teaser text.

    How a regression manifests: black ink appears in the transparent margin
    above the border (y < margin), the same overflow seen on the board.
    """
    margin = 4
    width, height = 200, 64
    widget = IconButtonWidget(
        0, 0, width, height,
        update_callback=lambda *a, **k: None,
        key="stockfish",
        label="Stockfish (~12m)\n~3550 Elo",
        icon_name="checkbox_checked",
        selected=False,
        enabled=True,
        margin=margin,
        icon_size=36,
        layout="horizontal",
        font_size=11,
        border_width=2,
        padding=2,
        description=(
            "A strong open-source chess engine used worldwide "
            "for analysis and play."
        ),
        description_font_size=10,
    )
    sprite = Image.new("L", (width, height), 255)
    widget.render(sprite)

    outside = 0
    for y in range(height):
        for x in range(width):
            if sprite.getpixel((x, y)) >= 128:
                continue
            if (
                x < margin
                or x > width - 1 - margin
                or y < margin
                or y > height - 1 - margin
            ):
                outside += 1
    assert outside == 0, (
        f"{outside} ink pixels outside the button border "
        "(checkbox overflowed the frame)"
    )


def test_selected_universal_logo_mattes_enclosed_face_and_neck():
    """Enclosed white of the horse (face, neck) must stay white on a selected row.

    Why this test exists: the stored mask is black ink only. Dilating that
    ink for a halo left the face and neck as holes, so the dithered fill
    showed through them.

    How a regression manifests: the centre of a black ring (paper inside
    the profile, not in the ink mask) is black from the Bayer dither.
    """
    from universalchess.epaper.icon_button import set_knight_logo

    size = 32
    logo = Image.new("1", (size, size), 1)
    mask = Image.new("1", (size, size), 0)
    # 12x12 black frame at (10,10)..(21,21) with a 6x6 white hole at (13,13)..(18,18).
    for y in range(10, 22):
        for x in range(10, 22):
            if x < 13 or x > 18 or y < 13 or y > 18:
                logo.putpixel((x, y), 0)
                mask.putpixel((x, y), 255)
    set_knight_logo(size, logo, mask)

    width = height = 80
    widget = IconButtonWidget(
        0, 0, width, height,
        update_callback=lambda *a, **k: None,
        key="Universal",
        label="",
        icon_name="universal_logo",
        selected=True,
        icon_size=size,
        layout="vertical",
    )
    sprite = Image.new("1", (width, height), 1)
    widget.render(sprite)

    paste = (
        widget.margin + widget.border_width + widget.padding
        + (width - 2 * (widget.margin + widget.border_width + widget.padding)) // 2
        - size // 2
    )
    hole = (paste + 15, paste + 15)
    frame = (paste + 10, paste + 10)
    assert sprite.getpixel(frame) == 0, "ink frame was not black"
    assert sprite.getpixel(hole) in _WHITE, "dither filled the enclosed face/neck"
