"""Tests for the Players menus, now driven by the shared menu engine.

Background / why these tests exist
----------------------------------
The Players top-level menu (settings.players) and the per-player detail menu
(settings.player_detail) were migrated off bespoke builders onto the data-driven
engine. Player 1 and Player 2 share one ``field.player.*`` node set; each detail
menu binds the engine's generic "player" store to that player and flags
``has_color`` so the Color row shows only for Player 1. Type-conditional rows
(name/engine/elo/hand-brain/lichess), the color/type radio lists with per-option
icons, the Hand+Brain cycle, the computed player summaries, and the Start Game
token all come from the catalog through the engine. These tests build from the
*real* catalog with dict-backed stores and recorded actions, pinning the same
guarantees the deleted ``players_menu``/``hand_brain_menu`` modules enforced.
"""

from universalchess.managers.menu import MenuResult, MenuSelection
from universalchess.menus.board_context import BoardMenuContext, run_engine_menu
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import MenuRow, build_rows, dispatch

_EXIT_RESULTS = {MenuResult.BACK, MenuResult.SHUTDOWN, MenuResult.HELP}


class _FakeMenuManager:
    """Drives run_menu_loop from a scripted list of selection keys.

    Mirrors MenuManager.run_menu_loop (break/exit short-circuit, then handler)
    so the adapter is tested without a display. Records the entries shown on each
    iteration (including inner option lists) for assertions.
    """

    def __init__(self, script):
        self._script = list(script)
        self.shown = []

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0, track_selection=True, on_index_change=None):
        while True:
            self.shown.append(build_entries())
            selection = MenuSelection.from_key(self._script.pop(0))
            if selection.key == "REFRESH":
                continue
            if selection.is_break or selection.result_type in _EXIT_RESULTS:
                return selection
            result = handle_selection(selection)
            if result is not None:
                return result


def _player_state(**overrides):
    base = {
        "color": "white",
        "type": "human",
        "name": "Alice",
        "engine": "stockfish",
        "elo": "1500",
        "think_time": 5,
        "hand_brain_mode": "normal",
        "account": "",
    }
    base.update(overrides)
    return base


def _detail_ctx(state, *, has_color=True, calls=None):
    """Board context for one player's detail menu (store "player").

    Mirrors main._build_player_detail_context: a dict-backed player store plus
    the virtual ``has_color`` key, the installed-engine and per-engine ELO list
    providers backing the Engine/ELO selects, and the keyboard-name/Lichess
    actions recorded so their invocation can be asserted without a real display.

    The store setter mirrors main's ELO-reset cascade: ELO levels are
    engine-specific, so changing the engine resets ELO to Default (a prior
    engine's level is meaningless for another engine).
    """
    calls = calls if calls is not None else []

    def player_get(key):
        if key == "has_color":
            return has_color
        return state[key]

    def player_set(key, value):
        state[key] = value
        if key == "engine":
            state["elo"] = "Default"

    ctx = BoardMenuContext()
    ctx.register_store("player", player_get, player_set)
    game = {"lichess_rated": False, "lichess_use_dev": False}
    ctx.register_store("game", lambda key: game[key], lambda key, value: game.__setitem__(key, value))
    ctx.register_provider(
        "installed_engines",
        lambda: [
            MenuRow(key="stockfish", label="stockfish", icon="engine"),
            MenuRow(key="maia", label="maia", icon="engine"),
        ],
    )
    ctx.register_provider(
        "engine_levels",
        lambda: [
            MenuRow(key="Default", label="Default", icon="elo"),
            MenuRow(key="1500", label="1500", icon="elo"),
        ],
    )
    ctx.register_provider(
        "player_accounts",
        lambda: [
            MenuRow(key="", label="Default account", icon="lichess"),
            MenuRow(key="magnusc", label="MagnusC", icon="lichess"),
        ],
    )
    ctx.register_value(
        "player_account",
        lambda node: "MagnusC" if state.get("account") == "magnusc" else "Default",
    )
    # Mirror main._build_player_detail_context: the Name row shows the stored
    # name, or the per-slot default ("Player N") when unset. has_color marks
    # Player 1 (the only slot that picks a color), so it derives the slot number.
    player_num = 1 if has_color else 2
    ctx.register_value(
        "player_name",
        lambda node: state.get("name") or f"Player {player_num}",
    )
    ctx.register_action("edit_name", lambda: calls.append("edit_name") or None)
    ctx._recorded_calls = calls
    return ctx


