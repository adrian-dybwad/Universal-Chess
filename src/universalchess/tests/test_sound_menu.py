"""Tests for the Sound submenu, now driven by the shared menu engine.

Background / why these tests exist
----------------------------------
The Sound submenu was migrated off a bespoke builder onto the data-driven
engine: its rows, master-first order, labels, state icons, and bold master
styling all come from the ``settings.sound`` catalog node. These tests build the
menu rows from the *real* catalog through the engine with a fake sound store, so
the migrated composition is pinned at the same fidelity the old
``build_sound_entries`` guarded -- without the deleted module.
"""

from universalchess.menus.board_context import BoardMenuContext, _row_to_entry
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows


def _ctx(**overrides):
    """Board context with a dict-backed sound store keyed by catalog bind keys."""
    state = {
        "enabled": True,
        "piece_events": False,
        "game_events": True,
        "errors": True,
        "key_press": False,
    }
    state.update(overrides)
    ctx = BoardMenuContext(option_set_fn=lambda name: [])
    ctx.register_store("sound", lambda k: state[k], lambda k, v: state.__setitem__(k, bool(v)))
    return ctx


def _rows(**overrides):
    catalog = load_catalog()
    return build_rows("settings.sound", _ctx(**overrides), platform="board", catalog=catalog)


def test_master_sound_toggle_is_first_entry():
    """'Sound Enabled' (master switch) is the first row of the Sound submenu.

    Why this test exists: the master switch gates all other categories, so it
    reads top-of-list. After migration the order comes from the catalog node's
    children. How the regression manifests: the enabled row is no longer first,
    because the catalog children order was changed or the engine reordered rows.
    """
    rows = _rows()
    assert rows[0].node["id"] == "field.sound.enabled"
    assert [r.node["id"] for r in rows] == [
        "field.sound.enabled",
        "field.sound.piece_events",
        "field.sound.game_events",
        "field.sound.errors",
        "field.sound.key_press",
    ]


def test_master_toggle_marked_bold():
    """The master switch renders bold to distinguish it from categories.

    Why: the bold styling (from the node's epaper block) is the visual cue that
    the master switch is structurally different. How the regression manifests:
    the enabled entry loses bold because the catalog epaph style or the row->entry
    conversion dropped it.
    """
    entries = {r.node["id"]: _row_to_entry(r) for r in _rows()}
    assert entries["field.sound.enabled"].bold is True
    assert entries["field.sound.piece_events"].bold is False


def test_toggle_icons_reflect_setting_state():
    """Each row's icon reflects its on/off state via the state-map icon.

    Why: the checkbox-style icon is the only indication of whether a category is
    enabled; the engine must select it from the bound value. How the regression
    manifests: an 'on' setting shows 'timer' (empty) or an 'off' setting shows
    'timer_checked'.
    """
    by_id = {r.node["id"]: r for r in _rows(enabled=True, key_press=False)}
    assert by_id["field.sound.enabled"].icon == "timer_checked"
    assert by_id["field.sound.key_press"].icon == "timer"
