"""Armbian gpiochip/spidev nodes must be reachable by the service user.

Why these tests exist:
    After the Orange Pi overlay loaded, ``/dev/gpiochip*`` and
    ``/dev/spidev0.0`` were ``root:root`` mode 600. The board service runs as
    the UID 1000 user, so libgpiod and spidev would raise PermissionError
    even with RPi.GPIO gone. Raspberry Pi OS already ships gpio/spi udev
    rules; Armbian does not.

How a regression manifests:
    ``universal-chess.service`` starts, then dies opening ``/dev/gpiochip1``
    or ``/dev/spidev0.0`` as the service user.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import universalchess.services.update_service as um

DEB_ROOT = (
    Path(um.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
)
POSTINST = DEB_ROOT / "DEBIAN" / "postinst"
UDEV_RULES = DEB_ROOT / "usr" / "lib" / "udev" / "rules.d" / "60-universal-chess-gpio.rules"
ENSURE = "ensure_system_group"


def test_udev_rules_grant_gpiochip_and_spidev_to_named_groups():
    # Why: without GROUP/MODE, Armbian leaves the nodes 600 root. Manifests
    # as missing KERNEL gpiochip/spidev lines or MODE 666 (world-writable).
    text = UDEV_RULES.read_text()
    assert 'KERNEL=="gpiochip*"' in text
    assert 'GROUP="gpio"' in text
    assert 'KERNEL=="spidev*"' in text
    assert 'GROUP="spi"' in text
    assert 'MODE="0660"' in text
    assert 'MODE="0666"' not in text


def test_postinst_creates_gpio_and_spi_groups_then_adds_the_service_user():
    # Why: usermod into a missing group exits 6; udev GROUP=gpio is a no-op
    # if the group was never created. Manifests as no ensure_system_group
    # gpio/spi, or spi membership omitted.
    text = POSTINST.read_text()
    assert re.search(rf"^{ENSURE}\(\)", text, re.MULTILINE)
    gpio_idx = text.index(f"{ENSURE} gpio")
    spi_idx = text.index(f"{ENSURE} spi")
    add_gpio = text.index("add_primary_user_to_group_if_present gpio")
    add_spi = text.index("add_primary_user_to_group_if_present spi")
    assert gpio_idx < add_gpio
    assert spi_idx < add_spi


def test_ensure_system_group_creates_a_missing_group(tmp_path):
    # Why: Armbian has no gpio group. Manifests as skipping groupadd when
    # getent misses, so udev has nothing to chgrp to.
    proc, log = _run_ensure(tmp_path, present=set(), group="gpio")
    assert proc.returncode == 0, proc.stderr
    assert log.exists()
    assert "--system" in log.read_text()
    assert "gpio" in log.read_text()


def test_ensure_system_group_is_quiet_when_the_group_exists(tmp_path):
    # Why: Pi OS already has gpio. Manifests as groupadd on every upgrade
    # (exit 9 / abort under set -e).
    proc, log = _run_ensure(tmp_path, present={"gpio"}, group="gpio")
    assert proc.returncode == 0, proc.stderr
    assert not log.exists()


def _run_ensure(
    tmp_path: Path, present: set[str], group: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    text = POSTINST.read_text()
    match = re.search(rf"(?sm)^{ENSURE}\(\) \{{.*?^}}", text)
    assert match, f"{ENSURE} not found in postinst"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "groupadd.log"
    present_list = " ".join(sorted(present))
    getent = bindir / "getent"
    getent.write_text(
        "#!/bin/sh\n"
        f'for g in {present_list}; do [ "$2" = "$g" ] && exit 0; done\n'
        "exit 2\n"
    )
    getent.chmod(0o755)
    groupadd = bindir / "groupadd"
    groupadd.write_text(f"#!/bin/sh\necho \"$*\" >> '{log}'\n")
    groupadd.chmod(0o755)
    import os

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    proc = subprocess.run(  # noqa: S603
        [
            "/bin/sh",
            "-c",
            f'{match.group(0)}\n{ENSURE} "$1"',
            "sh",
            group,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return proc, log
