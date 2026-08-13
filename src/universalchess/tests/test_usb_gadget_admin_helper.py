"""Tests for the uc-usb-gadget-admin root helper (scripts/uc-usb-gadget-admin).

This pinned passwordless-sudo helper is the only way the unprivileged service
can change USB Ethernet gadget mode: off, auto, client, or shared. Its security
value is the verb ``case`` and argument validation gating the privileged calls,
so the tests exercise that boundary in DRY_RUN mode, which records the intended
invocation instead of running it.

Current ``rpi-usb-gadget`` (trixie) only has ``on|off|toggle|status|help`` -- no
``shared`` verb -- and ``on`` brings Shared up with the ICS watcher enabled.
UC's Client/Shared preference must therefore pin the matching NetworkManager
profile and disable the ICS auto-switcher after ``on -f``; ``auto`` is the
opposite, restoring that vendor arrangement so the watcher chooses.

Each test states the regression it guards and how it would surface.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "uc-usb-gadget-admin"
# Absolute interpreter so the run does not depend on PATH resolution.
_BASH = shutil.which("bash") or "/bin/bash"

CLIENT_CONN = "USB Gadget (client)"
SHARED_CONN = "USB Gadget (shared)"
ICS_UNIT = "rpi-usb-gadget-ics.service"
NETPLAN_ETH0_CONN = "netplan-eth0"


def _run(args, action_log, *, dry_run="1", extra_env=None):
    env = dict(os.environ)
    env["UC_USB_GADGET_ADMIN_DRY_RUN"] = dry_run
    env["UC_USB_GADGET_ADMIN_ACTION_LOG"] = str(action_log)
    if extra_env:
        env.update(extra_env)
    argv = [_BASH, str(_HELPER), *args]
    # Fixed argv (no shell) running the repo's own helper under bash; test-only.
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    lines = action_log.read_text().splitlines() if action_log.exists() else []
    return proc, lines


def test_client_verb_enables_gadget_then_pins_client_profile(tmp_path):
    """``client`` runs ``on -f``, disables ICS auto-switch, and pins Client.

    Why: vendor ``rpi-usb-gadget on`` brings Shared up (Shared autoconnect yes,
    Client no) and enables ``rpi-usb-gadget-ics.service``, which leaves Shared
    active when the host is not offering ICS -- exactly the Client-then-reboot
    comes-back-Shared failure. Failure: action log is only ``on -f``, or never
    downs Shared / ups Client / disables the ICS unit.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["client"], log)
    assert proc.returncode == 0
    assert lines[0] == "rpi-usb-gadget on -f"
    assert f"systemctl disable --now {ICS_UNIT}" in lines
    assert f"nmcli connection modify {CLIENT_CONN} connection.autoconnect yes" in lines
    assert f"nmcli connection modify {SHARED_CONN} connection.autoconnect no" in lines
    assert f"nmcli connection down {SHARED_CONN}" in lines
    assert f"nmcli connection up {CLIENT_CONN}" in lines
    assert "pin-netplan-eth0-off-usb0" in lines
    assert f"nmcli connection down {NETPLAN_ETH0_CONN}" in lines
    assert "ensure-early-g-ether-cmdline" in lines
    assert not any(line.startswith("rpi-usb-gadget shared") for line in lines)


def test_shared_verb_enables_gadget_then_pins_shared_profile(tmp_path):
    """``shared`` does not call a nonexistent ``rpi-usb-gadget shared`` verb.

    Current packages only document on|off|toggle|status|help. Failure: helper
    exits 1 after ``rpi-usb-gadget shared``, or leaves Client pinned / ICS on so
    a later ICS detection flips the link.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["shared"], log)
    assert proc.returncode == 0
    assert lines[0] == "rpi-usb-gadget on -f"
    assert f"systemctl disable --now {ICS_UNIT}" in lines
    assert f"nmcli connection modify {SHARED_CONN} connection.autoconnect yes" in lines
    assert f"nmcli connection modify {CLIENT_CONN} connection.autoconnect no" in lines
    assert f"nmcli connection down {CLIENT_CONN}" in lines
    assert f"nmcli connection up {SHARED_CONN}" in lines
    assert "ensure-early-g-ether-cmdline" in lines
    assert not any(line == "rpi-usb-gadget shared" for line in lines)


def test_auto_verb_enables_the_gadget_then_hands_it_to_the_vendor_switcher(tmp_path):
    """``auto`` enables the ICS unit and restores the autoconnect it expects.

    Why: Client/Shared apply by disabling ``rpi-usb-gadget-ics.service`` and
    pinning one profile, so Auto has to undo both halves. Enabling the unit while
    leaving the previous pin in place is neither mode -- the watcher would fight a
    profile pinned against it -- so Auto restores what a fresh ``on -f`` leaves:
    Shared autoconnect yes, Client no.

    Failure: the action log still disables the ICS unit, never enables it, or
    leaves Client pinned to autoconnect yes -- the board then stays in whichever
    mode it was pinned to and never switches.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["auto"], log)
    assert proc.returncode == 0
    assert lines[0] == "rpi-usb-gadget on -f"
    assert f"systemctl enable --now {ICS_UNIT}" in lines
    assert f"systemctl disable --now {ICS_UNIT}" not in lines
    assert f"nmcli connection modify {SHARED_CONN} connection.autoconnect yes" in lines
    assert f"nmcli connection modify {CLIENT_CONN} connection.autoconnect no" in lines
    assert "ensure-early-g-ether-cmdline" in lines
    assert "pin-netplan-eth0-off-usb0" in lines


