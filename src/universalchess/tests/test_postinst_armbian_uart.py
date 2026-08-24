"""Tests for the postinst helper that frees the header UART on Armbian.

Why these tests exist:
    The DGT Centaur chess MCU is wired to the 40-pin header UART. On Raspberry
    Pi OS the postinst strips ``console=serial0`` from cmdline.txt. Armbian
    does not use cmdline.txt; it uses ``/boot/armbianEnv.txt``, and this
    image's ``boot.cmd`` still injects ``console=ttyS0,115200`` when
    ``console=display`` (it treats display like both). The measured working
    setting is ``console=none`` plus ``extraargs=console=tty1``. Without that,
    installing Universal Chess on the Orange Pi Zero 2W would leave getty and
    the kernel console on ttyS0, the same port the MCU uses.

How a regression manifests:
    - ``console=display`` surviving: kernel console stays on ttyS0 after reboot.
    - extraargs missing ``console=tty1``: no Linux console at all after
      ``console=none``.
    - A second run duplicating extraargs: bootargs grow every upgrade.
    - Writing dtoverlay/spidev into armbianEnv: stock H616 SPI overlays are
      the wrong header pins for the Centaur e-paper.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import universalchess.services.update_service as um

POSTINST = (
    Path(um.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "postinst"
)

HELPER = "configure_armbian_uart"


@pytest.fixture
def configure_armbian_uart(tmp_path):
    """Run the shipped helper against a temp armbianEnv.txt."""
    assert POSTINST.exists(), f"postinst missing: {POSTINST}"
    text = POSTINST.read_text()
    match = re.search(rf"(?sm)^{HELPER}\(\) \{{.*?^\}}", text)
    assert match, f"{HELPER} not found in postinst"
    source = match.group(0)
    counter = {"n": 0}

    def run(env_text: str) -> str:
        counter["n"] += 1
        target = tmp_path / f"armbianEnv{counter['n']}.txt"
        target.write_text(env_text)
        proc = subprocess.run(  # noqa: S603 - runs the postinst's own function
            ["/bin/sh", "-c", f'{source}\n{HELPER} "$1"', "sh", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return target.read_text()

    return run


STOCK = """verbosity=1
bootlogo=false
console=both
disp_mode=1920x1080p60
overlay_prefix=sun50i-h616
rootdev=UUID=e3393778-406f-4935-a4f1-58d4f7a05f78
rootfstype=ext4
usbstoragequirks=0x2537:0x1066:u,0x2537:0x1068:u
"""


def test_stock_both_becomes_none_with_tty1_extraargs(configure_armbian_uart):
    # Why: this is the file on the bring-up board. console=both puts ttyS0
    # on the kernel cmdline. A regression leaves console=both or omits tty1.
    result = configure_armbian_uart(STOCK)
    assert re.search(r"^console=none$", result, re.MULTILINE)
    assert not re.search(r"^console=both$", result, re.MULTILINE)
    assert "extraargs=console=tty1" in result
    assert "spidev" not in result
    assert "overlays=" not in result


def test_display_is_not_treated_as_safe(configure_armbian_uart):
    # Why: this image's boot.cmd maps console=display to ttyS0+tty1, same as
    # both. Using display as the "HDMI only" value would not free the UART.
    result = configure_armbian_uart(STOCK.replace("console=both", "console=display"))
    assert re.search(r"^console=none$", result, re.MULTILINE)
    assert "console=display" not in result


def test_existing_extraargs_keep_other_tokens(configure_armbian_uart):
    # Why: boards may already set extraargs (cgroup, etc.). Replacing the
    # whole line would drop those tokens. Manifests as extraargs=console=tty1
    # alone.
    src = STOCK + "extraargs=cgroup_enable=memory\n"
    result = configure_armbian_uart(src)
    line = next(row for row in result.splitlines() if row.startswith("extraargs="))
    assert "cgroup_enable=memory" in line
    assert "console=tty1" in line


def test_postinst_configures_vendor_orangepi_env_as_well_as_armbian_env():
    # Why: stock Orange Pi OS writes /boot/orangepiEnv.txt, not
    # armbianEnv.txt. A postinst that only touches Armbian's file leaves
    # vendor images with getty on ttyS0 and no spi-gpio overlay. Manifests
    # as orangepiEnv.txt absent from the shipped postinst.
    text = POSTINST.read_text()
    assert "/boot/orangepiEnv.txt" in text
    assert "/boot/armbianEnv.txt" in text


def test_second_run_does_not_duplicate_extraargs(configure_armbian_uart):
    # Why: postinst runs on every upgrade. Duplicating console=tty1 would
    # grow bootargs. Manifests as console=tty1 appearing twice.
    once = configure_armbian_uart(STOCK)
    twice = configure_armbian_uart(once)
    assert twice.count("console=tty1") == 1
    assert twice.count("console=none") == 1
