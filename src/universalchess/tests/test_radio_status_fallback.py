"""Wi-Fi status must survive whichever of ``iw`` / wireless-tools is installed.

Why these tests exist
---------------------
Moving status off wireless-tools fixed Armbian (which has only ``iw``) and
introduced the mirror-image defect on Raspberry Pi OS. ``iw`` is not a declared
dependency of this package, so an ``iw``-only probe reports an associated board
as disconnected on any image without it -- the header indicator and the e-paper
menu both go to "not connected" while the board is on the network.

``wifi_enabled`` had two further defects from the same move: it answered False
when nothing could be determined (a board with no rfkill entry for wlan is not a
board with the radio switched off), and it looked only at ``soft blocked``, so a
hard-blocked radio was reported as enabled and usable.

How a regression manifests
--------------------------
- Fallback dropped: ``link_status`` returns connected=False on a Pi whose
  ``iwconfig`` shows an ESSID, and the header Wi-Fi icon shows disconnected.
- Fail-closed restored: ``wifi_enabled`` returns False on a board with no wlan
  rfkill entry, and the UI offers to "enable Wi-Fi" that is already on.
- Hard block ignored: a hard-blocked radio reads as enabled, so status shows
  disconnected-but-enabled and every connect attempt fails.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from universalchess.board.wireless_capability import WirelessCapability
from universalchess.connectivity import radio as wifi_radio
from universalchess.services.system import SystemPollingService
from universalchess.state import get_system
from universalchess.state.system import (
    WIFI_CONNECTED,
    WIFI_DISCONNECTED,
)

IW_LINK_CONNECTED = """Connected to 8a:8c:b5:69:97:82 (on wlan0)
\tSSID: DISPLAY
\tfreq: 5745.0
\tsignal: -52 dBm
"""

# iw builds that omit signal/freq for the associated BSS; the band and bars have
# to come from somewhere else.
IW_LINK_SSID_ONLY = """Connected to 8a:8c:b5:69:97:82 (on wlan0)
\tSSID: DISPLAY
"""

IWCONFIG_CONNECTED = """wlan0     IEEE 802.11  ESSID:"DISPLAY"
          Mode:Managed  Frequency:2.437 GHz  Access Point: 8A:8C:B5:69:97:82
          Bit Rate=72.2 Mb/s   Tx-Power=31 dBm
          Link Quality=57/70  Signal level=-53 dBm
"""

IWCONFIG_IDLE = """wlan0     IEEE 802.11  ESSID:off/any
          Mode:Managed  Access Point: Not-Associated   Tx-Power=31 dBm
