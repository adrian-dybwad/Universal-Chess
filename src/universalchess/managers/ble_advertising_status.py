"""Schema + helpers for the BLE advertisement registration sub-block.

The board (main) process owns the :class:`~universalchess.managers.ble.BleManager`,
which registers the BLE advertisements (DGT PEGASUS / Chessnut Air / MILLENNIUM
CHESS) that let phone chess apps discover the board over Bluetooth LE. When that
registration fails -- e.g. the service user cannot run the privileged ``btmgmt``
setup, so BlueZ refuses the advertisements with ``org.bluez.Error.Failed`` -- the
board still answers classic RFCOMM but is invisible to BLE scans, so apps never
find it.

The live status is held by :class:`~universalchess.managers.bluetooth_status_state.BluetoothStatusState`
in the board process and broadcast to the web; this module owns only the
``advertising`` sub-block *shape* and its failure wording, so the engine, the
board menu, and the web card cannot drift on what the counts mean.

Schema (``make_status``)::

    {
        "expected": int,    # advertisements the board tried to register
        "registered": int,  # advertisements BlueZ accepted
        "failed": int,      # advertisements BlueZ rejected
        "ok": bool,         # failed == 0 (pending counts as ok: never alarm early)
        "error": str|None,  # last BlueZ error message, for diagnostics
        "names": [str],     # local names of the advertisements (e.g. "DGT PEGASUS")
    }
"""

from typing import List, Optional


def make_status(expected: int, registered: int, failed: int,
                error: Optional[str] = None,
                names: Optional[List[str]] = None) -> dict:
    """Build a normalized advertisement-status dict.

    ``ok`` is defined purely as ``failed == 0`` so that the still-pending startup
    window (results not in yet, ``failed == 0``) is reported as ok and the UI
    never flashes a false failure before BlueZ has answered. Only an actual
    rejection (``failed > 0``) flips it, which is the condition that hides the
    board from BLE scans.
    """
    return {
        "expected": int(expected),
        "registered": int(registered),
        "failed": int(failed),
        "ok": int(failed) == 0,
        "error": error,
        "names": list(names) if names else [],
    }


def unknown_status() -> dict:
    """Return the status used when no result has been written yet.

    Treated as ok so a missing file (BLE never started, fresh boot, or BLE
    disabled) does not render as a failure.
    """
    return make_status(0, 0, 0, error=None, names=[])


def failure_label(status: dict) -> Optional[str]:
    """Return a short one-line failure summary, or ``None`` when ok.

    Shared by the board e-paper menu and the web card so the wording stays
    identical on both surfaces.
    """
    if status.get("ok", True):
        return None
    failed = status.get("failed", 0)
    expected = status.get("expected", 0)
    if expected:
        return f"{failed}/{expected} BLE adverts failed"
    return "BLE advertising failed"
