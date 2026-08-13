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


def _run(args, action_log, *, dry_run="1", fs_root=None, extra_env=None):
    """Run the helper in dry run, with every path it touches under ``fs_root``.

    A seam root is always set, even when a test does not care about files: the
    helper decides between deleting and re-matching netplan-eth0 by looking for
    ``/sys/class/net/eth0``, so without it the recorded actions would depend on
    whether the machine running the tests happens to have an eth0.
    """
    env = dict(os.environ)
    env["UC_USB_GADGET_ADMIN_DRY_RUN"] = dry_run
    env["UC_USB_GADGET_ADMIN_ACTION_LOG"] = str(action_log)
    env["UC_USB_GADGET_FS_ROOT"] = str(fs_root if fs_root is not None else action_log.parent / "fs")
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


@pytest.mark.parametrize("mode", ["shared", "client", "auto"])
def test_every_on_mode_pins_netplan_eth0_off_usb0(tmp_path, mode):
    """Generic eth0 netplan must not match usb0 (DHCP-client vs Shared server).

    Why: stock ``netplan-eth0`` uses empty match and steals usb0 when Shared
    drops, turning the Pi into a DHCP client on the gadget link. Auto needs this
    as much as the pinned modes, since the switcher can land on Shared.

    Failure: an apply never detaches the profile from usb0, so Shared loses the
    link to a DHCP client profile on the next drop.
    """
    log = tmp_path / f"actions-{mode}.log"
    proc, lines = _run([mode], log)
    assert proc.returncode == 0
    assert "pin-netplan-eth0-off-usb0" in lines
    assert f"nmcli connection down {NETPLAN_ETH0_CONN}" in lines
    assert "ensure-early-g-ether-cmdline" in lines


@pytest.mark.parametrize(
    ("eth0_present", "expected", "forbidden"),
    [
        (
            True,
            f"nmcli connection modify {NETPLAN_ETH0_CONN} "
            "match.interface-name eth0 connection.interface-name eth0",
            f"nmcli connection delete {NETPLAN_ETH0_CONN}",
        ),
        (
            False,
            f"nmcli connection delete {NETPLAN_ETH0_CONN}",
            f"nmcli connection modify {NETPLAN_ETH0_CONN} "
            "match.interface-name eth0 connection.interface-name eth0",
        ),
    ],
)
def test_netplan_eth0_is_kept_where_there_is_an_eth0_to_keep_it_for(
    tmp_path, eth0_present, expected, forbidden
):
    """A board with real ethernet keeps the profile; one without loses it.

    Why: on a board that has eth0 the profile still has an interface to serve, so
    restricting its match is enough and deleting it would take that port off the
    network. On a Pi Zero (usb0 + wlan0 only) the same profile has nothing left to
    match except the gadget, which is the conflict being removed.

    How a regression shows: the branches swap -- a board with ethernet loses its
    DHCP profile, or a Pi Zero keeps a profile that goes on claiming usb0.
    """
    root = tmp_path / "fs"
    if eth0_present:
        (root / "sys" / "class" / "net" / "eth0").mkdir(parents=True)
    else:
        root.mkdir()
    _, lines = _run(["client"], tmp_path / "actions.log", fs_root=root)
    assert expected in lines
    assert forbidden not in lines


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


