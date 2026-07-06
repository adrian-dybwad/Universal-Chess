"""Tests for the data-driven Game and Agents submenus.

Background / why these tests exist
----------------------------------
The board groups Time Control, Move Notation, Live Analysis, the Coach persona,
and the Agent selector under one ``settings.game`` container (matching the web
Game tab). The AI *agent* configuration lives under a separate ``settings.agents``
container that lists every registered agent (built-in + user modules); selecting
one opens its ``agents.detail`` submenu to configure that agent's API key, model,
and -- for agents that require one -- base URL. The same catalog nodes drive both
the board and the web, so these tests build from the *real* catalog with fake
stores/providers to pin: the Game row set/order and conditional Engine row; the
Agent selector being a registry-backed select; the Agents list expanding to one
row per agent; and the per-agent detail rows (visibility by model kind and
base-URL requirement, the secret-hiding API-key label, and the live model select).
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
    chess960=False,
    coach_provider="none",
    coach_id="auto",
    coach_language="English",
    agent_edit_id="",
    agent_model_kind="model",
    agent_requires_base_url=False,
    agent_api_key="",
    agent_model="",
    agent_base_url="",
):
    """Board context mirroring main._build_game_menu_context/_build_agents_menu_context.

    The ``analysis`` store's ``mode``/``engine`` are read/written; the ``game``
    store backs Time Control, Move Notation, Coach persona (``coach_id``) and the
    Agent selector (``coach_provider``). The ``agent_edit`` store is the transient
    selection the Agents detail screen edits: metadata (``id``/``model_kind``/
    ``requires_base_url``) gates the detail rows, and ``api_key``/``model``/
    ``base_url`` proxy the selected agent's stored values. Providers back the
    selects (agents list, agent choices, live models, installed engines, coaches).
    """
    state = {
        "mode": mode,
        "engine": engine,
        "time_control": time_control,
        "notation": notation,
        "chess960": chess960,
        "coach_provider": coach_provider,
        "coach_id": coach_id,
        "coach_language": coach_language,
    }
    edit_state = {
        "id": agent_edit_id,
        "name": agent_edit_id,
        "model_kind": agent_model_kind,
        "requires_base_url": agent_requires_base_url,
        "api_key": agent_api_key,
        "model": agent_model,
        "base_url": agent_base_url,
        # Mirrors the board adapter's derived flag that gates the "Clear API Key"
        # detail row: it is shown only when a key is actually stored.
        "has_api_key": bool(agent_api_key),
    }

    ctx = BoardMenuContext()
    ctx.register_store("analysis", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    ctx.register_store("game", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    ctx.register_store("agent_edit", lambda k: edit_state[k], lambda k, v: edit_state.__setitem__(k, v))
    ctx.register_value(
        "time_control",
        lambda node: "Disabled" if state["time_control"] == 0 else f"{state['time_control']} min",
    )
    # Concise label for the active coach; Auto shows the resolved coach in the real
    # app, but the fake just distinguishes Auto from an explicit pick.
    ctx.register_value(
        "coach_selected_label",
        lambda node: "Auto" if state["coach_id"] == "auto" else state["coach_id"],
    )
    ctx.register_value(
        "agent_key_status",
        lambda node: "Set" if edit_state["api_key"] else "Not set",
    )
    ctx.register_value(
        "agent_model_label",
        lambda node: edit_state["model"] or "Default",
    )
    ctx.register_value(
        "agent_base_url_label",
        lambda node: edit_state["base_url"] or "Not set",
    )
    ctx.register_provider(
        "coaches",
        lambda: [
            MenuRow(key="off", label="Disabled", icon="settings"),
            MenuRow(key="auto", label="Auto", icon="engine"),
            MenuRow(key="dave", label="Dave (800)", icon="engine"),
            MenuRow(key="myron", label="Myron (1250)", icon="engine"),
        ],
    )
    # Only fully-configured agents are offered; there is no Disabled entry (that
    # lives on the Coach selector). Mirrors main.agents_choices after the redesign.
    ctx.register_provider(
        "agents_choices",
        lambda: [
            MenuRow(key="openai", label="OpenAI", icon="agents"),
            MenuRow(key="anthropic", label="Anthropic", icon="agents"),
        ],
    )
    ctx.register_provider(
        "agents",
        lambda: [
            MenuRow(key="openai", label="OpenAI\nSet", icon="agents"),
            MenuRow(key="anthropic", label="Anthropic\nNot set", icon="agents"),
            MenuRow(key="custom", label="Custom\nNot set", icon="agents"),
        ],
    )
    ctx.register_provider(
        "agent_models",
        lambda: [
            MenuRow(key="", label="Default", icon="settings"),
            MenuRow(key="gpt-4o", label="gpt-4o", icon="engine"),
        ],
    )
    ctx.register_provider(
        "installed_engines",
        lambda: [
            MenuRow(key="stockfish", label="stockfish", icon="engine"),
            MenuRow(key="lc0", label="lc0", icon="engine"),
        ],
    )
    ctx._state = state
    ctx._edit_state = edit_state
    return ctx


def _rows(**kwargs):
    """Rows for the Game submenu container (``settings.game``)."""
    ctx = _game_ctx(**kwargs)
    return ctx, build_rows("settings.game", ctx, platform="board", catalog=load_catalog())


def _agents_rows(**kwargs):
    """Rows for the Agents submenu container (``settings.agents``).

    Agents lists every registered agent (one row per agent, via the ``agents``
    provider); selecting one opens its detail submenu. The coach persona and the
    agent selector live under Game. The same fake context backs all containers
    because a context is only a registry of stores/providers/values (the container
    id selects which nodes render).
    """
    ctx = _game_ctx(**kwargs)
    return ctx, build_rows("settings.agents", ctx, platform="board", catalog=load_catalog())


def _detail_rows(**kwargs):
    """Rows for a single agent's detail submenu (``agents.detail``)."""
    ctx = _game_ctx(**kwargs)
    return ctx, build_rows("agents.detail", ctx, platform="board", catalog=load_catalog())


