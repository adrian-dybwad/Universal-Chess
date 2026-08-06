"""Tests for what makes bluez-selfheal decide to rebuild bluetoothd at all.

Why these tests exist
---------------------
``bluez-selfheal`` substitutes a locally rebuilt ``bluetoothd`` when the stock
one cannot register an LE advertisement. Its only evidence is
``bluez-advertising-probe``, and it originally treated *any* non-zero probe exit
as "stock BlueZ is broken -- patch it". Two states are not that:

1. **No Bluetooth controller at all** (a plain Raspberry Pi Zero: no "W", no
   wireless die). Nothing can ever advertise, so there is nothing to heal. The
   old behaviour spent up to 45 minutes compiling bluetoothd from source -- with
   "Repairing Bluetooth advertising" on the panel -- then failed the re-probe and
   wrote a not-healthy marker, which makes the boot unit run the whole thing
   again on the next boot. Forever.
2. **The probe could not run** (exit 2: no D-Bus / bluetoothd not up yet). That
   is an inconclusive measurement, not a regression, so it must not authorize a
   rebuild either; it re-evaluates on the next run instead.

The rebuild only reproduces on-device (tens of minutes of compiling), so the
deterministic guards are the adapter gate and the probe-verdict mapping, both
exercised here through the script's own diagnostic subcommands and an
unprivileged ``run``/``boot`` (the gate deliberately sits before any state
directory, lock, or systemctl call, so it works as a normal user).
"""

import os
import subprocess
from pathlib import Path

import pytest

from universalchess.board.wireless_capability import SYSFS_BLUETOOTH

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "bluez-selfheal"

# Log fragments that mark how far the script got. The skip line proves the gate
# fired; the evaluate line is the first thing past it and is what leads to
# bluetooth restarts, the probe, and the compile.
_SKIP_LOG = "no Bluetooth controller"
_EVALUATE_LOG = "evaluating bluez"


def _run(*args, sysfs_bluetooth=None):
    """Run the helper with the sysfs Bluetooth path pointed at a test tree."""
    env = dict(os.environ)
    if sysfs_bluetooth is not None:
        env["UC_SYSFS_BLUETOOTH"] = str(sysfs_bluetooth)
    return subprocess.run(  # noqa: S603 - test invokes the pinned helper with fixed args
        ["bash", str(_HELPER), *args],  # noqa: S607 - bash on PATH is fine in tests
        env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def no_adapter(tmp_path):
    """An empty Bluetooth class directory: a board with no controller.

    Mirrors a plain Pi Zero, where the directory exists (the subsystem is built
    into the Pi kernel) but the kernel never registers an ``hciX`` device.
    """
    path = tmp_path / "bluetooth"
    path.mkdir()
    return path


@pytest.fixture
def with_adapter(tmp_path):
    """A Bluetooth class directory holding one controller (any W variant, or a dongle)."""
    path = tmp_path / "bluetooth"
    (path / "hci0").mkdir(parents=True)
    return path


# --------------------------------------------------------------------------- #
# Adapter presence
# --------------------------------------------------------------------------- #

def test_reports_no_adapter_when_class_dir_is_empty(no_adapter):
    # Why: the plain-Zero state. A regression that reports an adapter here is
    # what let the rebuild loop start, so this is the innermost guard.
    proc = _run("has-adapter", sysfs_bluetooth=no_adapter)
    assert proc.returncode != 0, proc.stderr


def test_reports_no_adapter_when_class_dir_is_absent(tmp_path):
    # Why: a kernel without the Bluetooth subsystem has no directory at all.
    # With ``set -u`` and an unexpanded glob this must still be a clean "absent",
    # not a script error that the caller might read as success.
    proc = _run("has-adapter", sysfs_bluetooth=tmp_path / "missing")
    assert proc.returncode != 0, proc.stderr


def test_reports_adapter_when_an_hci_device_exists(with_adapter):
    # Why: the W variants (and a USB dongle) must still be healed. A regression
    # here disables the self-heal everywhere, and BLE advertising silently stops
    # working on the next kernel that carries the length-validation change.
    proc = _run("has-adapter", sysfs_bluetooth=with_adapter)
    assert proc.returncode == 0, proc.stderr


def test_bash_and_python_agree_on_the_sysfs_path():
    # Why: the presence check is implemented twice -- in bash (the install-time
    # self-heal, which cannot import the package) and in Python (the menus, the
    # web payload, the startup gating). If the two paths drift, one surface hides
    # Bluetooth while the other keeps rebuilding bluetoothd. Manifests as this
    # literal disappearing from the script.
    assert f'UC_SYSFS_BLUETOOTH:-{SYSFS_BLUETOOTH}' in _HELPER.read_text()


# --------------------------------------------------------------------------- #
# The gate on the real entry points
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["run", "boot"])
def test_no_adapter_skips_the_heal_entirely(mode, no_adapter):
    # Why: both entry points must bail. ``run`` is the apt/install path and
    # ``boot`` is the safety net that re-runs whenever the last decision was not
    # healthy -- which is exactly the state a no-adapter board would be left in,
    # so an ungated ``boot`` alone recreates the every-boot rebuild.
    #
    # Manifests as either a non-zero exit (systemd would mark the unit failed on
    # a board that is simply not equipped) or the evaluate line appearing, which
    # means the script went on to restart bluetoothd and probe.
    proc = _run(mode, sysfs_bluetooth=no_adapter)
    assert proc.returncode == 0, proc.stderr
    assert _SKIP_LOG in proc.stderr
    assert _EVALUATE_LOG not in proc.stderr


def test_gate_runs_before_any_privileged_state(no_adapter, tmp_path):
    # Why: the gate has to be the first thing ``main`` does -- ahead of the state
    # directory, the lock file, and the systemctl calls -- otherwise a board with
    # no controller still pays for (and can fail on) all of that before deciding
    # there is nothing to do. Running as an unprivileged user with no write
    # access to /var/lib is the observable proxy: the skip must be reached with
    # no lock contention message and no marker written.
    proc = _run("run", sysfs_bluetooth=no_adapter)
    assert _SKIP_LOG in proc.stderr
    assert "holds the lock" not in proc.stderr
    assert "marker:" not in proc.stderr


# --------------------------------------------------------------------------- #
# Probe verdict: an unusable probe is not a broken BlueZ
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "exit_code, verdict",
    [
        (0, "pass"),    # advertising registered: stock is fine, retire any patch
        (1, "fail"),    # BlueZ rejected the advert: the regression, patch it
        (2, "error"),   # probe could not run (no bus / helper missing)
        (127, "error"),  # any other exit is equally inconclusive
    ],
)
def test_probe_exit_codes_map_to_distinct_verdicts(exit_code, verdict):
    # Why: exit 2 used to be indistinguishable from exit 1, so an inconclusive
    # measurement authorized a 45-minute rebuild. Manifests as "error" collapsing
    # into "fail" (rebuild on any hiccup) or into "pass" (a real regression
    # silently left unhealed).
    proc = _run("probe-verdict", str(exit_code))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == verdict


def test_only_a_failed_probe_triggers_a_build():
    # Why: the decision table is the thing that must not drift -- the build is
    # only reachable from the "fail" verdict. Asserted on the recipe text because
    # reaching the build in a test would compile bluetoothd. Manifests as the
    # build being reachable from "error" again.
    content = _HELPER.read_text()
    assert 'verdict="$(probe_verdict' in content
    assert 'error)' in content
