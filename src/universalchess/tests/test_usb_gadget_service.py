"""Tests for services/usb_gadget_service.py.

USB Ethernet gadget mode is a four-way preference (off / auto / client / shared)
that must reflect both what Universal Chess wants and what the OS is actually
doing, plus whether ``enable_usb_gadget.py`` prepared the boot partition. These
tests pin that contract:

  * prepared detection from config.txt / cmdline samples
  * live mode inference (usb0 + address / NM profile hooks)
  * seeding desired from the boot the card was prepared with
  * in_expected_state when desired matches live, including Auto, whose live mode
    is whichever concrete mode the vendor switcher currently holds
  * set() invokes the pinned helper argv and reports applied

Privileged work goes through ``uc-usb-gadget-admin`` via ``sudo -n``; the
command runner is injected so argv is asserted without root.
"""

from __future__ import annotations

import pytest

# Module under test -- red phase until implemented.
import universalchess.services.usb_gadget_service as ugs

_HELPER = "/opt/universalchess/scripts/uc-usb-gadget-admin"

PREPARED_CONFIG = """\
arm_64bit=1
# Universal Chess: USB Ethernet gadget
[all]
dtoverlay=dwc2,dr_mode=peripheral
"""

UNPREPARED_CONFIG = """\
arm_64bit=1
"""

PREPARED_CMDLINE = (
    "console=serial0,115200 root=PARTUUID=x rootwait "
    "modules-load=dwc2,g_ether\n"
)

UNPREPARED_CMDLINE = "console=serial0,115200 root=PARTUUID=x rootwait\n"


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _recording_runner(result=None):
    # Built per call rather than shared as a default instance, so one test's
    # runner can never observe another's result object.
    if result is None:
        result = _Result()
    calls = []

    def run(args, timeout):
        calls.append(list(args))
        return result if not callable(result) else result(args, timeout)

    return run, calls


@pytest.fixture(autouse=True)
def clear_cache():
    if hasattr(ugs, "invalidate_status_cache"):
        ugs.invalidate_status_cache()
    yield
    if hasattr(ugs, "invalidate_status_cache"):
        ugs.invalidate_status_cache()


def test_is_prepared_true_when_overlay_and_g_ether_present():
    """Boot prepared by enable_usb_gadget.py (or equivalent) is detected.

    Why: the System dropdown must show Client on a card the host script prepared
    even before UC has stored a preference. Failure: prepared stays False and
    the select defaults to Off.
    """
    assert ugs.is_prepared(config_txt=PREPARED_CONFIG, cmdline_txt=PREPARED_CMDLINE) is True


def test_is_prepared_false_when_boot_has_no_gadget():
    """An untouched boot partition is not prepared.

    Failure: a stock image reads as prepared and seeds desired=client wrongly.
    """
    assert ugs.is_prepared(config_txt=UNPREPARED_CONFIG, cmdline_txt=UNPREPARED_CMDLINE) is False


def test_is_prepared_requires_both_overlay_and_modules():
    """Overlay alone or modules alone is not enough.

    Partial prep leaves the gadget broken; treating it as prepared would lie.
    """
    assert ugs.is_prepared(config_txt=PREPARED_CONFIG, cmdline_txt=UNPREPARED_CMDLINE) is False
    assert ugs.is_prepared(config_txt=UNPREPARED_CONFIG, cmdline_txt=PREPARED_CMDLINE) is False


def test_is_prepared_true_when_g_ether_comes_from_modules_load_d():
    """rpi-usb-gadget writes ``/etc/modules-load.d/usb-gadget.conf``, not cmdline.

    Why: after ``on -f`` + reboot on current images, ``/proc/cmdline`` has no
    ``g_ether`` while the module still loads from modules-load.d. Treating that
    board as unprepared keeps ``reboot_required`` true forever and offers a
    Reboot button that cannot clear the mismatch. Failure: prepared stays False
    when overlay + modules-load.d g_ether are present.
    """
    assert (
        ugs.is_prepared(
            config_txt=PREPARED_CONFIG,
            cmdline_txt=UNPREPARED_CMDLINE,
            modules_load_txt="g_ether\n",
        )
        is True
    )
    assert (
        ugs.is_prepared(
            config_txt=PREPARED_CONFIG,
            cmdline_txt=UNPREPARED_CMDLINE,
            modules_load_txt="# empty\n",
        )
        is False
    )


