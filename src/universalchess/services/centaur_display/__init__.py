"""Centaur display-translation gateway.

Lets the original DGT Centaur software drive whatever e-paper panel Universal
Chess has installed. centaur talks to a *virtual* panel (an LD_PRELOAD shim that
fakes the panel handshake and absorbs its real SPI/GPIO); the shim forwards the
DC-tagged SPI byte stream to this package, which decodes it back into a
framebuffer image and renders it through UC's driver stack
(``Manager.display_frame``).

Modules:
    decoder: Pure per-controller SPI-stream -> framebuffer-image decoder.
    protocol: Wire format (DC-tagged records) between the shim and the gateway.
    gateway: Socket endpoint that decodes the stream and renders each frame.
"""

from .decoder import (
    CentaurDisplayDecoder,
    ControllerProfile,
    UC8151D_PROFILE,
    SSD1680_PROFILE,
    PANEL_WIDTH,
    PANEL_HEIGHT,
)
from .gateway import (
    CentaurDisplayGateway,
    ThreadedGatewayServer,
    DEFAULT_SOCKET_PATH,
)

__all__ = [
    "CentaurDisplayDecoder",
    "ControllerProfile",
    "UC8151D_PROFILE",
    "SSD1680_PROFILE",
    "PANEL_WIDTH",
    "PANEL_HEIGHT",
    "CentaurDisplayGateway",
    "ThreadedGatewayServer",
    "DEFAULT_SOCKET_PATH",
]
