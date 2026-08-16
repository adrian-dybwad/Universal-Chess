"""Display > Text Size scaling for icon menus.

Why these tests exist
---------------------
Text Size previously scaled only the coach panel and the analysis move list.
Menus kept their catalog font sizes and a 45px minimum row height at every
setting, so Large did not use the extra space already allocated when few
rows share the panel, and dense menus packed 20px glyphs into 45px rows.

How a regression manifests
--------------------------
Large min_button_height stays 45 (dense menus do not grow, visible count
does not drop), button font_size stays 16, or the Text Size option preview
rows (13/16/20) get scaled a second time so "Large" renders at 25px.
"""

from universalchess.epaper.icon_menu import IconMenuEntry, IconMenuWidget
from universalchess.epaper.text_scale import scale_font

WIDTH = 128
HEIGHT = 280


def _entries(count=3, font_size=16, scale_with_text_size=True):
    return [
        IconMenuEntry(
            key=str(index),
            label=f"Row {index}",
            icon_name="home",
            font_size=font_size,
            scale_with_text_size=scale_with_text_size,
        )
        for index in range(count)
    ]


def _menu(entries, text_size="medium"):
    return IconMenuWidget(
        0, 0, WIDTH, HEIGHT, lambda *a, **k: None,
        entries=entries, text_size=text_size,
    )


def test_medium_keeps_the_unscaled_minimum_row_height():
    """medium is identity: existing menus must not shift when the setting is
    first wired through. Failure: min_button_height is no longer 45.
    """
    assert _menu(_entries()).min_button_height == 45


def test_large_raises_minimum_row_height():
    """Large must demand taller rows so 20px labels are not packed into 45px.
    Failure: min_button_height stays 45 or is not the scaled 45.
    """
    large = _menu(_entries(), text_size="large")
    assert large.min_button_height == scale_font(45, "large")
    assert large.min_button_height > 45


def test_large_scales_catalog_font_sizes_onto_buttons():
    """Catalog font_size is the medium design; Large multiplies it. Failure:
    the button still has font_size 16.
    """
    large = _menu(_entries(font_size=16), text_size="large")
    assert large._buttons[0].font_size == scale_font(16, "large")
    assert large._buttons[0].description_font_size == scale_font(11, "large")


def test_preview_font_size_is_not_scaled():
    """Text Size option rows declare 13/16/20 so the list previews the sizes.
    Scaling those with the current setting would make Large render at 25px.
    Failure: scale_with_text_size=False still multiplies font_size.
    """
    large = _menu(
        _entries(font_size=20, scale_with_text_size=False),
        text_size="large",
    )
    assert large._buttons[0].font_size == 20


def test_large_fits_fewer_rows_on_a_dense_menu():
    """Eight equal rows already scroll at medium; Large's taller minimum must
    show fewer of them, each taller. Failure: visible_count and button height
    match medium, so Large did not take more space.
    """
    many = _entries(count=8)
    medium = _menu(many, text_size="medium")
    large = _menu(many, text_size="large")
    assert large._visible_count < medium._visible_count
    assert large._buttons[0].height > medium._buttons[0].height
