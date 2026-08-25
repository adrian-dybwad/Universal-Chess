"""Splash text stays inside its bands, and clear of the battery, in every language.

Why these tests exist
---------------------
The splash was laid out around English proportions and nothing checked the
translations against it. Three separate faults were shipping at once, each
invisible off-device:

- French ``power.press_play`` ("Appuyez sur [>]") needs two lines at font 18.
  The battery is drawn at a fixed ``_message_y + 24``, which assumes exactly one
  line, so the second line landed on the icon.
- German ``splash.tagline`` wraps to four lines in a band sized for three. WRAP
  drops what does not fit, so the byline was silently truncated.
- Dutch ``splash.centaur_hold_back`` wraps to six lines in a band that holds
  five, losing the last line of a hint whose whole purpose is to be read.

The two failure modes are different and need different assertions: the French
one draws over another element, while the German and Dutch ones quietly lose
text. Both are checked here.

Why the real font matters
-------------------------
``get_font(size)`` falls back to ``ImageFont.load_default()`` when no
``ResourceLoader`` is registered, and that default ignores the requested size --
it is a fixed ~10px bitmap font. Every measurement taken without the
``bundled_fonts`` fixture is therefore against the wrong font and will report
that everything fits. That is exactly how all three faults above passed review,
via ``scripts/measure_locale_fit.py``, which registered no loader.

How a regression manifests
--------------------------
A translated string grows, or a band is resized, and either the message reaches
the battery again or a line is dropped off the bottom of its band.
"""

import json
import pathlib

import pytest

from universalchess.epaper.splash_screen import SplashScreen
from universalchess.resources import (
    ResourceLoader,
    get_resource_loader,
    set_resource_loader,
)
from universalchess.tests.test_text_overflow import RES_DIR

LOCALE_DIR = pathlib.Path(__file__).resolve().parents[1] / "i18n" / "locale"

# Every shipped bundle. English is included deliberately: it is the source
# language, so a band that cannot even hold English is a layout bug rather than
# a translation one, and the distinction matters when one of these fails.
LOCALES = ["en", "de", "es", "fr", "nl", "pl"]

TAGLINE_KEY = "splash.tagline"
PRESS_PLAY_KEY = "power.press_play"
HOLD_BACK_KEY = "splash.centaur_hold_back"


@pytest.fixture
def bundled_fonts():
    """Measure with the bundled TrueType face, not PIL's default bitmap font."""
    previous = get_resource_loader()
    set_resource_loader(ResourceLoader(RES_DIR))
    yield
    set_resource_loader(previous)


def _bundle(code):
    return json.loads((LOCALE_DIR / f"{code}.json").read_text("utf-8"))


def _splash(message, tagline=None, show_battery=False):
    """Build a real full-screen splash, as the shutdown and Centaur paths do."""
    return SplashScreen(
        lambda *args, **kwargs: None,
        message=message,
        leave_room_for_status_bar=False,
        show_battery=show_battery,
        tagline=tagline,
    )


def _needed_height(widget):
    """Height the text wants, ignoring the room the widget actually has.

    ``used_height()`` clamps to the widget, which is the drawn height -- useful
    for collisions but useless for spotting loss, because a dropped line simply
    does not appear in it. Comparing this against the widget height is what
    exposes truncation.
    """
    line_height = widget.fitted_font_size + 2
    lines = widget.wrap_lines() if widget.fitted_wrap else widget.text.split("\n")
    return max(1, len(lines)) * line_height


def _overflowing_lines(widget):
    """Return the lines that are wider than the column, which would clip."""
    lines = widget.wrap_lines() if widget.fitted_wrap else widget.text.split("\n")
    return [
        line for line in lines
        if line and widget._text_len(line, widget._font) > widget.width
    ]