def test_game_menu_rows_and_engine_visibility():
    """Game lists Time Control + Chess960 + Notation + Live Analysis + Coach + Agent selector.

    Why this test exists: the unified Game menu must show Time Control, the
    Chess960 variant toggle, Notation,
    and the analysis toggle always, reveal the Analysis Engine row only when Live
    Analysis is on (via ``visibleWhen``), and always show the Coach persona, Agent
    selector, and Coach Language (their key/model config lives under Agents). How a
    regression manifests: an item is dropped/reordered, the Engine row shows while
    analysis is off (dead row) or never shows while on, or coach_id/coach_provider/
    coach_language go missing.
    """
    _, off_rows = _rows(mode=False)
    assert [r.key for r in off_rows] == [
        "TimeControl", "Chess960", "Notation", "enabled", "coach_id", "coach_provider", "coach_language",
    ]

    _, on_rows = _rows(mode=True)
    assert [r.key for r in on_rows] == [
        "TimeControl", "Chess960", "Notation", "enabled", "engine", "coach_id", "coach_provider", "coach_language",
    ]


def test_agents_menu_lists_every_agent():
    """The Agents submenu expands to one selectable row per registered agent.

    Why this test exists: the Agents tab/submenu must list all agent modules (the
    ``agents`` dynamic provider), each opening its own settings, rather than a fixed
    set of config fields for one active agent. How a regression manifests: the list
    collapses back to per-field rows, or the dynamic node stops expanding so no
    agents appear.
    """
    _, rows = _agents_rows()
    assert [r.key for r in rows] == ["openai", "anthropic", "custom"]
    # Each row carries an item action so selecting it opens that agent's detail.
    assert all(r.action == "agent_select" for r in rows)


def test_agent_selector_row_is_registry_backed_select():
    """Selecting the Agent row in Game opens the ``agents_choices`` list on coach_provider.

    Why this test exists: the Agent selector is a registry-backed select of every
    *configured* agent so user-added agents appear automatically and the pick
    persists to game.coach_provider. How a regression manifests: the row reverts to
    a static optionSet (user agents never appear) or drops its provider/binding so
    the pick has nowhere to persist.
    """
    ctx = _game_ctx()
    node = load_catalog().get_node("coach.provider")
    outcome = dispatch(node, ctx)
    assert outcome.kind == "select"
    assert outcome.provider == "agents_choices"
    assert outcome.store == "game" and outcome.key == "coach_provider"


