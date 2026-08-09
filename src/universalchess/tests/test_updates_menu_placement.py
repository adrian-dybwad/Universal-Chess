"""Tests that Updates sits in the same place on the board as on the web.

Background / why these tests exist
----------------------------------
Updates was reachable on the board only through System -> About -> Updates, while
the web puts it directly in the System tab. The same feature therefore lived at a
different depth under a different parent depending on which interface you used,
so knowing where it was on one told you nothing about the other. "About" also
reads as a read-only screen, which is a poor place to hide the only actions that
change the installed software.

The web placement won: the board now lists Updates directly in its System menu,
between the device preferences and Reset, mirroring the web tab's card order.
About keeps what the web's system-info card shows -- version and telemetry.

These tests pin both halves of that: the catalog placement, and the board wiring
that has to travel with the node. The wiring is checked against main.py's source
because importing ``universalchess.main`` performs hardware and display
initialisation at import time, which the rest of the suite also avoids.
"""

import ast
from pathlib import Path

import universalchess
from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch, resolve_icon

MAIN_PY = Path(universalchess.__file__).resolve().parent / "main.py"

UPDATES_NODE = "system.updates"
SYSTEM_CONTEXT = "_build_system_context"
ABOUT_CONTEXT = "_build_about_context"

# The action that opens the Updates menu, the compute that supplies the row's
# summary label, and the store key its icon state reads. All three have to be
# available wherever the row is rendered.
OPEN_ACTION = "open_updates"
STATUS_VALUE = "updates_status"
STATE_KEY = "update_state"


def _catalog():
    return load_catalog()


