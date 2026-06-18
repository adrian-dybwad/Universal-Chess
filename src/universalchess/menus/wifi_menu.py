"""WiFi menu helpers (pure icon mapping).

The whole WiFi menu is now data-driven: the ``wifi`` catalog container declares
the status readout, Scan, and the enable toggle, and the ``wifi.networks``
container declares the scanned-network list as a dynamic, item-actioned list
(selecting a network runs the ``wifi_connect`` action with its SSID). The slow
scan, the password keyboard, and the live-status subscription are board side
effects wired in main's WiFi actions. This module keeps only the pure transforms
those actions reuse: the signal-bucket -> icon mappings (shared by the status
readout and the network rows so thresholds live in one place) and the
scan-results -> rows builder (so row construction is tested, not buried inline).
"""

from typing import List

from universalchess.menus.engine import MenuRow


def wifi_signal_icon(signal: int) -> str:
    """Return the e-paper icon id for a WiFi signal strength percentage.

    Pure bucket mapping (>=70 strong, >=40 medium, else weak) for an available
    network's row. Kept framework-free and shared with :func:`wifi_status_icon`
    so the thresholds cannot drift between the status readout and the scan list.
    """
    if signal >= 70:
        return "wifi_strong"
    if signal >= 40:
        return "wifi_medium"
    return "wifi_weak"


def wifi_status_icon(status: dict) -> str:
    """Return the e-paper icon id for a WiFi status dict.

    Maps the live status (enabled/connected/signal) to the icon shown in the WiFi
    menu's readout row: disabled and disconnected have their own icons, and a
    connected radio falls through to the shared signal-strength bucket. Matches
    the readout the deleted ``handle_wifi_settings_menu`` scaffold rendered, so
    the icon never silently changes meaning.
    """
    if not status.get("enabled"):
        return "wifi_disabled"
    if not status.get("connected"):
        return "wifi_disconnected"
    return wifi_signal_icon(status.get("signal", 0))


def wifi_network_rows(networks: list) -> List[MenuRow]:
    """Build engine rows for scanned WiFi networks (the ``wifi_networks`` provider).

    Pure transform from scan results to platform-neutral rows: one selectable row
    per network, keyed by SSID so the engine's ``wifi_connect`` item action acts
    on the chosen network, labelled with a (truncated) SSID and a signal-bucket
    icon. Returns a single non-selectable 'No networks found' row when the list is
    empty so the network submenu never renders blank.

    No e-paper styling is set here: ``MenuRow`` is platform-neutral (it has no
    font/size fields -- those belong to the board's ``IconMenuEntry``), and the
    board renderer applies default entry chrome when converting these rows. Kept
    pure so row construction is unit-tested rather than buried in a board closure.
    """
    if not networks:
        return [MenuRow(key="__none__", label="No networks found", icon="wifi_disconnected", selectable=False)]
    rows: List[MenuRow] = []
    for net in networks[:10]:
        ssid = net["ssid"]
        label = ssid[:18] if len(ssid) > 18 else ssid
        rows.append(MenuRow(key=ssid, label=label, icon=wifi_signal_icon(net["signal"])))
    return rows
