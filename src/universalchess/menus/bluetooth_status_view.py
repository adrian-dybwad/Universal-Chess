"""Pure derivation of the Bluetooth status menu row.

Turns a :class:`~universalchess.managers.bluetooth_status_state.BluetoothStatusState`
snapshot (plus the adapter identity) into a single framework-neutral row
descriptor (``{key, label, icon}``). Kept free of any renderer or hardware
import so it is unit-testable and shared: the board adapter
(``main._bluetooth_status_rows``) wraps this into an e-paper ``MenuRow`` with the
selectable ``bluetooth.enabled`` toggle chrome (the readout *is* the enable
control), while the wording lives here once.

The readout is deliberately ONE fixed button rather than a variable list of
rows: it always carries the device identity (icon, host name, MAC) and then a
state-driven activity block, so the layout is predictable and the whole readout
can double as the enable/disable toggle.
"""

from typing import List, Optional

from universalchess.managers.ble_advertising_status import failure_label
from universalchess.managers.bluetooth_status_state import (
    ADV_ADVERTISING,
    ADV_FAILED,
    ADV_HEALING,
    ADV_PAUSED_CONNECTED,
    ADV_RADIO_OFF,
    ADV_UNKNOWN,
)


def bluetooth_status_menu_rows(
    snapshot: dict,
    device_name: Optional[str],
    address: Optional[str],
) -> List[dict]:
    """Return the single merged Bluetooth status/enable button row.

    The row always begins with the device identity -- host name and, when known,
    the adapter MAC. It then adds, in order:

    * a live connection line ("In play: <emulator>" + peer) whenever a chess app
      is linked -- the "what's in play" readout, shown independent of advertising
      because an RFCOMM link keeps BLE advertising up;
    * a state-driven activity block derived from ``adv_state`` (a closed union):

        - ``advertising``  -> "Broadcasting:" + the advertised names;
        - ``unknown``      -> "Starting Bluetooth" + the names it will advertise
          (registration is still pending -- do not yet claim it is broadcasting);
        - ``paused_connected`` -> nothing extra (the connection line above already
          states the link; LE advertising is legitimately paused);
        - ``healing``      -> "Fixing Bluetooth" + the shared self-heal phase
          label + the names (the multi-minute on-board rebuild is repairing
          advertising, shown instead of the raw failure so the user is reassured);
        - ``failed``       -> "Apps can't find board" + the failure summary + the
          names it is trying to broadcast, and the icon becomes ``cancel`` so the
          single button itself signals the alarm;
        - ``radio_off``    -> "Disabled" and NO names (nothing is being
          broadcast, so listing names would falsely imply discoverability).

    Every known ``adv_state`` is handled explicitly; an unrecognised value raises
    rather than silently defaulting, so adding a new state to the engine forces a
    matching UI decision here instead of the button quietly misrepresenting it.

    The patched-stack (non-stock ``bluetoothd``) warning is intentionally NOT
    shown here: it lives in the web System Information card (which reads the
    self-heal marker directly), so the board's Bluetooth readout stays focused on
    live connection/advertising state.

    Args:
        snapshot: A :meth:`BluetoothStatusState.to_dict` payload.
        device_name: The primary advertised host name (heads the button).
        address: The adapter MAC address, or empty/``None`` when not yet probed.
    """
    lines: List[str] = []
    if device_name:
        lines.append(device_name)
    if address:
        lines.append(address)

    if snapshot.get("connected"):
        emulator = snapshot.get("emulator") or snapshot.get("transport") or "app"
        lines.append(f"In play: {str(emulator).capitalize()}")
        peer = snapshot.get("peer") or {}
        peer_label = peer.get("name") or peer.get("address")
        if peer_label:
            lines.append(str(peer_label))

    names = [name for name in (snapshot.get("advertised_names") or []) if name]
    adv_state = snapshot.get("adv_state")
    icon = "bluetooth"

    if adv_state == ADV_RADIO_OFF:
        lines.append("Disabled")
    elif adv_state == ADV_FAILED:
        icon = "cancel"
        detail = failure_label(snapshot.get("advertising") or {}) or "BLE advertising failed"
        lines.append("Apps can't find board")
        lines.append(detail)
        lines.extend(names)
    elif adv_state == ADV_HEALING:
        heal = snapshot.get("heal") or {}
        lines.append("Fixing Bluetooth")
        lines.append(heal.get("label") or "Repairing advertising...")
        lines.extend(names)
    elif adv_state == ADV_PAUSED_CONNECTED:
        # A BLE central is connected; LE advertising pauses. The connection line
        # above already states the active link, so no broadcasting header (and no
        # names) is added -- claiming "Broadcasting" here would be false.
        pass
    elif adv_state == ADV_ADVERTISING:
        lines.append("Broadcasting:")
        lines.extend(names)
    elif adv_state == ADV_UNKNOWN:
        lines.append("Starting Bluetooth")
        lines.extend(names)
    else:
        raise ValueError(f"Unhandled Bluetooth adv_state: {adv_state!r}")

    return [{"key": "Info", "label": "\n".join(lines), "icon": icon}]