def _detail_rows(state, *, has_color=True):
    return build_rows(
        "settings.player_detail",
        _detail_ctx(state, has_color=has_color),
        platform="board",
        catalog=load_catalog(),
    )


# -- detail menu: type-conditional row sets ------------------------------------


def test_human_shows_color_type_name_only():
    """A human player exposes Color, Type, and Name (no Engine/ELO/H+B/Lichess).

    Why this test exists: row visibility is gated by ``visibleWhen`` on the player
    type. Engine/ELO must NOT show for a human -- those fields drive engine and
    hand-brain players only; a human's hints use the global analysis engine (see
    DisplayManager.get_hint_move, which reads the analysis handle, not the player's
    engine/elo). This previously leaked Engine/ELO onto the board for a human while
    the web already hid them, so the two UIs disagreed. How a regression manifests:
    "field.player.engine"/"field.player.elo" reappear in this id list for a human.
    """
    ids = [r.node["id"] for r in _detail_rows(_player_state(type="human"))]
    assert ids == [
        "field.player.color",
        "field.player.type",
        "field.player.name",
    ]


def test_engine_hides_name_and_hand_brain_rows():
    """An engine player drops Name and Hand+Brain, keeping Engine, ELO, Think Time.

    How a regression manifests: the Name row (human-only) or the Hand+Brain mode
    row (hand_brain-only) reappears for an engine player, or the Think Time row
    (engine/hand_brain-only) stops appearing after Engine/ELO.
    """
    ids = [r.node["id"] for r in _detail_rows(_player_state(type="engine"))]
    assert ids == [
        "field.player.color",
        "field.player.type",
        "field.player.engine",
        "field.player.elo",
        "field.player.think_time",
    ]


def test_think_time_row_visibility_and_board_label():
    """Think Time shows for engine/hand_brain (with its {value} label), not human/lichess.

    Why this test exists: per-move think time only applies when a player is driven
    by an engine (engine or hand_brain); a human/lichess slot has no engine to
    time, so the row is gated to ["engine", "hand_brain"]. The board label uses
    the "Think\\n{value}" template resolved through the ``think_time`` option set
    (stored int 5 -> "5 sec"). How a regression manifests: the row leaks onto a
    human/lichess slot, disappears for an engine slot, or the label shows the raw
    value ("Think\\n5") or the long web label instead of "Think\\n5 sec".
    """
    engine_rows = {r.node["id"]: r for r in _detail_rows(_player_state(type="engine", think_time=5))}
    assert engine_rows["field.player.think_time"].label == "Think\n5 sec"

    hb_ids = [r.node["id"] for r in _detail_rows(_player_state(type="hand_brain"))]
    assert "field.player.think_time" in hb_ids

    human_ids = [r.node["id"] for r in _detail_rows(_player_state(type="human"))]
    assert "field.player.think_time" not in human_ids
    lichess_ids = [r.node["id"] for r in _detail_rows(_player_state(type="lichess"))]
    assert "field.player.think_time" not in lichess_ids


def test_selecting_think_time_writes_chosen_option_value():
    """Selecting Think Time opens the option set and writes the chosen value.

    Why this test exists: think_time is a static-optionSet ``select`` bound to
    player.think_time; drilling in must open the seconds list and persist the
    pick. The board writes the option value verbatim (a string, e.g. "10"); the
    ``int`` coercion to match the declared field type is enforced by
    PlayerSettings.set (see test_player_config_rebuild.test_set_think_time_
    coerces_string_to_int), not by the menu engine. How a regression manifests:
    Think Time no longer opens the list, or the chosen value is not written.
    """
    state = _player_state(type="engine", think_time=5)
    ctx = _detail_ctx(state)
    mm = _FakeMenuManager(["field.player.think_time", "10", "BACK"])
    run_engine_menu("settings.player_detail", ctx, mm, catalog=load_catalog())
    assert state["think_time"] == "10"


