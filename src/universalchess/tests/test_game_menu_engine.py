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


def _game_ctx(
    *,
    mode=False,
    engine="stockfish",
    time_control=0,
    notation="figurine",
    coach_provider="none",
    coach_api_key="",
    coach_model="",
    coach_base_url="",
):
    """Board context mirroring main._build_game_menu_context over fake stores.

    The ``analysis`` store's ``mode`` is read/written (the toggle persists it)
    and ``engine`` is read/written (the analysis-engine select persists the
    pick). The ``game`` store backs Time Control, Move Notation, and the AI Coach
    fields; ``time_control`` is its concise computed label and ``coach_key_status``
    reports whether an API key is set (without exposing it). The
    ``installed_engines`` provider backs the Analysis Engine select so its
    dispatch can be asserted without the real flow.
    """
    state = {
        "mode": mode,
        "engine": engine,
        "time_control": time_control,
        "notation": notation,
        "coach_provider": coach_provider,
        "coach_api_key": coach_api_key,
        "coach_model": coach_model,
        "coach_base_url": coach_base_url,
    }

    ctx = BoardMenuContext()
    ctx.register_store("analysis", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    ctx.register_store("game", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    ctx.register_value(
        "time_control",
        lambda node: "Disabled" if state["time_control"] == 0 else f"{state['time_control']} min",
    )
    ctx.register_value(
        "coach_key_status",
        lambda node: "Set" if state["coach_api_key"] else "Not set",
    )
    ctx.register_value(
        "coach_model_label",
        lambda node: state["coach_model"] or "Default",
    )
    ctx.register_provider(
        "installed_engines",
        lambda: [
            MenuRow(key="stockfish", label="stockfish", icon="engine"),
            MenuRow(key="lc0", label="lc0", icon="engine"),
        ],
    )
    ctx.register_provider(
        "coach_models",
        lambda: [
            MenuRow(key="", label="Default", icon="settings"),
            MenuRow(key="gpt-4o", label="gpt-4o", icon="engine"),
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
    analysis toggle. The AI Coach provider row always shows (its key/model/base
    URL sub-fields are gated separately, see
    test_coach_rows_visibility_tracks_provider).
    """
    _, off_rows = _rows(mode=False)
    assert [r.key for r in off_rows] == ["TimeControl", "Notation", "enabled", "coach_provider"]

    _, on_rows = _rows(mode=True)
    assert [r.key for r in on_rows] == [
        "TimeControl", "Notation", "enabled", "engine", "coach_provider",
    ]


def test_coach_rows_visibility_tracks_provider():
    """Coach API key/model rows appear once a provider is chosen; base URL only for custom.

    Why this test exists: the AI Coach card must not clutter the Game menu with
    secret/model/URL rows while the coach is disabled, and the base URL is only
    meaningful for the custom (OpenAI-compatible) provider. How a regression
    manifests: the API key row shows while disabled (prompting for a key that is
    never used), or the base URL row is missing for the custom provider (so a
    self-hosted endpoint can't be entered) or shown for a built-in one.
    """
    _, none_rows = _rows(coach_provider="none")
    keys = [r.key for r in none_rows]
    assert "coach_provider" in keys
    assert "coach_api_key" not in keys
    assert "coach_model" not in keys
    assert "coach_base_url" not in keys

    _, openai_rows = _rows(coach_provider="openai")
    keys = [r.key for r in openai_rows]
    assert "coach_api_key" in keys
    assert "coach_model" in keys
    assert "coach_base_url" not in keys

    _, custom_rows = _rows(coach_provider="custom")
    custom_keys = [r.key for r in custom_rows]
    # Custom keeps a free-text model row (endpoint-specific ids) and adds base URL.
    assert "coach_model" in custom_keys
    assert "coach_base_url" in custom_keys


def test_coach_model_row_is_provider_backed_select_for_builtin_providers():
    """OpenAI/Anthropic Coach Model is a live provider-backed select on coach_model.

    Why this test exists: the model must come from the live ``coach_models`` list
    (fetched via the API key), not a free-text field where a typo/stale id 404s.
    How a regression manifests: the row reverts to text, or its select drops the
    provider/binding so the fetched models can't be chosen.
    """
    ctx = _game_ctx(coach_provider="openai")
    node = load_catalog().get_node("coach.model")
    outcome = dispatch(node, ctx)
    assert outcome.kind == "select"
    assert outcome.provider == "coach_models"
    assert outcome.store == "game" and outcome.key == "coach_model"


def test_coach_model_row_label_reads_default_when_blank():
    """A blank coach_model renders as "Model\\nDefault", not an empty line.

    Why this test exists: blank means "use the provider default"; the board label
    must communicate that. How a regression manifests: the label shows "Model\\n"
    (trailing blank) so the user can't tell what will be used.
    """
    _, rows = _rows(coach_provider="openai", coach_model="")
    model_row = {r.key: r for r in rows}["coach_model"]
    assert model_row.label == "Model\nDefault"

    _, rows = _rows(coach_provider="openai", coach_model="gpt-4o")
    model_row = {r.key: r for r in rows}["coach_model"]
    assert model_row.label == "Model\ngpt-4o"


def test_coach_api_key_row_label_hides_the_secret():
    """The API key row's board label shows only whether a key is set, never the key.

    Why this test exists: a secret must not be rendered on the shared board
    display. The label uses the ``coach_key_status`` compute ("Set"/"Not set").
    How a regression manifests: switching the label to ``{value}`` would print the
    raw API key on-screen.
    """
    _, rows = _rows(coach_provider="openai", coach_api_key="sk-secret")
    api_row = {r.key: r for r in rows}["coach_api_key"]
    assert "sk-secret" not in api_row.label
    assert api_row.label == "API Key\nSet"

    _, rows = _rows(coach_provider="openai", coach_api_key="")
    api_row = {r.key: r for r in rows}["coach_api_key"]
    assert api_row.label == "API Key\nNot set"


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