def test_off_verb_disables_the_gadget_and_undoes_the_boot_time_changes(tmp_path):
    """``off`` runs ``rpi-usb-gadget off`` then reverses what the on-modes did.

    Why: Off is the user withdrawing consent for the feature. The vendor tool
    removes its own overlay and modules-load.d entry, but the gadget modules this
    helper put on the kernel command line, the stock netplan-eth0 profile it
    moved aside, and the state-directory mode an older release widened are all
    ours -- a board "turned off" that keeps them is one the user cannot turn off.

    Failure: the action log is only ``rpi-usb-gadget off`` (residue left behind),
    or Off starts moving connections up and down, which would claim an interface
    the user just switched off.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["off"], log)
    assert proc.returncode == 0
    assert lines == [
        "rpi-usb-gadget off",
        "disarm-early-g-ether-cmdline",
        "restore-netplan-eth0",
        "ensure-nm-state-dir-private",
    ]


def test_off_undoes_the_command_line_arming_for_real(tmp_path):
    """Off removes the gadget modules from an armed command line.

    Why: this is the residue that survives a reboot, and it is invisible in the
    UI. The marker test above proves the step is invoked; this proves the file it
    is invoked on comes back clean, through the same code path the board runs.

    Failure: modules-load= still names dwc2/g_ether after Off, so boot keeps
    loading the gadget stack for a feature that is off.
    """
    root = tmp_path / "fs"
    cmdline = root / "boot" / "firmware" / "cmdline.txt"
    cmdline.parent.mkdir(parents=True)
    cmdline.write_text(
        "root=PARTUUID=aa-02 rootwait modules-load=dwc2,g_ether quiet\n",
        encoding="utf-8",
    )
    proc, _ = _run(["off"], tmp_path / "actions.log", fs_root=root)
    assert proc.returncode == 0
    text = cmdline.read_text(encoding="utf-8")
    assert "g_ether" not in text
    assert "dwc2" not in text
    assert "root=PARTUUID=aa-02" in text


def test_an_on_mode_arms_the_command_line_for_real(tmp_path):
    """Applying Client edits the actual command line under the seam.

    Why: the arming is what makes a host plugged in before boot enumerate on its
    first try. Asserting only the dry-run marker would keep passing if the tool
    it delegates to were called with the wrong path or verb.

    Failure: cmdline.txt is unchanged, or gains a second modules-load parameter.
    """
    root = tmp_path / "fs"
    cmdline = root / "boot" / "firmware" / "cmdline.txt"
    cmdline.parent.mkdir(parents=True)
    cmdline.write_text("root=PARTUUID=aa-02 rootwait quiet\n", encoding="utf-8")
    proc, _ = _run(["client"], tmp_path / "actions.log", fs_root=root)
    assert proc.returncode == 0
    tokens = cmdline.read_text(encoding="utf-8").split()
    assert [t for t in tokens if t.startswith("modules-load=")] == [
        "modules-load=dwc2,g_ether"
    ]


def test_a_command_line_it_refuses_to_edit_does_not_fail_the_mode(tmp_path):
    """A cmdline the file tool refuses leaves the mode applied and says so.

    Why: the early bind is an optimisation; the mode itself (profiles, switcher)
    applied. Failing the whole apply would leave the user unable to select a mode
    because of an unrelated boot file, while silently reporting success would
    claim a boot edit that did not happen.

    Failure: exit 1 from the helper (mode unusable), or no error logged, which is
    how a board ends up behaving differently from what the log says.
    """
    root = tmp_path / "fs"
    cmdline = root / "boot" / "firmware" / "cmdline.txt"
    cmdline.parent.mkdir(parents=True)
    # No root= parameter: the shape a truncated write leaves behind.
    cmdline.write_text("console=tty1 rootwait quiet\n", encoding="utf-8")
    before = cmdline.read_bytes()
    proc, lines = _run(["client"], tmp_path / "actions.log", fs_root=root)
    assert proc.returncode == 0
    assert cmdline.read_bytes() == before
    assert "ERROR" in proc.stderr
    assert f"nmcli connection up {CLIENT_CONN}" in lines


def test_the_helper_never_widens_the_networkmanager_state_directory(tmp_path):
    """No mode may make NetworkManager's state directory world-traversable.

    Why: up to 2.0.0 this helper ran ``chmod o+x /var/lib/NetworkManager`` so the
    unprivileged service could read the Shared lease file. That exposed
    NetworkManager's state to every local user to save one privileged read, which
    ``read-shared-leases`` now performs instead.

    Failure: an ``o+x``/``o+r`` chmod of that directory reappears in the helper,
    which no behavioural assertion would catch because the widening is invisible
    until someone goes looking for it.
    """
    source = _HELPER.read_text(encoding="utf-8")
    assert "chmod o+" not in source
    assert "o+x /var/lib/NetworkManager" not in source
    for mode in ("off", "auto", "client", "shared"):
        log = tmp_path / f"actions-{mode}.log"
        _, lines = _run([mode], log)
        assert "ensure-nm-state-dir-private" in lines
        assert not [line for line in lines if "chmod" in line]


def test_read_shared_leases_prints_the_lease_file(tmp_path):
    """``read-shared-leases`` is a privileged read of the Shared lease file.

    Why: a lease is the only signal that a host on the gadget link took an
    address instead of self-assigning one, and NetworkManager's state directory
    is 0700 root. The service asks for it through the grant it already has.

    Failure: nothing is printed (the web card's lease count goes permanently
    unknown), or the verb performs a privileged *action*, which would put a
    status read inside the mode-changing boundary.
    """
    root = tmp_path / "fs"
    lease_file = root / "var" / "lib" / "NetworkManager" / "dnsmasq-usb0.leases"
    lease_file.parent.mkdir(parents=True)
    lease_file.write_text(
        "1767139200 aa:bb:cc:dd:ee:ff 10.12.194.42 laptop *\n", encoding="utf-8"
    )
    proc, lines = _run(["read-shared-leases"], tmp_path / "actions.log", fs_root=root)
    assert proc.returncode == 0
    assert "10.12.194.42" in proc.stdout
    assert lines == []


def test_read_shared_leases_with_no_lease_file_is_empty_and_successful(tmp_path):
    """No lease file reads as no leases, not as an error.

    Why: the file only exists once Shared has served a lease, so its absence is
    the normal state in every other mode. Reporting failure would make the
    caller show "unknown" where zero is the truth.

    Failure: non-zero exit or output on stdout, either of which the caller turns
    into an unknown lease count.
    """
    root = tmp_path / "fs"
    proc, _ = _run(["read-shared-leases"], tmp_path / "actions.log", fs_root=root)
    assert proc.returncode == 0
    assert proc.stdout == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_a_lease_file_that_cannot_be_read_fails_instead_of_printing_nothing(tmp_path):
    """An unreadable lease file exits non-zero rather than looking empty.

    Why: the caller turns empty output into a count of zero, which means "Shared
    is running and no host has taken an address" -- a diagnosis. Reporting that
    for a file this board could not read would invent an observation, and zero is
    exactly the state the user is asked to act on.

    Failure: exit 0 with no output, and the web card claims no leases whenever the
    read is denied.
    """
    root = tmp_path / "fs"
    lease_file = root / "var" / "lib" / "NetworkManager" / "dnsmasq-usb0.leases"
    lease_file.parent.mkdir(parents=True)
    lease_file.write_text("1767139200 aa:bb:cc:dd:ee:ff 10.12.194.42 laptop *\n")
    lease_file.chmod(0o000)
    proc, _ = _run(["read-shared-leases"], tmp_path / "actions.log", fs_root=root)
    assert proc.returncode != 0
    assert proc.stdout == ""


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