def test_hand_brain_shows_mode_row_with_checkbox_icon():
    """Hand+Brain adds the mode row (cycle) marked by its checkbox state icon.

    Why: Reverse mode is toggled in place; the row's icon reflects the current
    mode (checked when reverse). How a regression manifests: the mode row is
    missing for hand_brain, or its icon does not track the reverse/normal state.
    """
    by_id = {r.node["id"]: r for r in _detail_rows(_player_state(type="hand_brain", hand_brain_mode="reverse"))}
    assert "field.player.hand_brain_mode" in by_id
    mode_row = by_id["field.player.hand_brain_mode"]
    assert mode_row.label == "Reverse"
    assert mode_row.icon == "checkbox_checked"

    normal = {r.node["id"]: r for r in _detail_rows(_player_state(type="hand_brain", hand_brain_mode="normal"))}
    assert normal["field.player.hand_brain_mode"].icon == "checkbox_empty"


def test_lichess_shows_color_type_account_and_rated():
    """A Lichess player exposes Color, Type, the Account picker, and Rated.

    Why this test exists: an online (Lichess) slot binds to a specific saved
    account via the account picker (``field.player.account``), which is gated to
    online player types. Host/dev and the Lichess lobby are not per-slot: they
    live under Players → Lichess Settings. How a regression manifests: the
    engine/ELO/name rows (non-Lichess) leak in, the account picker is hidden
    for an online player, or lichess.dev / Lichess Settings reappear on the slot.
    """
    ids = [r.node["id"] for r in _detail_rows(_player_state(type="lichess"))]
    assert ids == [
        "field.player.color",
        "field.player.type",
        "field.player.account",
        "field.player.lichess_rated",
    ]


def test_account_row_shows_bound_account_identity_via_compute():
    """The Account row's board label resolves to the bound account's identity.

    Why: the picker stores the account id, but the row must show the human
    identity (or "Default" when unbound) so a user recognises which account the
    slot plays as. How a regression manifests: the row shows the raw id, an empty
    string, or "Default" even when an account is bound.
    """
    bound = next(
        r for r in _detail_rows(_player_state(type="lichess", account="magnusc"))
        if r.node["id"] == "field.player.account"
    )
    assert bound.label == "Account\nMagnusC"
    unbound = next(
        r for r in _detail_rows(_player_state(type="lichess", account=""))
        if r.node["id"] == "field.player.account"
    )
    assert unbound.label == "Account\nDefault"


def test_account_row_hidden_for_offline_player():
    """The account picker is hidden for a non-online (human) player.

    How a regression manifests: ``field.player.account`` appears for a human/
    engine slot, offering an account binding that has no meaning offline.
    """
    ids = [r.node["id"] for r in _detail_rows(_player_state(type="human"))]
    assert "field.player.account" not in ids


def test_player2_detail_hides_color_row():
    """Player 2's detail omits the Color row (has_color is False).

    Why this test exists: Player 1 picks a color and Player 2 always takes the
    opposite, so only Player 1 shows the Color row -- driven by the virtual
    ``has_color`` flag the per-player context sets. How a regression manifests:
    the Color row appears for Player 2, implying both could pick the same color.
    """
    ids = [r.node["id"] for r in _detail_rows(_player_state(type="human"), has_color=False)]
    assert "field.player.color" not in ids
    assert ids[0] == "field.player.type"


# -- detail menu: board labels -------------------------------------------------


def test_detail_rows_use_board_abbreviations_and_bound_values():
    """Detail rows render e-paper labels with the bound value substituted.

    Why: the board uses the optional boardLabel templates ("Type\\n{value}",
    "Color\\n{value}", "Engine\\n{value}", "ELO\\n{value}"), where {value}
    resolves through the option set (Type/Color) or the raw value (Engine/ELO).
    How a regression manifests: a row shows the long web label or loses its
    current value.
    """
    by_id = {
        r.node["id"]: r
        for r in _detail_rows(_player_state(type="engine", color="black", engine="maia", elo="1900"))
    }
    assert by_id["field.player.type"].label == "Type\nEngine"
    assert by_id["field.player.color"].label == "Color\nBlack"
    assert by_id["field.player.engine"].label == "Engine\nmaia"
    assert by_id["field.player.elo"].label == "ELO\n1900"


