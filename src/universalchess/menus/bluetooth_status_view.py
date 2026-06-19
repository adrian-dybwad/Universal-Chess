"""Pure derivation of the Bluetooth status menu rows.

Turns a :class:`~universalchess.managers.bluetooth_status_state.BluetoothStatusState`
snapshot into framework-neutral row descriptors (``{key, label, icon}``). Kept
free of any renderer or hardware import so it is unit-testable and shared: the
board adapter (``main._bluetooth_status_rows``) wraps these into e-paper
``MenuRow``s with the ``bluetooth.status`` catalog chrome, while the wording and
which-rows-to-show logic lives here once.
"""

from typing import List

from universalchess.managers.ble_advertising_status import failure_label
from universalchess.managers.bluetooth_status_state import ADV_FAILED
from universalchess.managers.bluez_patch_status import warning_label as stack_warning_label


def bluetooth_status_menu_rows(snapshot: dict, status_label: str) -> List[dict]:
    """Return the status rows for the board Bluetooth menu, in display order.

    Always includes the status readout. Adds, conditionally:

    * a connected-client detail row (the active emulator and peer) only while a
      chess app is connected -- this is the live "what's in play" readout;
    * the advertised-names row when names are known;
    * an advertising-error row only when ``adv_state`` is ``failed`` -- i.e. the
      board is invisible to BLE scans -- so the failure is shown but never
      flashed while merely pending or paused;
    * a patched-stack warning row only when the board runs a substituted
      (non-stock) ``bluetoothd`` -- so the operator can see the deviation (and
      that it forgoes distro security updates) directly on the device.

    Args:
        snapshot: A :meth:`BluetoothStatusState.to_dict` payload.
        status_label: The pre-formatted multi-line status readout (device
            name/address + connection summary).
    """
    rows: List[dict] = [
        {"key": "Info", "label": status_label, "icon": "bluetooth"},
    ]

    if snapshot.get("connected"):
        emulator = snapshot.get("emulator") or snapshot.get("transport") or "app"
        detail = f"In play: {str(emulator).capitalize()}"
        peer = snapshot.get("peer") or {}
        peer_label = peer.get("name") or peer.get("address")
        if peer_label:
            detail += f"\n{peer_label}"
        rows.append({"key": "Link", "label": detail, "icon": "bluetooth"})

    names = snapshot.get("advertised_names") or []
    if names:
        rows.append({"key": "Names", "label": "\n".join(names), "icon": "bluetooth"})

    if snapshot.get("adv_state") == ADV_FAILED:
        detail = failure_label(snapshot.get("advertising") or {}) or "BLE advertising failed"
        rows.append({
            "key": "AdvertError",
            "label": f"Apps can't find board\n{detail}",
            "icon": "cancel",
        })

    stack_warning = stack_warning_label(snapshot.get("stack") or {})
    if stack_warning:
        rows.append({
            "key": "Stack",
            "label": f"{stack_warning}\nNo distro security updates",
            "icon": "info",
        })

    return rows
