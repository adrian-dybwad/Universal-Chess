"""Tests for the data-driven Analysis Engine menu (the ``analysis`` container).

Background / why these tests exist
----------------------------------
The Analysis Engine submenu was migrated off the bespoke
``handle_analysis_mode_menu`` builder onto the shared engine: the ``analysis``
container declares the Enabled toggle and an Engine row gated by ``visibleWhen``
on the toggle. main.py supplies an ``analysis`` store over the game settings
(persisting ``analysis_mode`` on toggle) and the ``select_analysis_engine``
action for the still-imperative engine pick. These tests build from the *real*
catalog with a fake store, pinning the conditional Engine row, the toggle
icon/persistence, the Engine label, and the action dispatch -- the guarantees the
deleted builder used to provide.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch, resolve_icon


def _analysis_ctx(*, mode=False, engine="stockfish"):
    """Board context mirroring main._build_analysis_context over a fake store.

    ``mode`` is read/written (the toggle persists it); ``engine`` is read-only
    (the displayed label). The engine-pick action is recorded so dispatch wiring
    can be asserted without running the real selection flow.
    """
    state = {"mode": mode, "engine": engine}

    def analysis_get(key):
        return state[key]

    def analysis_set(key, value):
        state[key] = value

    ctx = BoardMenuContext()
    ctx.register_store("analysis", analysis_get, analysis_set)
    ctx.calls = []
    ctx.register_action("select_analysis_engine", lambda: ctx.calls.append("select_analysis_engine") or None)
    ctx._state = state
    return ctx


def _rows(**kwargs):
    ctx = _analysis_ctx(**kwargs)
    return ctx, build_rows("analysis", ctx, platform="board", catalog=load_catalog())


def test_engine_row_hidden_until_analysis_enabled():
    """The Engine row appears only when analysis is enabled.

    Why this test exists: the old builder appended the Engine row only when
    analysis_mode was on; the data-driven build must reproduce that via
    ``visibleWhen`` so a disabled analysis shows just the toggle. How a regression
    manifests: the Engine row shows while analysis is off (dead row) or never
    shows while on.
    """
    _, off_rows = _rows(mode=False)
    assert [r.key for r in off_rows] == ["enabled"]

    _, on_rows = _rows(mode=True)
    assert [r.key for r in on_rows] == ["enabled", "engine"]


def test_enabled_toggle_icon_and_persistence():
    """The Enabled toggle shows timer_checked/timer and toggling persists it.

    Why this test exists: the toggle must render the on/off icon and actually
    write analysis_mode through the store (the old builder flipped the dict and
    saved). How a regression manifests: the icon desyncs from the flag, or
    selecting the row no longer persists the change.
    """
    catalog = load_catalog()
    node = catalog.get_node("analysis.enabled")

    on_ctx = _analysis_ctx(mode=True)
    assert resolve_icon(node, on_ctx) == "timer_checked"

    off_ctx = _analysis_ctx(mode=False)
    assert resolve_icon(node, off_ctx) == "timer"

    dispatch(node, off_ctx)
    assert off_ctx._state["mode"] is True


def test_engine_row_label_shows_current_engine():
    """The Engine row label embeds the currently selected analysis engine.

    Why this test exists: the row binds the analysis_engine value into its label
    ("Engine\\n<name>"), replacing the builder's "Engine: <name>" string. How a
    regression manifests: the label shows a literal '{value}' or a stale/blank
    engine name.
    """
    _, rows = _rows(mode=True, engine="lc0")
    engine_row = {r.key: r for r in rows}["engine"]
    assert engine_row.label == "Engine\nlc0"


def test_selecting_engine_row_dispatches_selection_action():
    """Selecting the Engine row invokes the engine-selection action.

    Why this test exists: the engine pick is a dynamic list kept as an imperative
    sub-flow; the row must reach it through the registered action rather than the
    deleted bespoke branch. How a regression manifests: selecting Engine does
    nothing or runs the wrong handler.
    """
    ctx = _analysis_ctx(mode=True)
    node = load_catalog().get_node("analysis.engine")
    outcome = dispatch(node, ctx)
    assert outcome.kind == "action" and outcome.action == "select_analysis_engine"
    assert ctx.calls == ["select_analysis_engine"]
