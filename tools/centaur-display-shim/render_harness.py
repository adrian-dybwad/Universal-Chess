#!/usr/bin/env python3
"""On-device harness: render centaur's frames onto the real panel via UC.

This validates Milestone B (the live render path) without the menu: it builds
the production EPD + Manager (so it drives the panel exactly as the service
does), starts the threaded gateway pointed at ``Manager.display_frame``, and
launches centaur under the shim as the current (pi) user. centaur's frames are
decoded and rendered onto whatever panel is fitted.

Run with the universal-chess service STOPPED (this process must own the panel):

    sudo systemctl stop universal-chess.service
    PYTHONPATH=/opt /opt/universalchess/.venv/bin/python render_harness.py

Ctrl-C or centaur exit ends the session and releases the panel.
"""

import os
import subprocess  # nosec B404 - dev harness launches the trusted local centaur binary
import sys
import time

from universalchess.epaper.framework.manager import Manager
from universalchess.epaper.framework.waveshare.epd2in9d import EPD
from universalchess.epaper.framework.waveshare import waveform_profiles as wp
from universalchess.services.centaur_display import (
    CentaurDisplayGateway,
    ThreadedGatewayServer,
    DEFAULT_SOCKET_PATH,
)

CENTAUR_DIR = "/home/pi/centaur"
SHIM = os.path.join(CENTAUR_DIR, "spishim.so")


def _wait(promise, what):
    if promise is not None and hasattr(promise, "result"):
        try:
            promise.result(timeout=20.0)
        except Exception as exc:  # noqa: BLE001 - harness diagnostics only
            print(f"[harness] {what} wait failed (continuing): {exc}", flush=True)


def main() -> None:
    print("[harness] building EPD + Manager (UC8151D)", flush=True)
    epd = EPD(profile=wp.get_profile("", wp.CONTROLLER_UC8151D))
    manager = Manager(epd=epd, batch_updates=False)
    _wait(manager.initialize(), "initialize")

    rendered = {"n": 0}

    def render(image):
        rendered["n"] += 1
        print(f"[harness] rendering frame #{rendered['n']} to panel", flush=True)
        return manager.display_frame(image)

    gateway = CentaurDisplayGateway(render_fn=render)
    server = ThreadedGatewayServer(gateway, socket_path=DEFAULT_SOCKET_PATH)
    server.start()
    time.sleep(1.0)

    env = dict(os.environ)
    env["LD_PRELOAD"] = SHIM
    env["UC_CENTAUR_DISPLAY_SOCK"] = DEFAULT_SOCKET_PATH
    env["UC_CENTAUR_BUSY_IDLE_HIGH"] = "1"

    print("[harness] launching centaur under shim (as current user)", flush=True)
    try:
        subprocess.run(["./centaur"], cwd=CENTAUR_DIR, env=env, check=False)  # noqa: S607  # nosec B603 B607 - dev harness, fixed local binary
    finally:
        print(f"[harness] centaur exited; {rendered['n']} frames rendered", flush=True)
        server.stop()
        manager.release_hardware()


if __name__ == "__main__":
    sys.exit(main())