def _function_node(name):
    """Return the AST for the named top-level function in main.py."""
    tree = ast.parse(MAIN_PY.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {MAIN_PY}")


def _registered(function_name):
    """Names registered on the context by a ``_build_*_context`` function.

    Returns a dict of registration kind -> set of names, covering nested
    functions so a registration inside a closure still counts.
    """
    found = {}
    for node in ast.walk(_function_node(function_name)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        kind = node.func.attr
        if not kind.startswith("register_") or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.setdefault(kind, set()).add(first.value)
    return found


def _store_keys_handled(function_name):
    """String literals the function compares a store key against.

    The stores are plain ``if key == "...":`` dispatchers, so the set of literals
    compared in the function body is the set of keys it can serve.
    """
    keys = set()
    for node in ast.walk(_function_node(function_name)):
        if isinstance(node, ast.Compare):
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    keys.add(comparator.value)
    return keys


def test_updates_is_listed_directly_in_the_board_system_menu():
    """The board's System menu names the Updates row itself.

    Why this test exists: this is the placement that matches the web System tab,
    and it is what stops the board from burying the update actions one level
    deeper than every other System entry.

    How a regression manifests: the row is moved back under another container, so
    the board and web disagree again about where updates live, and the board path
    grows an extra step.
    """
    catalog = _catalog()
    assert catalog.has_node(UPDATES_NODE)
    assert UPDATES_NODE in catalog.child_ids("system")


def test_updates_is_ordered_like_the_web_system_tab():
    """Updates sits after the device preferences and before Reset.

    Why this test exists: the web System tab orders its cards device settings,
    then updates, then the destructive reset actions. Matching that order is the
    point of the move -- placing the row correctly but ordering it differently
    still leaves the two interfaces looking unrelated.

    How a regression manifests: an append puts Updates after Reset and Power, so
    it lands among the destructive and terminal actions instead of beside the
    other device configuration.
    """
    children = _catalog().child_ids("system")
    assert children.index("group.system.device") < children.index(UPDATES_NODE)
    assert children.index(UPDATES_NODE) < children.index("system.reset")


def test_updates_is_no_longer_reachable_through_about():
    """About lists only the readouts; it no longer offers Updates.

    Why this test exists: leaving the old row in place would give the board two
    routes to the same screen and re-create the inconsistency this change
    removes. About's remaining contents mirror the web's system-info card.

    How a regression manifests: ``about.updates`` resolves again, or About's
    children regain an entry opening the update menu, so the board once more
    presents updates as an "about" detail.
    """
    catalog = _catalog()
    assert not catalog.has_node("about.updates")
    assert catalog.child_ids("about") == ["about.version", "about.telemetry"]
    for child_id in catalog.child_ids("about"):
        assert catalog.get_node(child_id).get("action") != OPEN_ACTION, child_id


def test_updates_row_reads_its_icon_state_from_the_system_store():
    """The row binds into the ``system`` store it is now rendered under.

    Why this test exists: the row's icon is driven by a bound value, so the bind
    has to name a store the System menu's context actually registers. Left
    pointing at the About screen's store, the row renders inside a context that
    has never heard of it.

    How a regression manifests: the System menu raises on the unknown store while
    building its rows, taking out the whole screen rather than just this row.
    """
    node = _catalog().get_node(UPDATES_NODE)
    assert node.get("bind") == {"store": "system", "key": STATE_KEY}
    assert node.get("action") == OPEN_ACTION
    assert node.get("restore_target") == "updates"


def test_system_context_supplies_everything_the_moved_row_needs():
    """main.py registers the action, the label compute and the store key.

    Why this test exists: the node carries three separate pieces of wiring, and
    moving the JSON without moving all three leaves a row that draws but cannot
    act, or one that fails while resolving its label or icon. Only the action is
    obvious from the node; the compute and the store key are easy to overlook.

    How a regression manifests: selecting Updates does nothing (missing action),
    or the System menu errors while rendering (missing compute or store key).
    """
    registered = _registered(SYSTEM_CONTEXT)
    assert OPEN_ACTION in registered.get("register_action", set())
    assert STATUS_VALUE in registered.get("register_value", set())
    assert STATE_KEY in _store_keys_handled(SYSTEM_CONTEXT)


def test_about_context_drops_the_wiring_it_no_longer_uses():
    """The About context stops registering the update action and compute.

    Why this test exists: wiring left behind for a row that no longer exists is
    indistinguishable from wiring that is still needed, and it invites someone to
    "restore" the row it appears to support.

    How a regression manifests: dead registrations accumulate in the About
    context and the two contexts drift, making it unclear which screen owns the
    update row.
    """
    registered = _registered(ABOUT_CONTEXT)
    assert OPEN_ACTION not in registered.get("register_action", set())
    assert STATUS_VALUE not in registered.get("register_value", set())
    assert STATE_KEY not in _store_keys_handled(ABOUT_CONTEXT)


def _system_ctx(*, update_state="manual", update_label="Manual"):
    """Context mirroring main._build_system_context, for row rendering.

    Only the values the System container needs to draw are supplied; the actions
    record rather than perform, so selecting a row is observable without running
    the real menu.
    """
    values = {
        "sleep_seconds": 300,
        "timezone": "UTC",
        "ntp_enabled": True,
        "ui_language": "en",
        STATE_KEY: update_state,
    }

    ctx = BoardMenuContext()
    ctx.register_store("system", values.__getitem__, values.__setitem__)
    ctx.register_value(STATUS_VALUE, lambda node: update_label)
    ctx.opened = []
    for action in ("engine_manager", "about", "reset_confirm", "cancel", "shutdown", "reboot"):
        ctx.register_action(action, lambda name=action: ctx.opened.append(name) or None)
    ctx.register_action(OPEN_ACTION, lambda: ctx.opened.append(OPEN_ACTION) or None)
    return ctx


def test_updates_row_icon_and_label_track_the_update_state():
    """The row's icon and summary stay consistent as the status changes.

    Why this test exists: this behaviour predates the move (it was covered on the
    About screen) and is the row's whole value at a glance -- the icon and the
    label are chosen from one status and must not disagree. Re-asserting it here
    proves the move preserved it rather than merely relocating a row that renders.

    How a regression manifests: the icon stops tracking the state, or the label
    desyncs from it, so a board with an update ready looks identical to one on
    manual updates.
    """
    catalog = _catalog()
    node = catalog.get_node(UPDATES_NODE)
    cases = {
        "ready": ("update", "Ready!"),
        "available": ("update", "v9.9.9"),
        "auto": ("checkbox_checked", "Auto"),
        "manual": ("checkbox_empty", "Manual"),
    }
    for state, (icon, label) in cases.items():
        ctx = _system_ctx(update_state=state, update_label=label)
        assert resolve_icon(node, ctx) == icon, state
        rows = {r.key: r for r in build_rows("system", ctx, platform="board", catalog=catalog)}
        assert rows["Updates"].label == f"Updates\n{label}", state


def test_selecting_updates_from_the_system_menu_opens_the_update_menu():
    """Selecting the row invokes the action that opens the Updates menu.

    Why this test exists: the row is the only route to the update screen on the
    board, and the action is resolved through the context the System menu builds,
    not the one About used to build.

    How a regression manifests: the action is unregistered in the new context, so
    selecting Updates silently does nothing and the board cannot be updated from
    its own menus.
    """
    ctx = _system_ctx()
    outcome = dispatch(_catalog().get_node(UPDATES_NODE), ctx)
    assert outcome.kind == "action" and outcome.action == OPEN_ACTION
    assert ctx.opened == [OPEN_ACTION]