def test_auto_verb_never_forces_a_profile_up_or_down(tmp_path):
    """Auto leaves the choice of live mode to the switcher.

    Why: the unit decides Client or Shared from what the host offers. Bringing
    one up here races it, and downing the active profile would drop a working USB
    session the moment the user selects Auto -- which is how they are most likely
    to be connected while changing this setting.

    Failure: any ``nmcli connection up``/``down`` of a gadget profile is recorded.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["auto"], log)
    assert proc.returncode == 0
    moved = [
        line
        for line in lines
        if line.startswith(("nmcli connection up ", "nmcli connection down "))
        and (CLIENT_CONN in line or SHARED_CONN in line)
    ]
    assert moved == [], f"auto moved gadget profiles: {moved}"


def test_every_on_mode_pins_netplan_eth0_off_usb0(tmp_path):
    """Generic eth0 netplan must not match usb0 (DHCP-client vs Shared server).

    Why: stock ``netplan-eth0`` uses empty match and steals usb0 when Shared
    drops, turning the Pi into a DHCP client on the gadget link. Auto needs this
    as much as the pinned modes, since the switcher can land on Shared. Failure:
    an apply never pins ``connection.interface-name eth0`` on netplan-eth0.
    """
    for mode in ("shared", "client", "auto"):
        log = tmp_path / f"actions-{mode}.log"
        proc, lines = _run([mode], log)
        assert proc.returncode == 0
        assert "pin-netplan-eth0-off-usb0" in lines
        assert f"nmcli connection down {NETPLAN_ETH0_CONN}" in lines
        assert "pin-netplan-eth0-match-name eth0" not in lines
        assert "ensure-early-g-ether-cmdline" in lines


def test_no_verb_ever_touches_the_gadget_driver(tmp_path):
    """No mode may load, unload, or rebind the USB gadget driver.

    Why: the gadget is armed once at boot and stays armed; a host enumerates it
    on insertion because the pull-up is already asserted. Cycling ``g_ether`` or
    the ``dwc2`` binding underneath a running controller leaves it wedged, and
    the board then enumerates nothing until its power is cut.

    Failure: a ``modprobe`` or driver bind/unbind write reaches the action log,
    which on the board is a cable insertion that produces no device at all.
    """
    forbidden = ("modprobe", "/sys/bus/platform/drivers", "soft_connect")
    for mode in ("off", "auto", "client", "shared"):
        log = tmp_path / f"actions-{mode}.log"
        proc, lines = _run([mode], log)
        assert proc.returncode == 0
        offending = [line for line in lines if any(word in line for word in forbidden)]
        assert offending == [], f"{mode} touches the driver: {offending}"


def test_off_verb_disables_the_gadget_and_touches_nothing_else(tmp_path):
    """``off`` runs ``rpi-usb-gadget off`` and nothing more.

    Why: Off is the user asking for the gadget to go away, so it must not also
    rewrite the cmdline or move NetworkManager profiles around -- a board turning
    the gadget off would silently keep boot-time changes it never asked for.

    Failure: the action log is empty (off never ran) or carries extra entries.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["off"], log)
    assert proc.returncode == 0
    assert lines == ["rpi-usb-gadget off"]


@pytest.mark.parametrize(
    "bad",
    [
        "on",
        "true",
        "1",
        "client; rm -rf /",
        "client shared",
        "../off",
        "",
        "reconnect",
        "reload-detached",
        "refresh-profile",
    ],
)
def test_rejects_anything_but_exact_mode_verbs(tmp_path, bad):
    """Only off, auto, client and shared are accepted (exit 2 otherwise), no call.

    This is the injection boundary for the sudo grant. Manifests as a non-empty
    action log or a zero exit for a bad token.
    """
    log = tmp_path / "actions.log"
    args = [bad] if bad != "" else []
    proc, lines = _run(args, log)
    assert proc.returncode == 2
    assert lines == []

def test_missing_argument_is_usage_error(tmp_path):
    """No argument is a usage error (exit 2), no privileged call."""
    log = tmp_path / "actions.log"
    proc, lines = _run([], log)
    assert proc.returncode == 2
    assert lines == []


def test_extra_argument_is_rejected(tmp_path):
    """A trailing token is rejected so the grant cannot become a passthrough.

    Failure: ``client -f`` or ``client foo`` reaches the action log.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["client", "extra"], log)
    assert proc.returncode == 2
    assert lines == []


def test_postinst_installs_a_sudo_grant_for_this_helper():
    """The package must grant passwordless sudo to exactly this helper path.

    Without the grant every apply returns "not applied" and the System select
    looks like it does not stick. The path is asserted whole because a grant for
    a different or misspelled path is equivalent to no grant at all.
    """
    postinst = (
        Path(__file__).resolve().parents[3]
        / "packaging" / "deb-root" / "DEBIAN" / "postinst"
    )
    text = postinst.read_text(encoding="utf-8")
    assert 'USB_GADGET_ADMIN_HELPER="${DGTCM_PATH}/scripts/uc-usb-gadget-admin"' in text
    assert "NOPASSWD: $USB_GADGET_ADMIN_HELPER" in text