def test_reconcile_applies_desired_when_live_differs(tmp_path):
    """Boot / startup re-applies desired mode when live does not match.

    Why: even after pinning NM autoconnect, a board that reboots into Shared
    while desired is Client must be corrected without another UI click.
    Failure: reconcile is a no-op and Client stays Shared across service start.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = client\n")
    calls: list[list[str]] = []

    def fake_set_mode(mode, **kwargs):
        calls.append([mode, kwargs.get("settings_path")])
        return True

    original = ugs.set_mode
    ugs.set_mode = fake_set_mode  # type: ignore[assignment]
    try:
        applied = ugs.reconcile_desired_mode(
            settings_path=ini,
            live="shared",
            prepared=True,
        )
    finally:
        ugs.set_mode = original  # type: ignore[assignment]

    assert applied is True
    assert calls == [["client", ini]]


def test_reconcile_skips_when_already_in_expected_state(tmp_path):
    """reconcile does not re-run the helper when desired already matches live.

    Failure: every web/board start bounces usb0 and drops the USB session.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = client\n")
    calls: list[str] = []

    def fake_set_mode(mode, **_kwargs):
        calls.append(mode)
        return True

    original = ugs.set_mode
    ugs.set_mode = fake_set_mode  # type: ignore[assignment]
    try:
        applied = ugs.reconcile_desired_mode(
            settings_path=ini,
            live="client",
            prepared=True,
        )
    finally:
        ugs.set_mode = original  # type: ignore[assignment]

    assert applied is False
    assert calls == []


def test_reconcile_leaves_auto_alone_whichever_mode_the_switcher_picked(tmp_path):
    """Auto must not be re-applied because the switcher chose Shared.

    Why: reconcile runs on every web/board start. Treating Shared as a mismatch
    for desired Auto would re-run the helper on each start and bounce usb0 --
    dropping the very USB session the user is browsing over. Failure: set_mode
    is called for a healthy Auto board.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = auto\n")
    calls: list[str] = []

    def fake_set_mode(mode, **_kwargs):
        calls.append(mode)
        return True

    original = ugs.set_mode
    ugs.set_mode = fake_set_mode  # type: ignore[assignment]
    try:
        applied = ugs.reconcile_desired_mode(
            settings_path=ini,
            live="shared",
            prepared=True,
            auto_switching=True,
        )
    finally:
        ugs.set_mode = original  # type: ignore[assignment]

    assert applied is False
    assert calls == []


def test_reconcile_reapplies_auto_when_the_switcher_is_no_longer_enabled(tmp_path):
    """Auto with the switcher disabled is re-applied so it switches again.

    Why: something else disabling the unit (an older pinned apply, a manual
    systemctl) leaves the board frozen in one mode while the widget says Auto.
    Failure: reconcile skips and Auto never recovers without a UI click.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = auto\n")
    calls: list[str] = []

    def fake_set_mode(mode, **_kwargs):
        calls.append(mode)
        return True

    original = ugs.set_mode
    ugs.set_mode = fake_set_mode  # type: ignore[assignment]
    try:
        applied = ugs.reconcile_desired_mode(
            settings_path=ini,
            live="client",
            prepared=True,
            auto_switching=False,
        )
    finally:
        ugs.set_mode = original  # type: ignore[assignment]

    assert applied is True
    assert calls == ["auto"]


def test_detect_live_off_when_usb0_absent():
    """No usb0 netdev means live mode is off.

    Failure: absent interface reported as client/shared or unknown when it is
    simply not there.
    """
    assert ugs.detect_live_mode(usb0_exists=False, usb0_ipv4=None, nm_active=None) == "off"


