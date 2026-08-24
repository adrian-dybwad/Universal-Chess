"""postinst must not fail when Pi-only groups are absent.

Why these tests exist:
    Raspberry Pi OS ships a ``gpio`` group (and often ``kmem``) that the
    e-paper stack is added to. Armbian on the Orange Pi Zero 2W has neither;
    ``usermod -aG gpio`` exits 6 under ``set -e`` and leaves the package
    unpacked but unconfigured. The helper must skip missing groups so
    Bluetooth/input grants still apply.

How a regression manifests:
    Installing the .deb on Armbian stops at ``usermod: group 'gpio' does
    not exist`` and ``dpkg --configure`` never finishes.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import universalchess.services.update_service as um

POSTINST = (
    Path(um.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "postinst"
)

HELPER = "add_primary_user_to_group_if_present"


def _run_helper(
    tmp_path: Path, present: set[str], group: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    text = POSTINST.read_text()
    match = re.search(rf"(?sm)^{HELPER}\(\) \{{.*?^}}", text)
    assert match, f"{HELPER} not found in postinst"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "usermod.log"
    present_list = " ".join(sorted(present))
    getent = bindir / "getent"
    getent.write_text(
        "#!/bin/sh\n"
        f'for g in {present_list}; do [ "$2" = "$g" ] && exit 0; done\n'
        "exit 2\n"
    )
    getent.chmod(0o755)
    usermod = bindir / "usermod"
    usermod.write_text(f"#!/bin/sh\necho \"$*\" >> '{log}'\n")
    usermod.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    proc = subprocess.run(  # noqa: S603
        [
            "/bin/sh",
            "-c",
            f'PRIMARY_USER=pa\n{match.group(0)}\n{HELPER} "$1"',
            "sh",
            group,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return proc, log


def test_missing_gpio_group_is_skipped_without_failing(tmp_path):
    # Why: measured on orangepizero2w — no gpio group. Manifests as
    # usermod exit 6 aborting postinst.
    proc, log = _run_helper(tmp_path, present={"input"}, group="gpio")
    assert proc.returncode == 0, proc.stderr
    assert not log.exists() or "gpio" not in log.read_text()


def test_present_group_still_adds_the_primary_user(tmp_path):
    # Why: Pi boards still need gpio membership. Manifests as skipping
    # usermod even when the group exists.
    proc, log = _run_helper(tmp_path, present={"gpio"}, group="gpio")
    assert proc.returncode == 0, proc.stderr
    assert log.exists()
    assert "gpio" in log.read_text()
    assert "pa" in log.read_text()