def test_elo_row_shows_provider_label_not_raw_stored_value():
    """The ELO parent row renders the provider's label, not the raw stored value.

    Why this test exists: ``field.player.elo`` is a provider-backed select whose
    submenu shows the ``engine_levels`` provider labels (an uncapped "Default"
    section displays as "Default (Unlimited)"). The parent "ELO\\n{value}" button
    must resolve the same label source so it matches the submenu. How a
    regression manifests: the stored value "Default" is shown verbatim
    ("ELO\\nDefault") on the parent button while drilling in shows
    "Default (Unlimited)".
    """
    calls = []
    ctx = _detail_ctx(_player_state(type="engine", elo="Default"), calls=calls)
    # Override the provider so the Default section is labeled like Stockfish.
    ctx.register_provider(
        "engine_levels",
        lambda: [
            MenuRow(key="Default", label="Default (Unlimited)", icon="elo"),
            MenuRow(key="1500", label="1500", icon="elo"),
        ],
    )
    rows = build_rows("settings.player_detail", ctx, platform="board", catalog=load_catalog())
    by_id = {r.node["id"]: r for r in rows}
    assert by_id["field.player.elo"].label == "ELO\nDefault (Unlimited)"


def test_name_row_shows_entered_name_via_compute_token():
    """The Name row renders the stored name through the {fn:player_name} token.

    How a regression manifests: the boardLabel stops substituting the computed
    name, so the row shows a literal '{fn:player_name}' or a blank name.
    """
    by_id = {r.node["id"]: r for r in _detail_rows(_player_state(type="human", name="Bobby"))}
    assert by_id["field.player.name"].label == "Name\nBobby"


def test_unset_name_shows_per_slot_default_without_fabricating_in_store():
    """An empty name renders the per-slot default ("Name\\nPlayer 1") via compute.

    Why this test exists: the default is per-slot ("Player 1"/"Player 2"), so it
    cannot be a single shared catalog ``valueDefault``; it is computed by the
    per-slot context ({fn:player_name}) rather than faked in the value store -- so
    the store (and thus the keyboard prefill and the game's PGN name) keep seeing
    the real empty value. The test ctx store returns the raw "" here; the rendered
    label must still read "Name\\nPlayer 1". How a regression manifests: the store
    is back to returning a fabricated name (the prefill/PGN would then wrongly
    show it), or the default is dropped and the row shows a blank "Name\\n".
    """
    state = _player_state(type="human", name="")
    # has_color=True marks Player 1, so the computed default is "Player 1".
    by_id = {r.node["id"]: r for r in _detail_rows(state, has_color=True)}
    assert by_id["field.player.name"].label == "Name\nPlayer 1"
    # The store itself stays truthful: the underlying value is still empty.
    assert state["name"] == ""


def test_unset_name_default_is_player_two_for_second_slot():
    """Player 2's empty Name row renders "Name\\nPlayer 2" (per-slot default).

    Why this test exists: the default is derived from the slot, so the two slots
    must differ. has_color=False marks Player 2. How a regression manifests: both
    slots fall back to the same literal (e.g. a shared "Player 1"), proving the
    default is not slot-aware.
    """
    state = _player_state(type="human", name="")
    by_id = {r.node["id"]: r for r in _detail_rows(state, has_color=False)}
    assert by_id["field.player.name"].label == "Name\nPlayer 2"
    assert state["name"] == ""


# -- detail menu: dispatch / persistence ---------------------------------------


def test_color_list_marks_active_with_star_and_keeps_piece_icons():
    """The Color list keeps per-option piece icons and stars the active color.

    Why this test exists: option sets that carry their own icon (color -> white/
    black piece) are rendered with that icon and the active row is marked with a
    leading "* " (not a radio glyph). How a regression manifests: the radio glyph
    overrides the piece icon, or the active color is unmarked/mismarked.
    """
    state = _player_state(color="white")
    # Open Color, then exit the inner list via BACK, then exit the detail menu.
    mm = _FakeMenuManager(["field.player.color", "BACK", "BACK"])
    run_engine_menu("settings.player_detail", _detail_ctx(state), mm, catalog=load_catalog())

    # shown[0] is the detail rows; shown[1] is the opened color option list.
    color_list = {e.key: e for e in mm.shown[1]}
    assert color_list["white"].label == "* White"
    assert color_list["white"].icon_name == "white_piece"
    assert color_list["black"].label == "Black"
    assert color_list["black"].icon_name == "black_piece"