def test_detect_live_shared_from_fixed_address():
    """usb0 at 10.12.194.1 is shared mode (Pi serves DHCP).

    Failure: shared boards read as client and the expected-state line lies.
    """
    assert (
        ugs.detect_live_mode(usb0_exists=True, usb0_ipv4="10.12.194.1", nm_active=None)
        == "shared"
    )


def test_detect_live_client_when_usb0_has_other_address():
    """usb0 with a host-leased address is client mode.

    Failure: client link reported as shared or off.
    """
    assert (
        ugs.detect_live_mode(usb0_exists=True, usb0_ipv4="192.168.2.3", nm_active=None)
        == "client"
    )


def test_detect_live_prefers_nm_profile_name_when_present():
    """NetworkManager profile name wins over address heuristics.

    Failure: NM says shared but address heuristic returns client (or vice versa).
    """
    assert (
        ugs.detect_live_mode(
            usb0_exists=True,
            usb0_ipv4="192.168.2.3",
            nm_active="USB Gadget (shared)",
        )
        == "shared"
    )
    assert (
        ugs.detect_live_mode(
            usb0_exists=True,
            usb0_ipv4="10.12.194.1",
            nm_active="USB Gadget (client)",
        )
        == "client"
    )


def test_detect_live_off_when_usb0_lingers_without_profile_or_address():
    """After ``rpi-usb-gadget off``, usb0 can linger until reboot with no NM/IP.

    Why: treating that leftover netdev as Client makes Off look broken (Desired
    Off / Live Client / Match No) even though the vendor tool reports off and
    the link is unusable. Failure: bare usb0 still returns ``client``.
    """
    assert (
        ugs.detect_live_mode(
            usb0_exists=True,
            usb0_ipv4=None,
            nm_active=None,
            nm_profile_names=frozenset(),
        )
        == "off"
    )


def test_detect_live_client_when_usb0_up_with_profiles_but_no_address():
    """Configured Client with usb0 up and no DHCP yet is live Client, not Off.

    Why: after reboot into Client the UDC is often ``not attached`` until the
    host plugs in / shares ICS -- usb0 exists, profiles exist, no IPv4. The
    previous idle→off rule reported Desired Client / Live Off / Match No with
    no reboot button and looked like apply failed. Failure: returns ``off``.
    """
    assert (
        ugs.detect_live_mode(
            usb0_exists=True,
            usb0_ipv4=None,
            nm_active=None,
            nm_profile_names=frozenset(
                {ugs.CLIENT_CONN_NAME, ugs.SHARED_CONN_NAME},
            ),
        )
        == "client"
    )


@pytest.mark.parametrize(
    ("udc_state", "expected"),
    [
        (None, "none"),
        ("", "none"),
        ("attached", "attached"),
        ("not attached", "not_attached"),
        ("Not Attached", "not_attached"),
        ("suspended", "unknown"),
    ],
)
def test_detect_attachment_matrix(udc_state, expected):
    """Attachment is the UDC host-link state, not Desired/Live mode.

    Why: Client can Match while the cable has no host (not attached); without
    this field the status looks fine and the missing USB session is invisible.
    Failure: wrong token or inventing attached from a bare usb0.
    """
    assert ugs.detect_attachment(udc_state=udc_state) == expected


@pytest.mark.parametrize(
    ("attachment", "ipv4", "expected"),
    [
        ("attached", "192.168.2.3", "Connected\n192.168.2.3"),
        ("attached", None, "Connected"),
        ("not_attached", None, "Disconnected"),
        ("not_attached", "10.12.194.1", "Connected\n10.12.194.1"),
        ("unknown", "192.168.2.35", "Connected\n192.168.2.35"),
        ("none", None, "Disconnected"),
    ],
)
def test_format_epaper_status_connected_and_ip(attachment, ipv4, expected):
    """Any usb0 address is Connected; never Disconnected/No host with an IP.

    Why: users reach the board over that address -- calling it No host or
    Disconnected is false. Failure: status line denies the link while ipv4 is set.
    """
    assert ugs.format_epaper_status(attachment=attachment, ipv4=ipv4) == expected


