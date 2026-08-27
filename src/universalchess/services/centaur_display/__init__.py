"""Centaur display-translation gateway.

Lets the original DGT Centaur software drive whatever e-paper panel Universal
Chess has installed. centaur talks to a *virtual* panel (an LD_PRELOAD shim that
fakes the panel handshake and absorbs its real SPI/GPIO); the shim forwards the
DC-tagged SPI byte stream to this package, which decodes it back into a
framebuffer image and renders it through UC's driver stack
(``Manager.display_frame``).

Modules:
    decoder: Pure per-controller SPI-stream -> framebuffer-image decoder.
    protocol: Wire format (DC-tagged SPI records plus GPIO/SPI observation).
    gateway: Socket endpoint that decodes the stream and renders each frame.
    observed_io: Last Translate Mode pin/SPI snapshot for the Settings card.
    shim_builder: Compiles the LD_PRELOAD shim on-device from shipped source.
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
    render_and_signal,
)

# NOTE: ``shim_builder`` is intentionally NOT re-exported here. It is a
# provisioning concern (compiling the LD_PRELOAD shim on-device), not part of the
# render API, and it is invoked as ``python -m ...centaur_display.shim_builder``
# from the deb postinst -- eager-importing it in this package __init__ makes that
# ``-m`` run warn (module already in sys.modules). Import it from the submodule.

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
    "render_and_signal",
]