@pytest.mark.parametrize("code", LOCALES)
def test_shutdown_message_never_reaches_the_battery(bundled_fonts, code):
    """The shutdown prompt must not draw over the battery icon.

    Why: this is the reported fault. The prompt is the last thing shown before
    the board sleeps and the battery is the reason it is shown at all -- a
    charge reading with a line of text through it is worse than no reading,
    because it is still legible enough to be misread.

    How a regression manifests: the message wraps (French needs two lines for
    "Appuyez sur [>]") and its second line overlaps the icon, so the bottom of
    the drawn message sits below the top of the battery.
    """
    bundle = _bundle(code)
    splash = _splash(bundle[PRESS_PLAY_KEY], tagline=bundle[TAGLINE_KEY],
                     show_battery=True)

    message_bottom = splash._message_y + splash._text_widget.used_height()

    assert message_bottom <= splash._battery_y, (
        f"{code}: {bundle[PRESS_PLAY_KEY]!r} is drawn down to y={message_bottom}, "
        f"over the battery at y={splash._battery_y}"
    )

    # Moving the icon down to dodge the text is only a fix if it is still on the
    # panel afterwards. Without the room being reserved from the message's own
    # height, a two-line message pushes the percentage off the bottom edge --
    # trading text drawn through the icon for no reading at all.
    stack_bottom = splash._battery_percent_y + SplashScreen.BATTERY_PERCENT_HEIGHT
    assert stack_bottom <= splash.height, (
        f"{code}: battery stack ends at y={stack_bottom}, past the "
        f"{splash.height}px panel"
    )


@pytest.mark.parametrize("code", LOCALES)
def test_the_tagline_is_never_truncated(bundled_fonts, code):
    """Every word of the byline must be drawn, not dropped off its band.

    Why: WRAP silently discards lines past the widget height, so an over-long
    translation is not reported anywhere -- it just renders a byline missing its
    end, which reads as a rendering fault rather than a text-length one. German
    needs four lines in a band sized for three.

    How a regression manifests: the wrapped text needs more height than the
    tagline band has, so its final line never reaches the panel.
    """
    bundle = _bundle(code)
    splash = _splash(bundle[PRESS_PLAY_KEY], tagline=bundle[TAGLINE_KEY])
    tagline = splash._tagline_text

    assert _needed_height(tagline) <= tagline.height, (
        f"{code}: tagline {bundle[TAGLINE_KEY]!r} needs "
        f"{_needed_height(tagline)}px in a {tagline.height}px band"
    )
    assert _overflowing_lines(tagline) == [], (
        f"{code}: tagline lines wider than the {tagline.width}px column: "
        f"{_overflowing_lines(tagline)}"
    )


@pytest.mark.parametrize("code", LOCALES)
def test_the_centaur_exit_hint_is_never_truncated(bundled_fonts, code):
    """The held-BACK hint must survive translation intact.

    Why: this splash is the only place on the device that teaches the gesture
    for leaving the original Centaur -- once Centaur paints, Universal Chess
    cannot address the panel again. A hint missing its last line is worse than
    useless, because it names the button without saying what to do with it.
    Dutch wraps to six lines where five fit.

    How a regression manifests: the wrapped hint needs more height than the
    message band has, so the instruction is cut off mid-sentence.
    """
    bundle = _bundle(code)
    splash = _splash(bundle[HOLD_BACK_KEY])
    message = splash._text_widget

    assert _needed_height(message) <= message.height, (
        f"{code}: hint {bundle[HOLD_BACK_KEY]!r} needs "
        f"{_needed_height(message)}px in a {message.height}px band"
    )
    assert _overflowing_lines(message) == [], (
        f"{code}: hint lines wider than the {message.width}px column: "
        f"{_overflowing_lines(message)}"
    )


@pytest.mark.parametrize("code", LOCALES)
def test_a_message_under_a_tagline_gets_only_the_room_that_is_left(bundled_fonts, code):
    """The message widget must be sized to the space below the byline.

    Why this test exists: the widget was always built with ``TEXT_HEIGHT`` (110)
    even when a tagline pushed it down to y=238, leaving 58. Wrapping decisions
    were therefore made against room that does not exist, so text the widget
    believed it had placed ran off the bottom of the panel. This is the
    structural cause behind the individual string failures, and pinning it stops
    them recurring the next time a translation grows.

    How a regression manifests: the height reverts to the constant, and the
    widget again reports space past the end of the screen.
    """
    bundle = _bundle(code)
    splash = _splash(bundle[PRESS_PLAY_KEY], tagline=bundle[TAGLINE_KEY])

    available = splash.height - splash._message_y

    assert splash._text_widget.height == available, (
        f"{code}: message widget claims {splash._text_widget.height}px but only "
        f"{available}px remain below the tagline"
    )
    assert splash._message_y + splash._text_widget.height <= splash.height