@pytest.mark.parametrize(
    ("lease_txt", "expected"),
    [
        ("", 0),
        ("# comment only\n", 0),
        ("1234 aa:bb:cc:dd:ee:ff 10.12.194.3 host *\n", 1),
        (
            "1 aa:bb:cc:dd:ee:ff 10.12.194.3 a *\n"
            "2 11:22:33:44:55:66 10.12.194.4 b *\n",
            2,
        ),
    ],
)
def test_count_dhcp_leases(lease_txt, expected):
    """Shared DHCP health is the lease-file record count.

    Why: an empty lease file with dnsmasq running is how Shared looked healthy
    while the host sat on APIPA. Failure: blank/comments counted as leases, or
    real lines ignored.
    """
    assert ugs.count_dhcp_leases(lease_txt) == expected


def test_get_status_includes_attachment_from_udc_state(tmp_path):
    """get_status exposes attachment so the Connectivity card can render it.

    Failure: attachment missing or always none when udc_state is not attached.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = client\n")
    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=UNPREPARED_CMDLINE,
        modules_load_txt="g_ether\n",
        usb0_exists=True,
        usb0_ipv4=None,
        nm_active=None,
        nm_profile_names=frozenset({ugs.CLIENT_CONN_NAME}),
        udc_state="not attached",
        settings_path=ini,
    )
    assert status.attachment == "not_attached"
    assert status.live == "client"


def test_get_status_attachment_follows_client_ipv4_when_udc_lies(tmp_path):
    """A Client DHCP address forces attachment=attached even if UDC lags.

    Why: Client DHCP on the cable while sysfs still reads not attached/unknown
    made Host link and the e-paper line deny a session the user is using.
    Failure: attachment stays not_attached when a non-Shared ipv4 is set.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = client\n")
    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=UNPREPARED_CMDLINE,
        modules_load_txt="g_ether\n",
        usb0_exists=True,
        usb0_ipv4="192.168.2.35",
        nm_active="USB Gadget (client)",
        nm_profile_names=frozenset({ugs.CLIENT_CONN_NAME}),
        udc_state="not attached",
        settings_path=ini,
    )
    assert status.ipv4 == "192.168.2.35"
    assert status.attachment == "attached"
    assert ugs.format_epaper_status(
        attachment=status.attachment, ipv4=status.ipv4
    ) == "Connected\n192.168.2.35"


def test_get_status_shared_ipv4_does_not_force_attached(tmp_path):
    """Shared's fixed 10.12.194.1 must not mask UDC not-attached.

    Why: after unplug the Pi still has the Shared address; treating that as
    Connected hid a dead cable. Failure: attachment=attached with Shared IP
    while udc_state is not attached.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = shared\n")
    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=UNPREPARED_CMDLINE,
        modules_load_txt="g_ether\n",
        usb0_exists=True,
        usb0_ipv4=ugs.SHARED_IPV4,
        nm_active="USB Gadget (shared)",
        nm_profile_names=frozenset({ugs.SHARED_CONN_NAME}),
        udc_state="not attached",
        settings_path=ini,
    )
    assert status.ipv4 == ugs.SHARED_IPV4
    assert status.attachment == "not_attached"


def test_reboot_required_when_desired_off_but_usb0_still_present(tmp_path):
    """Off applied (boot markers cleared) still needs reboot while usb0 exists.

    Why: ``rpi-usb-gadget off`` removes the overlay and modules-load.d file, so
    ``prepared`` is False, but g_ether/usb0 stay loaded until reboot. Without
    this flag the UI hides Reboot and Match can read Yes while the netdev
    remains. Failure: reboot_required False despite usb0 after Off.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = off\n")
    status = ugs.get_status(
        config_txt=UNPREPARED_CONFIG,
        cmdline_txt=UNPREPARED_CMDLINE,
        modules_load_txt="",
        usb0_exists=True,
        usb0_ipv4=None,
        nm_active=None,
        settings_path=ini,
    )
    assert status.desired == "off"
    assert status.live == "off"
    assert status.prepared is False
    assert status.reboot_required is True
    assert status.in_expected_state is True


