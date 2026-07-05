# Bluetooth Adapter Alias Derivation
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Derivation of the board's branded Bluetooth adapter *alias*.

The adapter alias is the friendly Bluetooth name a phone shows for the board:
it is BlueZ's ``Adapter1.Alias``, which is both the Classic-Bluetooth device
name and the source of the BLE GAP "Device Name" characteristic (0x2A00) a
client can read after connecting.

The alias is deliberately distinct from the per-advertisement BLE ``LocalName``
values (``MILLENNIUM CHESS`` / ``DGT PEGASUS`` / ``Chessnut Air``) that chess
apps discover by. Those are set per advertisement and are unaffected by the
alias -- verified against HIARCS, which connects over BLE regardless of the
adapter alias because it keys off the advertised ``LocalName``. Branding the
alias therefore does not change app discovery; it only changes the friendly
name shown for the board itself.

The alias is derived from the adapter's own MAC so every board gets a unique,
branded name (``UC-`` + the MAC's device-unique tail) with no per-unit config.
"""

import logging
import re
from typing import Optional

# Branded prefix for the adapter alias.
ALIAS_PREFIX = "UC-"

# Trailing MAC octets that uniquely identify the board. The leading octets are
# the vendor OUI (e.g. ``B8:27:EB`` is shared across all Raspberry Pis), so the
# last three octets are the device-unique portion.
UNIQUE_OCTET_COUNT = 3

# A single MAC octet: exactly two hex digits.
_OCTET_RE = re.compile(r"^[0-9A-Fa-f]{2}$")


def derive_adapter_alias(mac_address: str) -> Optional[str]:
    """Return the branded alias ``UC-<tail>`` for a MAC, or ``None`` if invalid.

    Args:
        mac_address: Colon-separated adapter MAC (e.g. ``"B8:27:EB:21:D2:51"``).

    Returns:
        ``"UC-"`` followed by the last :data:`UNIQUE_OCTET_COUNT` octets,
        uppercase and without separators (e.g. ``"UC-21D251"``). Returns
        ``None`` -- rather than a fabricated name -- when the MAC is empty or
        malformed, so callers fall back to their existing identity instead of
        advertising a meaningless alias.
    """
    if not mac_address:
        return None
    octets = mac_address.strip().split(":")
    if len(octets) < UNIQUE_OCTET_COUNT:
        return None
    tail = octets[-UNIQUE_OCTET_COUNT:]
    if not all(_OCTET_RE.match(octet) for octet in tail):
        return None
    return ALIAS_PREFIX + "".join(octet.upper() for octet in tail)


def resolve_adapter_alias(manager=None, log: Optional[logging.Logger] = None) -> Optional[str]:
    """Read the adapter MAC via BlueZ and derive the branded alias.

    Args:
        manager: Optional object exposing ``get_adapter_info() -> {"address": ...}``
            (a :class:`~universalchess.managers.bluez_pairing.BluezPairingManager`
            by default). Injectable so the resolution is testable without D-Bus.
        log: Optional logger for the failure path.

    Returns:
        The branded alias, or ``None`` when the MAC cannot be read or is
        malformed -- letting callers keep their prior identity. Never raises:
        alias branding must not break Bluetooth bring-up.
    """
    try:
        if manager is None:
            from universalchess.managers.bluez_pairing import BluezPairingManager

            manager = BluezPairingManager()
        address = manager.get_adapter_info().get("address", "")
        return derive_adapter_alias(address)
    except Exception as e:  # noqa: BLE001 - alias resolution must not break BT bring-up
        if log:
            log.warning(f"[AdapterAlias] Could not resolve adapter alias: {e}")
        return None
