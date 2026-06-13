"""Passkey display widget for Bluetooth keyboard pairing.

When a Bluetooth keyboard pairs, BlueZ asks the host (via its pairing agent) to
display a numeric passkey. The user types that passkey on the keyboard and
presses Enter to complete pairing. This full-screen modal widget shows the
passkey and instructions while pairing is in progress; the application removes
it once pairing completes or is cancelled.
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class PasskeyWidget(Widget):
    """Full-screen modal widget showing a Bluetooth pairing passkey.

    Args:
        update_callback: Callback to trigger display updates. Must not be None.
        passkey: Six-digit passkey string to display (already formatted).
        device_name: Optional name of the device being paired.
    """

    is_modal = True

    TITLE_Y = 30
    PROMPT_Y = 70
    PASSKEY_Y = 130
    INSTRUCTION_Y = 200
    DEVICE_Y = 250

    def __init__(self, update_callback, passkey: str, device_name: str = ""):
        super().__init__(0, 0, 128, 296, update_callback)
        self._passkey = passkey
        self._device_name = device_name
        self._font_loader = None

    def _get_font_loader(self):
        if self._font_loader is None:
            from universalchess.resources import ResourceLoader
            self._font_loader = ResourceLoader(
                "/opt/universalchess/resources", "/home/pi/resources")
        return self._font_loader

    def set_passkey(self, passkey: str) -> None:
        """Update the displayed passkey and refresh the screen."""
        self._passkey = passkey
        self.invalidate_and_update()

    def render(self, sprite: Image.Image) -> None:
        """Render the passkey screen onto the sprite."""
        self.draw_background_on_sprite(sprite)
        draw = ImageDraw.Draw(sprite)
        loader = self._get_font_loader()

        title_font = loader.get_font(14)
        passkey_font = loader.get_font(28)
        body_font = loader.get_font(11)

        draw.text((64, self.TITLE_Y), "Pair Keyboard",
                  font=title_font, fill=0, anchor="mm")
        draw.text((64, self.PROMPT_Y), "Type this code",
                  font=body_font, fill=0, anchor="mm")
        draw.text((64, self.PASSKEY_Y), self._passkey,
                  font=passkey_font, fill=0, anchor="mm")
        draw.text((64, self.INSTRUCTION_Y), "then press Enter",
                  font=body_font, fill=0, anchor="mm")

        if self._device_name:
            name = self._device_name[:18]
            draw.text((64, self.DEVICE_Y), name,
                      font=body_font, fill=0, anchor="mm")