@pytest.mark.parametrize(
    ("desired", "live", "expected"),
    [
        ("off", "off", True),
        ("client", "client", True),
        ("shared", "shared", True),
        ("client", "off", False),
        ("off", "client", False),
        ("shared", "client", False),
        ("client", "unknown", False),
        # Auto has no live mode of its own: the vendor switcher holds either
        # Client or Shared, and both are the expected state for Auto.
        ("auto", "client", True),
        ("auto", "shared", True),
        ("auto", "off", False),
        ("auto", "unknown", False),
    ],
)
def test_in_expected_state_matrix(desired, live, expected):
    """in_expected_state is desired==live; unknown never counts as a match.

    Why: the UI status line must say when the board is not running the selected
    mode (e.g. after a failed apply or mid-reboot). Failure: mismatch reported
    as fine, or unknown treated as matching.
    """
    assert ugs.in_expected_state(desired=desired, live=live) is expected


def test_in_expected_state_auto_is_false_while_the_switcher_is_disabled():
    """Auto with the vendor switcher disabled is a pinned board, not Auto.

    Why: Auto is only Auto while ``rpi-usb-gadget-ics.service`` is enabled --
    that unit is the thing choosing Client or Shared. Judging Auto by live mode
    alone reports a match for a board pinned to Client with nothing switching.
    Failure: Match reads Yes on a board that can no longer change mode.
    """
    assert (
        ugs.in_expected_state(desired="auto", live="client", auto_switching=False)
        is False
    )
    assert (
        ugs.in_expected_state(desired="auto", live="client", auto_switching=True)
        is True
    )


def test_in_expected_state_pinned_mode_is_false_while_the_switcher_runs():
    """Client/Shared are pins; a running switcher can move them at any moment.

    Why: the pinned modes apply by disabling the switcher, so finding it enabled
    means the apply did not complete and the mode is about to change underneath
    the user. Failure: Match reads Yes and the mode silently flips later.
    """
    assert (
        ugs.in_expected_state(desired="client", live="client", auto_switching=True)
        is False
    )
    # Unknown (probe failed / unit is static) must not flip a healthy pin to No.
    assert (
        ugs.in_expected_state(desired="client", live="client", auto_switching=None)
        is True
    )


@pytest.mark.parametrize(
    ("is_enabled", "expected"),
    [
        ("enabled", True),
        ("enabled-runtime", True),
        ("disabled", False),
        ("masked", False),
        # ``static`` has no [Install] section, so neither enable nor disable
        # applies and the unit's state is not ours to claim either way.
        ("static", None),
        ("", None),
        (None, None),
    ],
)
def test_detect_auto_switching_matrix(is_enabled, expected):
    """``systemctl is-enabled`` maps to switcher on / off / not known.

    Why: this drives whether Auto is really Auto. Inventing False from a failed
    probe would show Match No on a working Auto board; inventing True would hide
    a switcher that never started. Failure: any state read as a definite answer.
    """
    assert ugs.detect_auto_switching(is_enabled=is_enabled) is expected


def test_get_status_seeds_desired_auto_when_the_card_left_the_switcher_on(tmp_path):
    """A card prepared with ``--auto`` reads Auto, not Client.

    Why: ``enable_usb_gadget.py --auto`` prepares the boot and leaves the vendor
    switcher enabled without pinning a profile. Seeding Client there shows a
    mismatch the user never caused, and the first status read would pin the
    board away from the mode the card was built for. Failure: desired seeds to
    client while the switcher is enabled.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\n")

    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=PREPARED_CMDLINE,
        usb0_exists=True,
        usb0_ipv4="192.168.2.3",
        nm_active=None,
        auto_switching=True,
        settings_path=ini,
    )
    assert status.desired == "auto"
    assert status.live == "client"
    assert status.auto_switching is True
    assert status.in_expected_state is True
    assert "auto" in ini.read_text()


def test_get_status_seeds_desired_client_when_the_switcher_is_off(tmp_path):
    """A pinned card (Client/Shared prep) still seeds Client.

    Failure: Auto is seeded on a card whose runcmds disabled the switcher, and
    the widget offers to hand a pinned board back to the vendor by itself.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\n")

    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=PREPARED_CMDLINE,
        usb0_exists=True,
        usb0_ipv4="192.168.2.3",
        nm_active=None,
        auto_switching=False,
        settings_path=ini,
    )
    assert status.desired == "client"


