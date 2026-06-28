"""Tests for the resources.get_font module-level convenience.

Why these tests exist
---------------------
Widgets used to acquire fonts three different ways: per-module singletons in
text/keyboard, and freshly-constructed ResourceLoaders in about/help/passkey.
The latter built a separate loader per widget, each with its own empty font
cache, so the bundled Font.ttc was re-loaded independently and the app-wide
cache was defeated.

resources.get_font() is the single acquisition point: it resolves via the
app-wide ResourceLoader singleton and falls back to PIL's default font when no
loader is registered. These tests pin:

1. With no loader registered, get_font falls back to ImageFont.load_default, so
   off-board/test contexts still get a usable font instead of None/an error.
2. With a loader registered, get_font delegates to that loader's get_font with
   the same arguments, so every caller shares one loader (and its cache).
3. Repeated calls return the identical cached font instance, proving the cache
   is shared rather than rebuilt per caller (the regression being fixed).
"""

import sys

import pytest

import universalchess.resources as resources_mod
from universalchess.resources import ResourceLoader, get_font, set_resource_loader


@pytest.fixture(autouse=True)
def _real_pil_and_clean_singleton():
    """Bind the real PIL.ImageFont and isolate the app-wide loader singleton.

    Other test modules replace PIL with a MagicMock in sys.modules at import
    time and never restore it; rebind the real PIL so load_default/truetype work.
    Also save/clear/restore the module global so each test starts with no
    registered loader and leaves none behind for unrelated tests.
    """
    for name in ("PIL.ImageFont", "PIL.ImageDraw", "PIL.Image", "PIL"):
        sys.modules.pop(name, None)
    import PIL.ImageFont as real_font
    resources_mod.ImageFont = real_font

    saved = resources_mod._resource_loader
    resources_mod._resource_loader = None
    yield
    resources_mod._resource_loader = saved


def test_falls_back_to_default_font_when_no_loader(monkeypatch):
    # Regression guard: without a registered loader (off-board/tests), callers
    # must still receive a usable font. If get_font returned None or raised here,
    # widget render would crash. A sentinel load_default proves this exact branch
    # was taken, not some incidentally-equal real font.
    sentinel = object()
    monkeypatch.setattr(resources_mod.ImageFont, "load_default", lambda: sentinel)
    assert get_font(12) is sentinel


def test_delegates_to_registered_loader_with_same_args():
    # Guards that get_font routes through the app-wide singleton so every caller
    # shares one loader and its cache. The fake records the call; if get_font
    # built its own loader or ignored the singleton, `recorded` would stay empty
    # and the returned value would not match.
    recorded = []

    class FakeLoader:
        def get_font(self, size, path=None):
            recorded.append((size, path))
            return f"font-{size}-{path}"

    set_resource_loader(FakeLoader())
    result = get_font(18, "/custom/Font.ttc")
    assert result == "font-18-/custom/Font.ttc"
    assert recorded == [(18, "/custom/Font.ttc")]


def test_repeated_calls_share_one_cached_font(tmp_path):
    # Guards the bug being fixed: three widgets each built a separate loader, so
    # each re-loaded its font into a private cache. Routed through one singleton,
    # repeated get_font(size) must return the identical cached object. An empty
    # resources dir (no Font.ttc) is fine: ResourceLoader caches the load_default
    # fallback by (path, size) just the same, so identity still proves sharing.
    set_resource_loader(ResourceLoader(str(tmp_path)))
    first = get_font(12)
    second = get_font(12)
    assert first is second
