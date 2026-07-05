# Tests for Bluetooth adapter alias derivation
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Unit tests for :mod:`universalchess.managers.adapter_alias`.

These guard the branded-alias contract: the alias must be ``UC-`` plus the
device-unique MAC tail, and malformed MACs must yield ``None`` (so callers fall
back to their prior identity) rather than a fabricated, meaningless name.
"""

import unittest

from universalchess.managers.adapter_alias import (
    derive_adapter_alias,
    resolve_adapter_alias,
)


class TestDeriveAdapterAlias(unittest.TestCase):
    def test_uses_last_three_octets_uppercase_without_separators(self):
        # The canonical case: a Raspberry Pi MAC (B8:27:EB OUI) must yield
        # UC- + the last three octets, uppercase, no colons. A regression that
        # took the wrong octets or kept separators would change the branded name
        # every board advertises.
        assert derive_adapter_alias("B8:27:EB:21:D2:51") == "UC-21D251"

    def test_lowercase_mac_is_normalised_to_uppercase(self):
        # BlueZ reports uppercase, but a lowercase MAC from any source must
        # still produce the same uppercase alias so the name is stable
        # regardless of input casing.
        assert derive_adapter_alias("b8:27:eb:aa:bb:cc") == "UC-AABBCC"

    def test_empty_mac_returns_none(self):
        # No MAC (adapter absent / unread) must return None so the caller keeps
        # its existing identity instead of advertising a bare "UC-".
        assert derive_adapter_alias("") is None

    def test_malformed_mac_returns_none(self):
        # A non-MAC string must not be coerced into an alias; returning None
        # (not a fabricated value) is what lets callers fall back safely.
        assert derive_adapter_alias("not-a-mac") is None

    def test_too_few_octets_returns_none(self):
        # Fewer than three octets cannot form the device-unique tail; this must
        # be rejected rather than padded or partially used.
        assert derive_adapter_alias("D2:51") is None

    def test_non_hex_octet_returns_none(self):
        # An octet with non-hex characters (right shape, wrong content) must be
        # rejected; a lax parser would emit a garbage alias.
        assert derive_adapter_alias("B8:27:EB:21:D2:ZZ") is None


class _FakeManager:
    """Stand-in exposing only ``get_adapter_info`` for resolution tests."""

    def __init__(self, info):
        self._info = info

    def get_adapter_info(self):
        return self._info


class TestResolveAdapterAlias(unittest.TestCase):
    def test_resolves_alias_from_manager_address(self):
        # resolve_adapter_alias must read the MAC from the injected manager and
        # run it through the same derivation, so board wiring gets the branded
        # alias without touching D-Bus in the test.
        manager = _FakeManager({"address": "B8:27:EB:21:D2:51", "name": "x"})
        assert resolve_adapter_alias(manager=manager) == "UC-21D251"

    def test_returns_none_when_address_missing(self):
        # An adapter with no address (BlueZ unreachable/absent) must yield None
        # so the caller keeps its prior identity rather than crashing.
        assert resolve_adapter_alias(manager=_FakeManager({"address": ""})) is None

    def test_swallows_manager_errors_and_returns_none(self):
        # Alias branding must never break BT bring-up: a manager that raises
        # while reading the adapter must be caught and yield None, not propagate.
        class _Boom:
            def get_adapter_info(self):
                raise RuntimeError("dbus down")

        assert resolve_adapter_alias(manager=_Boom()) is None


if __name__ == "__main__":
    unittest.main()