def test_selecting_color_persists_choice():
    """Picking a color in the list writes it to the player store.

    How a regression manifests: the select does not persist (color unchanged) so
    the board cannot change a player's color.
    """
    state = _player_state(color="white")
    mm = _FakeMenuManager(["field.player.color", "black", "BACK"])
    run_engine_menu("settings.player_detail", _detail_ctx(state), mm, catalog=load_catalog())
    assert state["color"] == "black"


def test_selecting_type_persists_choice():
    """Picking a player type in the list writes it to the player store.

    How a regression manifests: the type select is inert, so a player cannot be
    switched between human/engine/hand_brain/lichess from the board.
    """
    state = _player_state(type="human")
    mm = _FakeMenuManager(["field.player.type", "engine", "BACK"])
    run_engine_menu("settings.player_detail", _detail_ctx(state), mm, catalog=load_catalog())
    assert state["type"] == "engine"


def test_hand_brain_mode_cycles_between_normal_and_reverse():
    """Selecting the Hand+Brain row cycles the mode in place (normal <-> reverse).

    Why: the mode row is a cycle, not a sub-list; one press flips it. How a
    regression manifests: the mode never changes, or advances to an unexpected
    value instead of toggling.
    """
    state = _player_state(type="hand_brain", hand_brain_mode="normal")
    # Cycle once (normal -> reverse), again (reverse -> normal), then exit.
    mm = _FakeMenuManager(
        ["field.player.hand_brain_mode", "field.player.hand_brain_mode", "BACK"]
    )
    run_engine_menu("settings.player_detail", _detail_ctx(state), mm, catalog=load_catalog())
    assert state["hand_brain_mode"] == "normal"

    state2 = _player_state(type="hand_brain", hand_brain_mode="normal")
    mm2 = _FakeMenuManager(["field.player.hand_brain_mode", "BACK"])
    run_engine_menu("settings.player_detail", _detail_ctx(state2), mm2, catalog=load_catalog())
    assert state2["hand_brain_mode"] == "reverse"


def test_name_row_invokes_edit_name_action():
    """Selecting the Name row runs the board ``edit_name`` action (keyboard).

    Why this test exists: ``field.player.name`` is a ``text`` node edited on the
    board via its named action. How a regression manifests: the text dispatch
    stops routing to the action, so the keyboard never opens.
    """
    state = _player_state(type="human")
    calls = []
    ctx = _detail_ctx(state, calls=calls)
    mm = _FakeMenuManager(["field.player.name", "BACK"])
    run_engine_menu("settings.player_detail", ctx, mm, catalog=load_catalog())
    assert calls == ["edit_name"]


def test_engine_row_opens_provider_select_and_persists_choice():
    """Selecting Engine opens the installed-engines list and writes the pick.

    Why this test exists: the engine picker was migrated from an imperative
    ``action`` sub-flow to a provider-backed ``select`` (options from the
    ``installed_engines`` provider, written to player.engine). How a regression
    manifests: Engine no longer opens the runtime list, or the chosen engine is
    not persisted so the board can't change a player's engine.
    """
    state = _player_state(type="engine", engine="stockfish", elo="1500")
    ctx = _detail_ctx(state)
    mm = _FakeMenuManager(["field.player.engine", "maia", "BACK"])
    run_engine_menu("settings.player_detail", ctx, mm, catalog=load_catalog())
    assert state["engine"] == "maia"