def test_reboot_required_when_auto_prepared_but_usb0_absent(tmp_path):
    """Auto selected from Off needs the same reboot as Client/Shared.

    Why: the helper writes the boot markers immediately, but usb0 only appears
    after reboot. Without this the card shows Desired Auto / Live Off / Match No
    and no way to finish. Failure: reboot_required False while usb0 is missing.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = auto\n")
    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=UNPREPARED_CMDLINE,
        modules_load_txt="g_ether\n",
        usb0_exists=False,
        usb0_ipv4=None,
        nm_active=None,
        auto_switching=True,
        settings_path=ini,
    )
    assert status.desired == "auto"
    assert status.live == "off"
    assert status.prepared is True
    assert status.reboot_required is True
    assert status.in_expected_state is False


def test_reboot_not_required_when_auto_holds_a_live_mode(tmp_path):
    """A working Auto link must not keep offering Reboot.

    Failure: reboot_required stays true for Auto whenever prepared, nagging on a
    board whose switcher already brought a mode up.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = auto\n")
    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=UNPREPARED_CMDLINE,
        modules_load_txt="g_ether\n",
        usb0_exists=True,
        usb0_ipv4=ugs.SHARED_IPV4,
        nm_active="USB Gadget (shared)",
        auto_switching=True,
        settings_path=ini,
    )
    assert status.live == "shared"
    assert status.in_expected_state is True
    assert status.reboot_required is False


def test_get_status_seeds_desired_client_when_prepared_and_unset(tmp_path):
    """Prepared boot + no stored preference => desired becomes client and persists.

    Why: enable_usb_gadget.py configures client mode; the dropdown must not read
    Off on that board. Failure: desired stays off/None and the select lies.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\n")

    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=PREPARED_CMDLINE,
        usb0_exists=True,
        usb0_ipv4="192.168.2.3",
        nm_active=None,
        settings_path=ini,
    )
    assert status.prepared is True
    assert status.desired == "client"
    assert status.live == "client"
    assert status.in_expected_state is True
    # Preference was written so the next read does not re-seed from heuristics.
    text = ini.read_text()
    assert "usb_gadget_mode" in text
    assert "client" in text


def test_get_status_keeps_stored_desired_even_when_live_differs(tmp_path):
    """A stored preference is the select value; mismatch sets in_expected_state false.

    Failure: live overwrites desired in the UI, hiding the user's choice.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = shared\n")

    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=PREPARED_CMDLINE,
        usb0_exists=True,
        usb0_ipv4="192.168.2.3",
        nm_active=None,
        settings_path=ini,
    )
    assert status.desired == "shared"
    assert status.live == "client"
    assert status.in_expected_state is False


def test_set_mode_client_invokes_helper_on_with_force(tmp_path):
    """set(client) runs sudo -n <helper> client and reports applied.

    Failure: wrong verb (shared/off) or missing -f equivalent means the vendor
    tool blocks or applies the wrong profile.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\n")
    run, calls = _recording_runner(_Result(returncode=0))

    applied = ugs.set_mode(
        "client",
        run=run,
        helper_path=_HELPER,
        settings_path=ini,
        prepared=True,
    )
    assert applied is True
    assert calls[0][:4] == ["sudo", "-n", _HELPER, "client"]
    assert "client" in ini.read_text()


def test_set_mode_auto_invokes_helper_auto(tmp_path):
    """set(auto) runs sudo -n <helper> auto and persists the preference.

    Failure: auto falls through to the client verb, which disables the very
    switcher Auto exists to run.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\n")
    run, calls = _recording_runner(_Result(returncode=0))

    applied = ugs.set_mode(
        "auto",
        run=run,
        helper_path=_HELPER,
        settings_path=ini,
        prepared=True,
    )
    assert applied is True
    assert calls[0][:4] == ["sudo", "-n", _HELPER, "auto"]
    assert "auto" in ini.read_text()


