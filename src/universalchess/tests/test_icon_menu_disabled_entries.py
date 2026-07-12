#!/usr/bin/env python3
"""Disabled menu entries render (greyed) but are non-selectable -- never hidden.

Why these tests exist
---------------------
``enabled=False`` was previously overloaded to *hide* a row (the widget filtered
disabled entries out entirely). That is the wrong contract: hiding a row is the
job of omission (e.g. the catalog's ``visibleWhen``), while ``enabled=False``
must mean "the control exists but is currently unavailable" -- visible, greyed,
skipped in navigation, and not activatable. These tests pin that contract so the
hide-via-enabled anti-pattern cannot creep back.
"""

import sys
from unittest.mock import MagicMock

# Stub the serial stack so the board module (imported lazily by handle_key)
# loads on non-hardware machines. PIL is intentionally real (buttons render).
for _mod in ("serial", "serial.tools", "serial.tools.list_ports"):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.epaper.icon_button import IconButtonWidget
from universalchess.epaper.icon_menu import IconMenuEntry, IconMenuWidget


def _menu(entries):
    return IconMenuWidget(0, 0, 128, 296, update_callback=lambda *a, **k: None, entries=entries)


def test_disabled_entry_is_not_filtered_out():
    """A disabled entry stays in the list rather than being dropped.

    Regression: reinstating the ``[e for e in entries if e.enabled]`` filter
    makes the disabled middle entry vanish, so the count falls from 3 to 2 and
    the user can no longer see that the option exists.
    """
    entries = [
        IconMenuEntry(key="a", label="A", icon_name="home", enabled=True),
        IconMenuEntry(key="b", label="B", icon_name="gear", enabled=False),
        IconMenuEntry(key="c", label="C", icon_name="info", enabled=True),
    ]
    menu = _menu(entries)
    assert [e.key for e in menu.entries] == ["a", "b", "c"]


def test_disabled_entry_is_not_selectable():
    """Navigation treats a disabled entry as non-selectable.

    Regression: if ``_is_selectable`` ignores ``enabled``, the cursor can land
    on the disabled entry and TICK (which gates on ``_is_selectable``) would
    activate an unavailable control.
    """
    entries = [
        IconMenuEntry(key="a", label="A", icon_name="home", enabled=True),
        IconMenuEntry(key="b", label="B", icon_name="gear", enabled=False),
        IconMenuEntry(key="c", label="C", icon_name="info", enabled=True),
    ]
    menu = _menu(entries)
    assert menu._is_selectable(1) is False
    # DOWN from the first entry skips the disabled one and lands on "c".
    assert menu._find_next_selectable(0, 1) == 2


def test_initial_selection_skips_leading_disabled_entry():
    """A disabled first entry is not focused on open.

    Regression: leaving the cursor on a disabled leading row makes the menu open
    with an unavailable control focused (and BACK/TICK behaving oddly).
    """
    entries = [
        IconMenuEntry(key="hdr", label="Header", icon_name="info", enabled=False),
        IconMenuEntry(key="go", label="Go", icon_name="play", enabled=True),
    ]
    menu = _menu(entries)
    assert menu.selected_index == 1


def _black_pixels(button: IconButtonWidget) -> int:
    sprite = Image.new("1", (button.width, button.height), 255)
    button.render(sprite)
    return sum(1 for p in sprite.getdata() if p == 0)


def test_disabled_button_renders_greyed_not_blank():
    """A disabled button fades its content (dithered) but still draws it.

    Regression: a disabled control that renders identically to an enabled one
    gives the user no signal it is unavailable; one that renders blank hides it.
    The disabled button must have fewer black pixels than the enabled one (faded)
    yet still more than an empty button (content present).
    """
    common = dict(x=0, y=0, width=128, height=70, update_callback=lambda *a, **k: None,
                  key="k", label="Label", icon_name="gear")
    enabled = IconButtonWidget(**common, enabled=True)
    disabled = IconButtonWidget(**common, enabled=False)
    enabled_ink = _black_pixels(enabled)
    disabled_ink = _black_pixels(disabled)
    # Faded: strictly less ink than enabled, but content is still present.
    assert 0 < disabled_ink < enabled_ink
