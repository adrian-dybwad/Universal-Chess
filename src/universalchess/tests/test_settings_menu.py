"""Tests for the top-level Settings list rendered through the menu engine.

Background / why these tests exist
----------------------------------
The Settings list is now rendered from the shared ``settings`` catalog container
via the engine (see ``main._build_settings_entries``); the bespoke
``create_settings_entries`` builder/override was removed. The surrounding board
loop still dispatches by entry key, so these tests pin (a) the full ordered key
set the dispatch relies on -- Game (grouping Time Control + Live Analysis, matching
the web Game tab), with Display and Sound as independent siblings and
Chromecast/About absent -- and (b) the runtime-resolved Players summary (a
computed label). A fake context supplies the same compute main.py registers, so
the catalog wiring is exercised without board hardware.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows

DISPLAY_KEY = "Display"
SOUND_KEY = "Sound"


def _settings_ctx(*, players_summary="Human\nvs Stockfish"):
    """Context mirroring main._build_settings_context for the Settings list.

    ``players_summary`` is the computed Players label; the other rows are static
    submenu labels, so no value store is needed at this level.
    """
    ctx = BoardMenuContext()
    ctx.register_value("players_summary", lambda node: players_summary)
    return ctx


def _rows(**kwargs):
    return build_rows("settings", _settings_ctx(**kwargs), platform="board", catalog=load_catalog())


def test_settings_full_entry_order():
    """Settings lists play setup first, then appearance, then device groups.

    Why this test exists: the dispatch loop keys off these entry keys, and this
    is the order the web's Settings tabs use -- both surfaces read it from the
    catalog now, so the board list and the web tab strip cannot diverge. Time
    Control and Live Analysis live inside Game and the AI coach/agent settings
    inside Agents (not at top level), Chromecast moved into Connectivity and
    About into System, so none appear here. Engines is a top-level row rather
    than a System child because that is where the web puts its tab.

    How a regression manifests: an item is dropped or reordered,
    TimeControl/Chromecast/About reappears, or Engines sinks back into System,
    changing this exact list -- and because the web derives its tabs from the
    same array, a reorder here silently moves the web tabs too.
    """
    keys = [r.key for r in _rows()]
    assert keys == [
        "Players",
        "Game",
        "Positions",
        DISPLAY_KEY,
        SOUND_KEY,
        "Connectivity",
        "Engines",
        "Agents",
        "System",
    ]


def test_game_row_is_static_submenu():
    """The Game row is a plain submenu label grouping time/analysis settings.

    Why this test exists: Game replaced the former top-level Time Control row and
    must route into the Game submenu (key "Game") rather than carrying a bound
    value. How a regression manifests: the key reverts to TimeControl (breaking
    the new dispatch branch) or the label/icon changes so the row is
    unrecognisable.
    """
    row = {r.key: r for r in _rows()}["Game"]
    assert (row.label, row.icon) == ("Game", "timer_checked")


def test_display_and_sound_are_independent_entries():
    """Display and Sound render as two siblings with their agreed label/icon.

    Why this test exists: they were split out of the former combined
    "Display & Sound" item and must not collapse back. How a regression
    manifests: a 'DisplaySound' key reappears, or a label/icon changes so the row
    is unrecognisable.
    """
    by_key = {r.key: r for r in _rows()}
    assert "DisplaySound" not in by_key
    assert (by_key[DISPLAY_KEY].label, by_key[DISPLAY_KEY].icon) == ("Display", "display")
    assert (by_key[SOUND_KEY].label, by_key[SOUND_KEY].icon) == ("Sound", "sound")


def test_players_row_shows_computed_summary():
    """The Players row's board label is the computed P1-vs-P2 summary.

    Why this test exists: the summary is a computed token ({fn:players_summary})
    rather than a static label, replacing the old runtime override. How a
    regression manifests: the row shows the literal "Players"/token instead of
    the live summary, so the user can't see the configured matchup at a glance.
    """
    row = {r.key: r for r in _rows(players_summary="Stockfish\nvs Human")}["Players"]
    assert row.label == "Stockfish\nvs Human"