def test_agent_selector_greyed_when_coach_disabled():
    """The Agent row is greyed (not selectable) while the Coach is Disabled.

    Why this test exists: disabling coaching lives on the Coach selector
    (coach_id ``off``); with no coach there is nothing for an agent to power, so the
    Agent row must render but be disabled via ``enabledWhen`` rather than being a
    live control. How a regression manifests: the enabledWhen gate is dropped and
    the Agent selector stays selectable while coaching is off (a misleading control),
    or the row is hidden entirely so the user cannot see which agent is set.
    """
    _, off_rows = _rows(coach_id="off")
    agent_row = {r.key: r for r in off_rows}["coach_provider"]
    assert agent_row.enabled is False

    _, on_rows = _rows(coach_id="auto")
    agent_row = {r.key: r for r in on_rows}["coach_provider"]
    assert agent_row.enabled is True


def test_coach_selector_row_always_shows_with_resolved_label():
    """The Coach selector row shows in Game regardless of agent, labelled with the pick.

    Why this test exists: the coaching persona is independent of the agent, so the
    Coach row must appear in Game even when the agent is disabled ("none"), and its
    board label must reflect the active coach (Auto vs an explicit pick) via the
    ``coach_selected_label`` compute. How a regression manifests: the row is gated
    behind an agent, or the label shows a literal '{fn:...}' token.
    """
    _, none_rows = _rows(coach_provider="none", coach_id="auto")
    coach_row = {r.key: r for r in none_rows}["coach_id"]
    assert coach_row.label == "Coach\nAuto"

    _, rows = _rows(coach_provider="none", coach_id="myron")
    coach_row = {r.key: r for r in rows}["coach_id"]
    assert coach_row.label == "Coach\nmyron"


def test_coach_selector_row_is_provider_backed_select_on_coach_id():
    """Selecting the Coach row opens the ``coaches`` provider list bound to coach_id.

    Why this test exists: the coach pick is a provider-backed select (Disabled +
    Auto + every registered coach) so the board and web share the roster source and
    persist to game.coach_id. How a regression manifests: the row reverts to a static/optionSet
    select or drops its provider/binding, so custom and user coaches never appear or
    the pick has nowhere to persist.
    """
    ctx = _game_ctx()
    node = load_catalog().get_node("coach.id")
    outcome = dispatch(node, ctx)
    assert outcome.kind == "select"
    assert outcome.provider == "coaches"
    assert outcome.store == "game" and outcome.key == "coach_id"


def test_agent_detail_rows_track_model_kind_and_base_url_requirement():
    """A live-select agent shows API key + model; a custom agent adds free-text model + base URL.

    Why this test exists: the detail submenu must render exactly the fields the
    selected agent needs -- a live model select for agents whose models can be
    listed, a free-text model for those that cannot, and a base URL only for agents
    that require one. How a regression manifests: the base URL row shows for a
    fixed-endpoint agent (dead config), or the free-text/live model rows both show
    (or neither), so the model cannot be set correctly.
    """
    _, builtin_rows = _detail_rows(
        agent_edit_id="openai", agent_model_kind="model", agent_requires_base_url=False
    )
    assert [r.key for r in builtin_rows] == ["agent_api_key", "agent_model"]

    _, custom_rows = _detail_rows(
        agent_edit_id="custom", agent_model_kind="model_text", agent_requires_base_url=True
    )
    # Both model nodes share the key "agent_model"; only the free-text one is visible
    # for a model_text agent, followed by the base URL row it requires.
    assert [r.key for r in custom_rows] == ["agent_api_key", "agent_model", "agent_base_url"]


def test_agent_detail_model_is_provider_backed_select_for_live_agents():
    """A live-select agent's Model row is a provider-backed select on agent_edit.model.

    Why this test exists: the model must come from the live ``agent_models`` list
    (fetched via the agent's key), not a free-text field where a typo/stale id 404s.
    The pick persists through the transient ``agent_edit`` store to the agent's
    namespaced slot. How a regression manifests: the row reverts to text, or its
    select drops the provider/binding so fetched models can't be chosen/saved.
    """
    ctx = _game_ctx(agent_edit_id="openai", agent_model_kind="model")
    node = load_catalog().get_node("agents.detail.model")
    outcome = dispatch(node, ctx)
    assert outcome.kind == "select"
    assert outcome.provider == "agent_models"
    assert outcome.store == "agent_edit" and outcome.key == "model"