def test_changing_engine_resets_elo_to_default():
    """Picking a different engine resets ELO to Default via the store cascade.

    Why this test exists: ELO levels are engine-specific (a maia level is invalid
    for stockfish), so the player store resets ELO whenever the engine changes --
    the same cascade the deleted handle_engine_selection performed inline. How a
    regression manifests: a stale ELO from the previous engine survives, so the
    new engine is asked for a level it does not define.
    """
    state = _player_state(type="engine", engine="stockfish", elo="1900")
    ctx = _detail_ctx(state)
    mm = _FakeMenuManager(["field.player.engine", "maia", "BACK"])
    run_engine_menu("settings.player_detail", ctx, mm, catalog=load_catalog())
    assert state["engine"] == "maia"
    assert state["elo"] == "Default"


def test_elo_row_opens_provider_select_and_persists_choice():
    """Selecting ELO opens the per-engine levels list and writes the pick.

    Why this test exists: the ELO picker was migrated to a provider-backed
    ``select`` sourced from ``engine_levels`` (scoped to the current engine),
    written to player.elo. How a regression manifests: ELO no longer opens the
    runtime list, or the chosen level is not persisted.
    """
    state = _player_state(type="engine", engine="stockfish", elo="Default")
    ctx = _detail_ctx(state)
    mm = _FakeMenuManager(["field.player.elo", "1500", "BACK"])
    run_engine_menu("settings.player_detail", ctx, mm, catalog=load_catalog())
    assert state["elo"] == "1500"


# -- top-level Players menu ----------------------------------------------------


def _players_ctx(p1, p2, *, calls=None, p1_summary="P1", p2_summary="P2"):
    """Top-level Players context (stores player1/player2 + summaries + actions)."""
    calls = calls if calls is not None else []
    ctx = BoardMenuContext()
    ctx.register_store("player1", lambda k: p1[k], lambda k, v: p1.__setitem__(k, v))
    ctx.register_store("player2", lambda k: p2[k], lambda k, v: p2.__setitem__(k, v))
    ctx.register_value("player1_summary", lambda node: p1_summary)
    ctx.register_value("player2_summary", lambda node: p2_summary)
    ctx.register_action("open_player1", lambda: calls.append("open_player1") or None)
    ctx.register_action("open_player2", lambda: calls.append("open_player2") or None)
    ctx.register_action("open_accounts", lambda: calls.append("open_accounts") or None)
    ctx.register_action("lichess", lambda: calls.append("lichess") or None)
    ctx.register_action("start_game", lambda: "START_GAME")
    game = {"lichess_use_dev": False, "lichess_rated": False}
    ctx.register_store(
        "game", lambda k: game[k], lambda k, v: game.__setitem__(k, v)
    )
    ctx._recorded_calls = calls
    ctx._game = game
    return ctx


def _players_rows(p1, p2, **kwargs):
    return build_rows("settings.players", _players_ctx(p1, p2, **kwargs), platform="board", catalog=load_catalog())


def test_top_level_lists_two_players_lichess_then_start():
    """Players lists Player 1, Player 2, Lichess Settings, then Start.

    Why: Lichess Settings holds credentials and the lobby, not a per-slot row.
    How a regression manifests: Lichess Settings is missing/reordered, Accounts
    returns to the top level, or Start Game is no longer last.
    """
    ids = [r.node["id"] for r in _players_rows(_player_state(), _player_state())]
    assert ids == [
        "players.player1",
        "players.player2",
        "players.lichess",
        "players.start",
    ]


def test_lichess_settings_contains_accounts_and_play():
    """Lichess Settings holds the credentials manager and the Play lobby action.

    Why: each Lichess login is a server:user credential, listed here; Play opens
    the lobby. How a regression manifests: a host toggle returns, Play is
    missing, or Accounts is not on this page.
    """
    ids = [
        r.node["id"]
        for r in build_rows(
            "players.lichess",
            _players_ctx(_player_state(), _player_state()),
            platform="board",
            catalog=load_catalog(),
        )
    ]
    assert ids == ["players.accounts", "players.lichess.play"]


def test_lichess_play_row_dispatches_lichess_action():
    """Selecting Play on Lichess Settings runs the existing lichess lobby.

    Why: the lobby used to be the whole 'Lichess Settings' row on a player
    slot. It must still open from the new page. How a regression manifests:
    dispatch action is wrong or the players context never registered lichess.
    """
    calls = []
    ctx = _players_ctx(_player_state(), _player_state(), calls=calls)
    outcome = dispatch(load_catalog().get_node("players.lichess.play"), ctx)
    assert outcome.kind == "action" and outcome.action == "lichess"
    assert calls == ["lichess"]


