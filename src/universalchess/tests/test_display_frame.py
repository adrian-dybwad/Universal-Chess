"""Tests for Manager.display_frame() - external frame injection.

Background / why these tests exist
----------------------------------
The centaur display-translation gateway reconstructs centaur's framebuffer as a
PIL image and must push it to whatever panel is installed, going through the
same scheduler/driver path the widget pipeline uses (so it is driver-agnostic
and respects three-color routing). display_frame() is that injection point.

These pin the contract the gateway depends on:
- a partial, batched refresh is requested. Original centaur drives its own panel
  with incremental partial updates (reserving full refreshes for specific
  actions like its back/options buttons), and it emits frames faster than an
  e-paper full refresh completes. Forcing full+immediate per frame caused the
  panel to flash and thrash ('interrupted by newer data'); partial+batched lets
  the scheduler coalesce a burst to the latest frame and skip the full-refresh
  flash,
- the panel rotation is applied exactly as the normal snapshot path applies it
  (so an injected upright frame lands the right way up),
- a supplied red plane is forwarded for three-color panels.
"""

from unittest.mock import MagicMock, patch

from PIL import Image, ImageChops

from universalchess.epaper.framework.manager import Manager


def _manager_with_mock_scheduler():
    """Manager around a mock EPD with the scheduler replaced by a mock.

    submit() returns a sentinel so the wrapper's pass-through of the Future can
    be asserted without a running thread.
    """
    epd = MagicMock()
    epd.width = 128
    epd.height = 296
    manager = Manager(epd=epd, batch_updates=False)
    manager._scheduler = MagicMock()
    sentinel = object()
    manager._scheduler.submit.return_value = sentinel
    return manager, sentinel


def test_display_frame_requests_partial_batched_refresh_and_returns_future():
    """display_frame() must submit a partial, batched refresh and return its Future.

    Why: centaur drives its panel with incremental partial updates and emits
    frames faster than a full refresh completes, so full+immediate per frame made
    the panel flash and thrash. Partial (full=False) avoids the full-refresh
    flash; batched (immediate=False) lets the scheduler coalesce a rapid burst to
    the latest frame. Failure manifests as full/immediate being set again (the
    flashing/thrash regression returns) or the Future not propagated (callers
    cannot await the paint).
    """
    manager, sentinel = _manager_with_mock_scheduler()
    img = Image.new("1", (128, 296), 255)

    with patch("universalchess.epaper.framework.manager.epdconfig.ROTATION", 0):
        result = manager.display_frame(img)

    assert result is sentinel
    manager._scheduler.submit.assert_called_once()
    kwargs = manager._scheduler.submit.call_args.kwargs
    assert kwargs["full"] is False
    assert kwargs["immediate"] is False
    assert kwargs["red_image"] is None
    # ROTATION=0: the image is passed through unchanged.
    assert kwargs["image"] is img


def test_display_frame_applies_panel_rotation():
    """display_frame() must rotate the frame by -ROTATION, like snapshot() does.

    Why: FrameBuffer.snapshot(rotation=ROTATION) feeds the scheduler an image
    rotated by -ROTATION; an injected frame must match so it is not upside down
    on a 180-mounted panel. A distinctive corner pixel is tracked: at ROTATION
    180 a black (0,0) pixel must move to (127,295). Failure (no rotation)
    manifests as the submitted image differing from the expected rotation.
    """
    manager, _ = _manager_with_mock_scheduler()
    img = Image.new("1", (128, 296), 255)
    img.putpixel((0, 0), 0)  # distinctive corner to detect the rotation

    with patch("universalchess.epaper.framework.manager.epdconfig.ROTATION", 180):
        manager.display_frame(img)

    submitted = manager._scheduler.submit.call_args.kwargs["image"]
    expected = img.rotate(-180, expand=False)
    assert ImageChops.difference(submitted.convert("1"), expected.convert("1")).getbbox() is None


def test_display_frame_forwards_red_plane_for_three_color():
    """A supplied red plane must be forwarded (rotated) to the scheduler.

    Why: three-color panels render the red plane separately; the gateway may
    pass it through. Failure manifests as red_image dropped (red content lost on
    BWR panels).
    """
    manager, _ = _manager_with_mock_scheduler()
    img = Image.new("1", (128, 296), 255)
    red = Image.new("1", (128, 296), 255)

    with patch("universalchess.epaper.framework.manager.epdconfig.ROTATION", 0):
        manager.display_frame(img, red_image=red)

    kwargs = manager._scheduler.submit.call_args.kwargs
    assert kwargs["red_image"] is red


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
