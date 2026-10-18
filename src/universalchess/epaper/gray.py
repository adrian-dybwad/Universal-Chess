"""Four-level gray for the one panel that can show it.

GDEW029I6FD is driven with the GxEPD2_4G waveform, which holds white, light
gray, dark gray, and black. Every other panel is 1-bit, and shades stay a
Bayer dither. The display manager makes the framebuffer 8-bit ("L") only for
that panel, and widget sprites take the mode of the canvas, so a widget reads
the choice from the image it draws on and never has to know which glass is
installed.

Shade numbers match the widget background scale: 0 is white, 16 is black,
and 8 is the 50% dither used for dark squares.
"""

# Bands of the 0..16 shade scale. 8 (the dark-square dither) is light gray,
# so a black piece still reads on it. 12 (the selected-button shade, about
# 75% black) is dark gray.
_WHITE_MAX = 3
_LIGHT_MAX = 8
_DARK_MAX = 12


def draws_gray(image) -> bool:
    """True when ``image`` is 8-bit, the mode used only for the 4-gray panel.

    A flat gray fill is only correct there: on a 1-bit image it thresholds to
    white or black, so every 1-bit image is drawn with the Bayer dither.
    """
    return image.mode == "L"


def shade_luma(shade: int) -> int:
    """Map a 0..16 shade onto a pixel value (255 white .. 0 black).

    The four values sit in the middle of the bins ``pack_gray4`` quantizes,
    so a flat fill survives the trip to the two controller planes.
    """
    shade = max(0, min(16, int(shade)))
    if shade <= _WHITE_MAX:
        return 255
    if shade <= _LIGHT_MAX:
        return 170
    if shade <= _DARK_MAX:
        return 85
    return 0
