"""Tests for the NetworkManager drop-in that keeps usb0 managed.

NetworkManager ships ``/usr/lib/udev/rules.d/85-nm-unmanaged.rules``, whose
``ENV{DEVTYPE}=="gadget"`` rule marks every USB gadget interface unmanaged. On a
stock Raspberry Pi OS image the only thing overriding that is the generic
``netplan-eth0`` profile: its empty ``match: {}`` claims every ethernet device,
so netplan generates a ``managed=1`` stanza that happens to cover usb0.

``uc-usb-gadget-admin`` deletes ``netplan-eth0`` on purpose -- that same empty
match otherwise claims usb0 as a DHCP *client* and fights Shared, which serves
DHCP. Deleting it leaves the udev rule unopposed, so usb0 reports ``STATE 10
(unmanaged)``, ``REASON 77 (unmanaged via udev rule)``: the cable enumerates,
the pinned profile never activates, and the host sees an adapter with no
address. The vendor tool's ``nmcli device set usb0 managed yes`` is runtime
state under ``/run`` and is wiped every boot, so only a drop-in in ``/etc``
holds across reboots.

Each test states the regression it guards and how it would surface.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEB_ROOT = _REPO_ROOT / "packaging" / "deb-root"
_DROP_IN = _DEB_ROOT / "etc" / "NetworkManager" / "conf.d" / "90-uc-usb-gadget-managed.conf"
_POSTINST = _DEB_ROOT / "DEBIAN" / "postinst"

_GADGET_IFACE = "usb0"


def _directives(text: str) -> dict[str, str]:
    """Return the ``key=value`` directives of an ini-style file, section aside."""
    directives: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        directives[key.strip()] = value.strip()
    return directives


def test_package_ships_a_drop_in_that_marks_the_gadget_managed():
    """The package must ship a NetworkManager drop-in claiming usb0.

    Why: without it the stock udev rule wins and usb0 is unmanaged from the
    first reboot after a Client/Shared apply, because that apply deletes the
    ``netplan-eth0`` profile that was implicitly managing it.

    Failure: the file is missing, so on the board the gadget enumerates and
    then sits at ``STATE 10 (unmanaged)`` with no carrier and no address.
    """
    assert _DROP_IN.is_file(), f"missing {_DROP_IN}"
    directives = _directives(_DROP_IN.read_text(encoding="utf-8"))
    assert directives.get("managed") == "1"
    assert directives.get("match-device") == f"interface-name:{_GADGET_IFACE}"


def test_drop_in_has_a_device_section_so_networkmanager_applies_it():
    """The directives must sit under a ``[device-...]`` section.

    Why: ``managed`` is only honoured inside a device section. The same two
    lines under ``[main]`` or with no section at all are silently ignored, which
    looks identical to the file being absent.

    Failure: no ``[device`` header, and the board still reports unmanaged after
    a reboot despite the file being installed.
    """
    text = _DROP_IN.read_text(encoding="utf-8")
    sections = [line.strip() for line in text.splitlines() if line.strip().startswith("[")]
    assert len(sections) == 1, f"expected exactly one section, got {sections}"
    assert sections[0].startswith("[device")


def test_drop_in_is_installed_under_etc_so_it_outranks_the_runtime_config():
    """The drop-in must live in ``/etc``, not ``/usr/lib``.

    Why: NetworkManager merges conf.d from ``/usr/lib``, ``/run`` and ``/etc``,
    with ``/etc`` highest. netplan writes its own generated config into
    ``/run/NetworkManager/conf.d``; a copy of ours in ``/usr/lib`` would lose to
    it whenever netplan has an opinion about the same device.

    Failure: the file is shipped from ``usr/lib`` instead, the setting is
    overridden at runtime, and the board still reports unmanaged -- a difference
    that only shows up where netplan is generating config, so the location is
    asserted against where the file actually is in the package rather than
    against a constant.
    """
    found = list(_DEB_ROOT.rglob(_DROP_IN.name))
    assert found == [_DROP_IN], f"expected exactly one drop-in at {_DROP_IN}, found {found}"
    assert found[0].relative_to(_DEB_ROOT).parts[:3] == ("etc", "NetworkManager", "conf.d")


def test_postinst_reloads_networkmanager_so_the_drop_in_applies_without_a_reboot():
    """Installing the drop-in must be followed by a NetworkManager reload.

    Why: NetworkManager reads conf.d at startup and on reload only. Without the
    reload the fix is inert until the next boot, so an upgrade on a running
    board appears not to have fixed anything -- the same symptom the drop-in
    exists to cure.

    Failure: no reload in postinst, and the gadget stays unmanaged until the
    user reboots for unrelated reasons.
    """
    text = _POSTINST.read_text(encoding="utf-8")
    assert "90-uc-usb-gadget-managed.conf" in text
    assert "nmcli general reload" in text or "systemctl reload NetworkManager" in text
