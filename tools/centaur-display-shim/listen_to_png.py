#!/usr/bin/env python3
"""Dev harness: decode the centaur display shim's socket stream to PNG files.

Used to validate the shim + decoder on-device without driving a real panel.
Run it (with PYTHONPATH=/opt so it imports the deployed package), then launch
centaur under LD_PRELOAD=spishim.so pointing at the same socket. Each refresh
centaur performs is decoded and written as a PNG so the reconstructed frames can
be inspected.

    PYTHONPATH=/opt /opt/universalchess/.venv/bin/python \
        listen_to_png.py /tmp/uc-centaur.sock /tmp/centaur_frames
"""

import os
import sys

from universalchess.services.centaur_display.gateway import CentaurDisplayGateway


def main() -> None:
    sock_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/uc-centaur.sock"  # noqa: S108  # nosec B108 - dev harness scratch path
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/centaur_frames"  # noqa: S108  # nosec B108 - dev harness scratch path
    os.makedirs(out_dir, exist_ok=True)

    count = {"n": 0}
    gateway_holder = {}

    def render(image):
        count["n"] += 1
        controller = gateway_holder["gw"]._decoder.controller
        path = os.path.join(out_dir, f"frame_{count['n']:03d}.png")
        image.convert("1").save(path)
        print(f"[listener] frame {count['n']:03d} controller={controller} -> {path}",
              flush=True)

    gateway = CentaurDisplayGateway(render_fn=render)
    gateway_holder["gw"] = gateway
    print(f"[listener] listening on {sock_path}, writing to {out_dir}", flush=True)
    gateway.serve(socket_path=sock_path)


if __name__ == "__main__":
    main()