def test_set_mode_off_invokes_helper_off(tmp_path):
    """set(off) runs the off verb so the live gadget is torn down.

    Failure: off only writes ini and leaves usb0 up.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = client\n")
    run, calls = _recording_runner(_Result(returncode=0))

    applied = ugs.set_mode("off", run=run, helper_path=_HELPER, settings_path=ini, prepared=True)
    assert applied is True
    assert calls[0][:4] == ["sudo", "-n", _HELPER, "off"]
    assert "usb_gadget_mode=off" in ini.read_text().replace(" ", "")


def test_set_mode_reports_not_applied_when_helper_fails(tmp_path):
    """A failed privileged apply returns False rather than raising.

    Failure: 500 / exception on a hand-installed board without the sudo grant.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\n")
    run, _ = _recording_runner(_Result(returncode=1, stderr="a password is required"))

    applied = ugs.set_mode("shared", run=run, helper_path=_HELPER, settings_path=ini, prepared=True)
    assert applied is False


def test_set_mode_rejects_unknown_mode_without_calling_helper(tmp_path):
    """Junk modes never reach sudo.

    Failure: caller-controlled strings enter the privileged argv.
    """
    ini = tmp_path / "centaur.ini"
    run, calls = _recording_runner(_Result(returncode=0))
    with pytest.raises(ValueError, match="invalid usb gadget mode"):
        ugs.set_mode("bridge", run=run, helper_path=_HELPER, settings_path=ini, prepared=False)
    assert calls == []


def test_reboot_required_when_desired_off_but_still_prepared(tmp_path):
    """desired off while boot still loads g_ether needs a reboot to finish.

    Failure: UI never says reboot required and the gadget returns after reboot.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = off\n")
    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=PREPARED_CMDLINE,
        usb0_exists=False,
        usb0_ipv4=None,
        nm_active=None,
        settings_path=ini,
    )
    assert status.desired == "off"
    assert status.prepared is True
    assert status.reboot_required is True


def test_reboot_required_when_client_prepared_but_usb0_absent(tmp_path):
    """Client/Shared after Off writes boot markers; usb0 appears only after reboot.

    Why: ``rpi-usb-gadget on`` sets prepared True immediately and prints
    "Reboot to apply changes", but without usb0 the old rule
    (client and not prepared) hid Reboot now -- Desired Client / Live Off /
    Match No with no button. Failure: reboot_required False while prepared and
    usb0 missing.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = client\n")
    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=UNPREPARED_CMDLINE,
        modules_load_txt="g_ether\n",
        usb0_exists=False,
        usb0_ipv4=None,
        nm_active=None,
        settings_path=ini,
    )
    assert status.desired == "client"
    assert status.live == "off"
    assert status.prepared is True
    assert status.reboot_required is True
    assert status.in_expected_state is False


def test_reboot_not_required_when_client_live_matches(tmp_path):
    """A working Client link must not keep offering Reboot.

    Failure: reboot_required stays true whenever prepared, nagging forever.
    """
    ini = tmp_path / "centaur.ini"
    ini.write_text("[system]\nusb_gadget_mode = client\n")
    status = ugs.get_status(
        config_txt=PREPARED_CONFIG,
        cmdline_txt=UNPREPARED_CMDLINE,
        modules_load_txt="g_ether\n",
        usb0_exists=True,
        usb0_ipv4="192.168.2.3",
        nm_active="USB Gadget (client)",
        settings_path=ini,
    )
    assert status.in_expected_state is True
    assert status.reboot_required is False