"""

SIGNAL_DBM = -53
SIGNAL_DBM_IW = -52


@contextmanager
def _tools(**stdout_by_tool):
    """Present exactly the named tools to radio, and yield the exec mock.

    ``stdout_by_tool`` maps a tool name to the stdout it produces; any tool not
    listed is absent from the image, which is the condition these tests are
    about. Yielding the mock lets a test assert which binaries were spawned,
    not just what the result was.
    """
    def fake_find_tool(name):
        return f"/usr/sbin/{name}" if name in stdout_by_tool else None

    def fake_run(argv, **_kwargs):
        name = argv[0].rsplit("/", 1)[-1]
        return MagicMock(returncode=0, stdout=stdout_by_tool[name], stderr="")

    run = MagicMock(side_effect=fake_run)
    with patch.object(wifi_radio, "find_tool", side_effect=fake_find_tool), \
         patch.object(wifi_radio.subprocess, "run", run):
        yield run


def _spawned(run):
    """Tool names passed to the exec mock, in order."""
    return [call.args[0][0].rsplit("/", 1)[-1] for call in run.call_args_list]


def test_parse_iwconfig_reads_ssid_signal_and_band():
    # Why: wireless-tools is the only source on an image without iw. Manifests
    # as connected=False with an empty SSID while iwconfig shows DISPLAY.
    parsed = wifi_radio.parse_iwconfig(IWCONFIG_CONNECTED)
    assert parsed["connected"] is True
    assert parsed["ssid"] == "DISPLAY"
    assert parsed["signal_dbm"] == SIGNAL_DBM
    assert parsed["frequency"] == "2.4 GHz"


def test_parse_iwconfig_treats_not_associated_as_disconnected():
    # Why: iwconfig prints a wlan0 block whether or not it is associated, so
    # presence of output is not presence of a link. Manifests as the UI keeping
    # the last SSID (or showing "off/any") after a disconnect.
    parsed = wifi_radio.parse_iwconfig(IWCONFIG_IDLE)
    assert parsed == {
        "connected": False,
        "ssid": "",
        "signal_dbm": None,
        "frequency": "",
    }


def test_link_status_uses_iw_when_it_answers_fully():
    # Why: wireless-tools must stay a fallback, not an extra process on every
    # 10-second poll. Manifests as iwconfig being spawned on Armbian, where the
    # binary does not exist, once per poll.
    with _tools(iw=IW_LINK_CONNECTED, iwconfig=IWCONFIG_CONNECTED) as run:
        status = wifi_radio.link_status()
    assert status["ssid"] == "DISPLAY"
    assert status["signal_dbm"] == SIGNAL_DBM_IW
    assert status["frequency"] == "5 GHz"
    assert _spawned(run) == ["iw"]


def test_link_status_falls_back_to_iwconfig_when_iw_is_absent():
    # Why: the Raspberry Pi regression this file exists for. iw is not a
    # package dependency; without a fallback an associated Pi reads as
    # disconnected. Manifests as connected=False and no SSID.
    with _tools(iwconfig=IWCONFIG_CONNECTED):
        status = wifi_radio.link_status()
    assert status["connected"] is True
    assert status["ssid"] == "DISPLAY"
    assert status["signal_dbm"] == SIGNAL_DBM
    assert status["frequency"] == "2.4 GHz"


def test_link_status_fills_missing_signal_and_band_from_iwconfig():
    # Why: an iw build that reports the SSID but no signal/freq would otherwise
    # leave the bars at zero and the band blank on a strong connection.
    # Manifests as a connected network drawn with an empty signal indicator.
    with _tools(iw=IW_LINK_SSID_ONLY, iwconfig=IWCONFIG_CONNECTED):
        status = wifi_radio.link_status()
    assert status["ssid"] == "DISPLAY"
    assert status["signal_dbm"] == SIGNAL_DBM
    assert status["frequency"] == "2.4 GHz"


def test_link_status_falls_back_to_iwgetid_as_a_last_resort():
    # Why: iwgetid answers on images where iwconfig output is unparseable but
    # the association is real. SSID alone is still enough to show connected.
    # Manifests as "not connected" on a board that has an SSID.
    with _tools(iwgetid="DISPLAY\n"):
        status = wifi_radio.link_status()
    assert status["connected"] is True
    assert status["ssid"] == "DISPLAY"
    assert status["signal_dbm"] is None


def test_link_status_reports_disconnected_when_no_tool_answers():
    # Why: absent tools must not fabricate a connection. Manifests as the UI
    # claiming a link on a board with no wireless tooling at all.
    with _tools():
        status = wifi_radio.link_status()
    assert status["connected"] is False
    assert status["ssid"] == ""


def _rfkill_entry(root, name, kind, soft, hard=None):
    entry = root / name
    entry.mkdir()
    (entry / "type").write_text(f"{kind}\n")
    (entry / "soft").write_text(f"{soft}\n")
    if hard is not None:
        (entry / "hard").write_text(f"{hard}\n")
    return entry


def test_wifi_enabled_reports_hard_blocked_radio_as_disabled(tmp_path):
    # Why: a hard-blocked radio cannot associate, so calling it enabled sends
    # the user to a scan that can never succeed. Only "soft blocked" was
    # checked. Manifests as Wi-Fi shown on, with every connect timing out.
    _rfkill_entry(tmp_path, "rfkill0", "wlan", soft=0, hard=1)
    with patch.object(wifi_radio, "find_tool", return_value=None):
        assert wifi_radio.wifi_enabled(sysfs_rfkill=str(tmp_path)) is False


def test_wifi_enabled_reports_unblocked_radio_as_enabled(tmp_path):
    # Why: the ordinary case must stay true once hard-block checking is added.
    # Manifests as Wi-Fi permanently shown as disabled.
    _rfkill_entry(tmp_path, "rfkill0", "wlan", soft=0, hard=0)
    with patch.object(wifi_radio, "find_tool", return_value=None):
        assert wifi_radio.wifi_enabled(sysfs_rfkill=str(tmp_path)) is True


def test_wifi_enabled_ignores_entries_for_other_radios(tmp_path):
    # Why: a soft-blocked Bluetooth entry sorts before the wlan one and must
    # not decide the answer. Manifests as Wi-Fi reported disabled whenever
    # Bluetooth is turned off.
    _rfkill_entry(tmp_path, "rfkill0", "bluetooth", soft=1)
    _rfkill_entry(tmp_path, "rfkill1", "wlan", soft=0)
    with patch.object(wifi_radio, "find_tool", return_value=None):
        assert wifi_radio.wifi_enabled(sysfs_rfkill=str(tmp_path)) is True


@pytest.mark.parametrize(
    "scenario",
    ["missing_sysfs", "no_wlan_entry"],
)
def test_wifi_enabled_assumes_enabled_when_undetermined(tmp_path, scenario):
    # Why: this returned False, which is a claim the radio is switched off --
    # something neither "rfkill is not installed" nor "this board exposes no
    # rfkill switch for wlan" says. Whether a radio exists is decided by
    # wireless_capability, not here. Manifests as a board that is on the
    # network showing Wi-Fi disabled, with the enable action doing nothing.
    root = tmp_path / "rfkill"
    if scenario == "no_wlan_entry":
        root.mkdir()
        _rfkill_entry(root, "rfkill0", "bluetooth", soft=0)
    with patch.object(wifi_radio, "find_tool", return_value=None):
        assert wifi_radio.wifi_enabled(sysfs_rfkill=str(root)) is True


def test_wifi_enabled_uses_rfkill_hard_block_line():
    # Why: `rfkill list wifi` is preferred when present, and its hard-block
    # line needs the same treatment as the sysfs one. Manifests as a
    # hard-blocked Pi radio reported enabled.
    output = "0: phy0: Wireless LAN\n\tSoft blocked: no\n\tHard blocked: yes\n"
    with _tools(rfkill=output):
        assert wifi_radio.wifi_enabled() is False


@pytest.fixture
def system_state():
    """The process-wide SystemState, reset around the test.

    The polling service publishes into the singleton, so a verdict left behind
    here would leak into unrelated tests that read the same object.
    """
    current = get_system()
    current.set_wifi(WIFI_DISCONNECTED, 0, None)
    yield current
    current.set_wifi(WIFI_DISCONNECTED, 0, None)


def test_status_bar_shows_the_link_on_a_wireless_tools_only_image(system_state):
    # Why: the regression at the level a user sees it. With only wireless-tools
    # installed -- no iw, no rfkill on PATH -- the header indicator must show
    # the network, not "disconnected". Both halves of the earlier defect are
    # exercised: wifi_enabled with nothing to read (which used to answer False
    # and short-circuit to WIFI_DISABLED) and the iw-only link probe.
    #
    # Manifests as wifi_state being WIFI_DISABLED or WIFI_DISCONNECTED, and the
    # SSID missing from the status bar, on a board that is on the network.
    service = SystemPollingService(
        capability=WirelessCapability(
            has_wifi=True, has_bluetooth=False, pi_model="Raspberry Pi 4 Model B"
        )
    )
    with _tools(iwconfig=IWCONFIG_CONNECTED):
        service._poll_wifi()

    assert system_state.wifi_state == WIFI_CONNECTED
    assert system_state.wifi_ssid == "DISPLAY"
    # -53 dBm maps to 61%, which is the middle of the three signal bands.
    assert system_state.wifi_signal_strength == 2
