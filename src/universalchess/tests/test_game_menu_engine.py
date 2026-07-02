"""Tests for the data-driven Game submenu (the ``settings.game`` container).

Background / why these tests exist
----------------------------------
Time Control and Live Analysis were unified into a single ``settings.game``
container so the board groups them under one "Game" menu, matching the web Game
tab (the same catalog nodes drive both -- one source of truth). The container
declares the Time Control select, the Live Analysis (``analysis.enabled``)
toggle, and an Analysis Engine (``analysis.engine``) row gated by ``visibleWhen``
on the toggle. main.py supplies the ``game`` store (Time Control), the
``analysis`` store (mode read/write, engine read-only label), the Time Control
label compute, and the ``select_analysis_engine`` action for the still-imperative
engine pick. These tests build from the *real* catalog with fake stores, pinning
the row set/order, the conditional Engine row, the toggle icon/persistence, the
Engine label, and the action dispatch -- the guarantees the separate Analysis
container used to provide before it was folded in.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import MenuRow, build_rows, dispatch, resolve_icon


def _game_ctx(*, mode=False, engine="stockfish", time_control=0, notation="figurine"):
    """Board context mirroring main._build_game_menu_context over fake stores.

    The ``analysis`` store's ``mode`` is read/written (the toggle persists it)
    and ``engine`` is read/written (the analysis-engine select persists the
    pick). The ``game`` store backs Time Control and Move Notation, and
    ``time_control`` is its concise computed label. The ``installed_engines``
    provider backs the Analysis Engine select so its dispatch can be asserted
    without the real flow.
    """
    state = {"mode": mode, "engine": engine, "time_control": time_control, "notation": notation}

    ctx = BoardMenuContext()
    ctx.register_store("analysis", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    ctx.register_store("game", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    ctx.register_value(
        "time_control",
        lambda node: "Disabled" if state["time_control"] == 0 else f"{state['time_control']} min",
    )
    ctx.register_provider(
        "installed_engines",
        lambda: [
            MenuRow(key="stockfish", label="stockfish", icon="engine"),
            MenuRow(key="lc0", label="lc0", icon="engine"),
        ],
    )
    ctx._state = state
    return ctx


def _rows(**kwargs):
    ctx = _game_ctx(**kwargs)
    return ctx, build_rows("settings.game", ctx, platform="board", catalog=load_catalog())


def test_game_menu_rows_and_engine_visibility():
    """Game lists Time Control + Live Analysis, with Engine gated on the toggle.

    Why this test exists: the unified Game menu must show Time Control and the
    analysis toggle always, and reveal the Analysis Engine row only when Live
    Analysis is on (via ``visibleWhen``). How a regression manifests: an item is
    dropped/reordered, the Engine row shows while analysis is off (dead row), or
    never shows while on. Move Notation always shows between Time Control and the
    analysis toggle.
    """
    _, off_rows = _rows(mode=False)
    assert [r.key for r in off_rows] == ["TimeControl", "Notation", "enabled"]

    _, on_rows = _rows(mode=True)
    assert [r.key for r in on_rows] == ["TimeControl", "Notation", "enabled", "engine"]


def test_time_control_row_label_and_icon_track_value():
    """The Time Control row shows a concise label and a value-dependent icon.

    Why this test exists: untimed must read "Time\\nDisabled" with the empty timer
    icon, and a set value "Time\\nN min" with the checked timer icon, from the
    catalog's computed label and state-mapped icon. How a regression manifests:
    the icon stops tracking whether a clock is set, or the label shows the verbose
    option text.
    """
    untimed = {r.key: r for r in _rows(time_control=0)[1]}["TimeControl"]
    assert untimed.label == "Time\nDisabled"
    assert untimed.icon == "timer"

    timed = {r.key: r for r in _rows(time_control=5)[1]}["TimeControl"]
    assert timed.label == "Time\n5 min"
    assert timed.icon == "timer_checked"


def test_enabled_toggle_icon_and_persistence():
    """The Live Analysis toggle shows timer_checked/timer and toggling persists it.

    Why this test exists: the toggle must render the on/off icon and actually
    write analysis_mode through the store. How a regression manifests: the icon
    desyncs from the flag, or selecting the row no longer persists the change.
    """
    catalog = load_catalog()
    node = catalog.get_node("analysis.enabled")

    on_ctx = _game_ctx(mode=True)
    assert resolve_icon(node, on_ctx) == "timer_checked"

    off_ctx = _game_ctx(mode=False)
    assert resolve_icon(node, off_ctx) == "timer"

    dispatch(node, off_ctx)
    assert off_ctx._state["mode"] is True


def test_engine_row_label_shows_current_engine():
    """The Engine row label embeds the currently selected analysis engine.

    Why this test exists: the row binds the analysis_engine value into its label
    ("Engine\\n<name>"). How a regression manifests: the label shows a literal
    '{value}' or a stale/blank engine name.
    """
    _, rows = _rows(mode=True, engine="lc0")
    engine_row = {r.key: r for r in rows}["engine"]
    assert engine_row.label == "Engine\nlc0"


def test_analysis_engine_row_is_provider_backed_select():
    """Selecting the Engine row opens a provider-backed select on analysis.engine.

    Why this test exists: the analysis engine pick was migrated from an imperative
    action sub-flow to a ``select`` whose options come from the ``installed_engines``
    provider and are written to analysis.engine -- the same provider/value pattern
    as the player Engine/ELO pickers. How a regression manifests: the row reverts
    to a (now-removed) action, or the outcome drops the provider/binding so the
    analysis engine can't be chosen on the board.
    """
    ctx = _game_ctx(mode=True)
    node = load_catalog().get_node("analysis.engine")
    outcome = dispatch(node, ctx)
    assert outcome.kind == "select"
    assert outcome.provider == "installed_engines"
    assert outcome.store == "analysis" and outcome.key == "engine"