def test_accounts_row_dispatches_open_accounts():
    """Selecting Accounts runs open_accounts (same handler as before the move).

    Why: the row must open the multi-account manager, not a dead action or a
    player detail. How a regression manifests: dispatch action is wrong or the
    context never registered open_accounts.
    """
    calls = []
    ctx = _players_ctx(_player_state(), _player_state(), calls=calls)
    outcome = dispatch(load_catalog().get_node("players.accounts"), ctx)
    assert outcome.kind == "action" and outcome.action == "open_accounts"
    assert calls == ["open_accounts"]


def test_player_rows_render_computed_summaries():
    """Player rows embed their computed summary via the {fn:...} label token.

    Why this test exists: the summary (engine name, "H+B N (White)", etc.) is
    composed per platform and injected through ``ctx.compute``; the catalog label
    supplies only the surrounding "Player N\\n{fn:...}" template. How a regression
    manifests: the {fn:...} token is left literal or not substituted.
    """
    rows = {
        r.node["id"]: r
        for r in _players_rows(
            _player_state(), _player_state(), p1_summary="Stockfish (White)", p2_summary="Human"
        )
    }
    assert rows["players.player1"].label == "Player 1\nStockfish (White)"
    assert rows["players.player2"].label == "Player 2\nHuman"


def test_player1_icon_tracks_its_color():
    """Player 1's row icon is the white/black piece matching its color.

    Why: the icon is a state map bound to player1.color. How a regression
    manifests: the icon stops tracking color (always one piece), so the row no
    longer signals which color Player 1 has.
    """
    white = {r.node["id"]: r for r in _players_rows(_player_state(color="white"), _player_state())}
    black = {r.node["id"]: r for r in _players_rows(_player_state(color="black"), _player_state())}
    assert white["players.player1"].icon == "white_piece"
    assert black["players.player1"].icon == "black_piece"


def test_selecting_player_opens_its_detail_action():
    """Selecting a player row runs that player's open action and stays in the menu.

    How a regression manifests: the open action is not invoked (detail never
    opens), or returns a signal that wrongly exits the Players menu.
    """
    calls = []
    ctx = _players_ctx(_player_state(), _player_state(), calls=calls)
    mm = _FakeMenuManager(["players.player1", "players.player2", "BACK"])
    result = run_engine_menu("settings.players", ctx, mm, catalog=load_catalog())
    assert calls == ["open_player1", "open_player2"]
    assert result.is_back()


def test_start_game_row_returns_start_token():
    """Selecting Start Game exits the Players menu with the START_GAME token.

    Why this test exists: the Settings handler turns this token into a new game.
    How a regression manifests: the action's signal is swallowed (menu just
    redraws) so Start Game never launches a game.
    """
    ctx = _players_ctx(_player_state(), _player_state())
    mm = _FakeMenuManager(["players.start"])
    result = run_engine_menu("settings.players", ctx, mm, catalog=load_catalog())
    assert result is not None
    assert result.key == "START_GAME"


def test_break_from_open_action_unwinds_players_menu():
    """A break result from opening a player detail propagates out of the menu.

    Why: a game started from a player's sub-menu must unwind every menu. The open
    action forwards a break token, which the engine's action loop turns back into
    a break MenuSelection. How a regression manifests: the break is dropped, so
    the board stays in the Players menu instead of entering the game.
    """
    ctx = BoardMenuContext()
    ctx.register_store("player1", lambda k: _player_state()[k], lambda k, v: None)
    ctx.register_store("player2", lambda k: _player_state()[k], lambda k, v: None)
    ctx.register_value("player1_summary", lambda node: "P1")
    ctx.register_value("player2_summary", lambda node: "P2")
    ctx.register_action("open_player1", lambda: "PLAY")  # a break token
    ctx.register_action("open_player2", lambda: None)
    ctx.register_action("start_game", lambda: "START_GAME")

    mm = _FakeMenuManager(["players.player1"])
    result = run_engine_menu("settings.players", ctx, mm, catalog=load_catalog())
    assert result is not None
    assert result.is_break
