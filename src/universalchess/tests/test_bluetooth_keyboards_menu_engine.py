"""Tests for the data-driven Bluetooth *keyboard pairing* flow.

Background / why these tests exist
----------------------------------
The continuous keyboard-discovery screen was migrated off the imperative
``bluetooth_menu.handle_keyboard_pairing_menu`` loop onto the shared engine: the
``bluetooth.keyboards`` container holds a ``dynamic`` list whose
``bluetooth_keyboards`` provider yields the live scan results and whose
``bluetooth_pair_select`` item action pairs the chosen keyboard. The scan thread,
the on-board passkey display, and the refresh-on-discovery subscription remain
board side effects wired in main's ``bluetooth_pair`` action (mirroring the WiFi
scan + live-status subscription).

These tests pin the catalog wiring and the pure ``keyboard_rows`` transform the
provider reuses -- one selectable row per named keyboard keyed by address, and the
Scanning/No-devices placeholders the deleted loop used to show so the screen is
never blank while discovery runs or after it ends empty.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.bluetooth_menu import keyboard_rows
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch, dispatch_row

_CATALOG = load_catalog()


def _kbd_ctx(*, named_devices=None, scanning=True):
    """Board context mirroring main's keyboard-pairing wiring.

    ``bluetooth_keyboards`` yields rows for the current scan results; the
    ``bluetooth_pair_select`` item action is recorded so the pairing seam is
    observable without the BlueZ discovery/pair side effects.
    """
    devs = named_devices if named_devices is not None else []
    ctx = BoardMenuContext()
    ctx.calls = []
    ctx.register_provider("bluetooth_keyboards", lambda: keyboard_rows(devs, scanning))
    ctx.register_action(
        "bluetooth_pair_select",
        lambda arg=None: ctx.calls.append(arg) or None,
    )
    return ctx


def test_pair_entry_opens_provider_backed_keyboard_list():
    """Pair is an action; the keyboard list is a provider-backed, item-actioned
    dynamic container.

    Why this test exists: Pair must stay an action (its handler owns the scan
    thread + passkey lifecycle), while the list it opens is data-driven -- the
    ``bluetooth_keyboards`` provider fills it and a pick routes to
    ``bluetooth_pair_select``. How a regression manifests: a missing
    provider/itemAction leaves the scan list inert, or Pair becomes a dead
    submenu with no scan lifecycle.
    """
    pair = _CATALOG.get_node("bluetooth.pair")
    assert pair["type"] == "action" and pair["action"] == "bluetooth_pair"

    assert _CATALOG.child_ids("bluetooth.keyboards") == ["bluetooth.keyboards.list"]
    lst = _CATALOG.get_node("bluetooth.keyboards.list")
    assert lst["type"] == "dynamic"
    assert lst["provider"] == "bluetooth_keyboards"
    assert lst["itemAction"] == "bluetooth_pair_select"


def test_keyboard_rows_one_selectable_row_per_named_device():
    """Each discovered, named keyboard is one selectable row keyed by address.

    Why this test exists: pairing acts on a specific address, so the row key must
    be the address; the label is the (truncated) advertised name. How a
    regression manifests: rows keyed by name/index pair the wrong device, or
    nameless mid-discovery entries leak in.
    """
    rows = keyboard_rows(
        [
            {"address": "AA:AA:AA:AA:AA:AA", "name": "Logi Keys"},
            {"address": "BB:BB:BB:BB:BB:BB", "name": "MX"},
        ],
        scanning=True,
    )
    assert [r.key for r in rows] == ["AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"]
    assert [r.label for r in rows] == ["Logi Keys", "MX"]
    assert all(r.selectable for r in rows)


def test_keyboard_rows_empty_while_scanning_shows_scanning_row():
    """An empty list while discovery runs shows a non-selectable 'Scanning...' row.

    Why this test exists: discovery is continuous and may be slow; the user needs
    feedback that the board is still looking rather than a blank screen. The row
    is non-selectable (keyed ``__scanning__``) so it cannot be paired. How a
    regression manifests: a blank list (looks broken) or a selectable placeholder
    that dispatches a bogus pair.
    """
    rows = keyboard_rows([], scanning=True)
    assert len(rows) == 1
    assert rows[0].key == "__scanning__"
    assert rows[0].label == "Scanning..."
    assert rows[0].selectable is False


def test_keyboard_rows_empty_after_scan_ends_shows_no_devices_row():
    """Once discovery ends with nothing found, the placeholder reads 'No devices'.

    Why this test exists: the end-of-scan empty state must be distinguishable
    from mid-scan so the user knows looking has stopped. How a regression
    manifests: a permanent 'Scanning...' that never resolves, implying the scan
    is stuck.
    """
    rows = keyboard_rows([], scanning=False)
    assert len(rows) == 1
    assert rows[0].key == "__none__"
    assert rows[0].label == "No devices"
    assert rows[0].selectable is False


def test_keyboard_row_dispatches_pair_select_with_address():
    """Selecting a keyboard runs the pair item action with its address.

    Why this test exists: this is the seam that pairs the chosen keyboard; the
    address must reach the handler so the right device is paired. How a
    regression manifests: the address is dropped and pairing targets nothing (or
    the wrong device).
    """
    ctx = _kbd_ctx(named_devices=[{"address": "CC:CC:CC:CC:CC:CC", "name": "K"}])
    rows = build_rows("bluetooth.keyboards", ctx, platform="board", catalog=_CATALOG)
    assert rows[0].action == "bluetooth_pair_select"
    out = dispatch_row(rows[0], ctx)
    assert out.kind == "action"
    assert ctx.calls == ["CC:CC:CC:CC:CC:CC"]
