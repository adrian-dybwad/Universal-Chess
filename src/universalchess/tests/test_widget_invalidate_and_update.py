#!/usr/bin/env python3
"""Tests for Widget.invalidate_and_update().

Why this exists
---------------
Across the widget set, the common "my own content changed" path repeated the
two-line pair `invalidate_cache(); request_update(...)`. invalidate_and_update()
is the DRY helper for that case. It must do BOTH, in order: invalidate this
widget's sprite cache (so draw_on() re-renders it) and then trigger a display
refresh, forwarding the refresh-policy args (full/forced/immediate) unchanged
and returning request_update()'s result.

These tests pin the helper's contract so it cannot silently drift into doing
only one of the two operations, or mangling the forwarded arguments.
"""

from unittest.mock import MagicMock

from PIL import Image

from universalchess.epaper.framework.widget import Widget


class _FakeWidget(Widget):
    """Minimal concrete Widget; render() is unused (cache is set directly)."""

    def render(self, sprite: Image.Image) -> None:
        pass


def _cached_widget(update_callback) -> _FakeWidget:
    """Build a widget that already holds a (stale) cached sprite."""
    widget = _FakeWidget(0, 0, 10, 10, update_callback=update_callback)
    # Simulate a prior render so we can prove the cache gets cleared.
    widget._cached_sprite = Image.new("1", (10, 10), 255)
    return widget


def test_invalidate_and_update_clears_cache_and_forwards_full_and_immediate():
    """Helper must clear the sprite cache AND request an update with the same
    full/immediate flags it was given, returning request_update()'s result.

    Regression: if it forgot to invalidate, draw_on() would re-blit the stale
    sprite (the original keyboard bug). If it dropped/mangled the flags, a
    caller asking for an immediate full refresh would get a batched partial one.
    request_update() forwards exactly (full, immediate) to the update callback.
    """
    update_callback = MagicMock(return_value="future-sentinel")
    widget = _cached_widget(update_callback)

    result = widget.invalidate_and_update(full=True, immediate=True)

    assert widget._cached_sprite is None, "sprite cache was not invalidated"
    update_callback.assert_called_once_with(True, True)
    assert result == "future-sentinel", "must return request_update()'s result"


def test_invalidate_and_update_defaults_to_partial_batched_refresh():
    """With no args, the helper requests a partial (full=False), batched
    (immediate=False) refresh -- the overwhelmingly common widget case.

    Regression: a wrong default (e.g. full=True) would make ordinary content
    updates trigger flashing full-screen refreshes.
    """
    update_callback = MagicMock(return_value=None)
    widget = _cached_widget(update_callback)

    widget.invalidate_and_update()

    assert widget._cached_sprite is None
    update_callback.assert_called_once_with(False, False)


def test_invalidate_and_update_forced_updates_hidden_widget():
    """forced=True must let a hidden widget still drive the refresh (used by
    show()/hide()), and the cache is invalidated regardless of visibility.

    Regression: without forwarding forced, show()/hide() converted to this
    helper would no-op on the (currently hidden) widget and the visibility
    change would never reach the panel.
    """
    update_callback = MagicMock(return_value=None)
    widget = _cached_widget(update_callback)
    widget.visible = False

    widget.invalidate_and_update(forced=True)

    assert widget._cached_sprite is None
    update_callback.assert_called_once_with(False, False)


def test_invalidate_and_update_hidden_not_forced_invalidates_but_skips_update():
    """A hidden, non-forced widget must NOT trigger a refresh (request_update
    short-circuits for hidden widgets), but the cache is still invalidated so
    the next time it becomes visible it re-renders fresh.

    Regression: if invalidation happened only after the visibility gate, a
    hidden widget whose state changed would later show a stale sprite when
    shown. The helper invalidates first, then requests, so the cache clears
    even though the update is suppressed.
    """
    update_callback = MagicMock(return_value=None)
    widget = _cached_widget(update_callback)
    widget.visible = False

    result = widget.invalidate_and_update()  # not forced

    assert widget._cached_sprite is None, "cache must clear even when update is suppressed"
    update_callback.assert_not_called()
    assert result is None, "hidden+unforced request_update returns None"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