def test_agent_detail_api_key_label_hides_the_secret():
    """The API key row's board label shows only whether a key is set, never the key.

    Why this test exists: a secret must not be rendered on the shared board display.
    The label uses the ``agent_key_status`` compute ("Set"/"Not set"). How a
    regression manifests: switching the label to ``{value}`` would print the raw API
    key on-screen.
    """
    _, rows = _detail_rows(agent_edit_id="openai", agent_api_key="sk-secret")
    api_row = {r.key: r for r in rows}["agent_api_key"]
    assert "sk-secret" not in api_row.label
    assert api_row.label == "API Key\nSet"

    _, rows = _detail_rows(agent_edit_id="openai", agent_api_key="")
    api_row = {r.key: r for r in rows}["agent_api_key"]
    assert api_row.label == "API Key\nNot set"


def test_agent_detail_clear_key_row_only_shows_when_a_key_is_stored():
    """The "Clear API Key" detail row appears only when the agent has a stored key.

    Why this test exists: clearing is the only way to remove a saved key (a blank
    save leaves the stored secret unchanged), but offering it when nothing is
    stored is a dead no-op. The row is gated on the derived ``has_api_key`` flag.
    How a regression manifests: the row shows for an agent with no key (a dead
    action), or never shows (no way to clear), so the gating flag is wrong.
    """
    _, with_key = _detail_rows(agent_edit_id="openai", agent_api_key="sk-secret")
    assert "agent_clear_key" in [r.key for r in with_key]

    _, without_key = _detail_rows(agent_edit_id="openai", agent_api_key="")
    assert "agent_clear_key" not in [r.key for r in without_key]


def test_agent_detail_clear_key_row_is_an_action_node():
    """The Clear API Key node is an action node wired to clear_agent_api_key.

    Why this test exists: the board runs an action node's named action on select;
    this row's whole purpose is to invoke the clear handler. How a regression
    manifests: the node loses its ``action`` (selecting it does nothing) or names a
    different handler, so the key can't be removed from the board.
    """
    node = load_catalog().get_node("agents.detail.clear_key")
    assert node["type"] == "action"
    assert node["action"] == "clear_agent_api_key"


def test_agent_detail_model_label_reads_default_when_blank():
    """A blank agent model renders as "Model\\nDefault", not an empty line.

    Why this test exists: blank means "use the agent default"; the board label must
    communicate that. How a regression manifests: the label shows "Model\\n"
    (trailing blank) so the user can't tell what will be used.
    """
    _, rows = _detail_rows(agent_edit_id="openai", agent_model="")
    model_row = {r.key: r for r in rows}["agent_model"]
    assert model_row.label == "Model\nDefault"

    _, rows = _detail_rows(agent_edit_id="openai", agent_model="gpt-4o")
    model_row = {r.key: r for r in rows}["agent_model"]
    assert model_row.label == "Model\ngpt-4o"


def test_time_control_row_label_and_icon_track_value():
    """The Time Control row shows a concise label and a value-dependent icon.

    Why this test exists: untimed must read "Time\\nDisabled" with the empty timer
    icon, and a set value "Time\\nN min" with the checked timer icon, from the
    catalog's computed label and state-mapped icon. How a regression manifests: the
    icon stops tracking whether a clock is set, or the label shows the verbose
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

    Why this test exists: the toggle must render the on/off icon and actually write
    analysis_mode through the store. How a regression manifests: the icon desyncs
    from the flag, or selecting the row no longer persists the change.
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

    Why this test exists: the analysis engine pick comes from the ``installed_engines``
    provider and is written to analysis.engine -- the same provider/value pattern as
    the player Engine/ELO pickers. How a regression manifests: the row reverts to a
    (now-removed) action, or the outcome drops the provider/binding so the analysis
    engine can't be chosen on the board.
    """
    ctx = _game_ctx(mode=True)
    node = load_catalog().get_node("analysis.engine")
    outcome = dispatch(node, ctx)
    assert outcome.kind == "select"
    assert outcome.provider == "installed_engines"
    assert outcome.store == "analysis" and outcome.key == "engine"
