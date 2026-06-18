"""Tests for the top-level Settings list rendered through the menu engine.

Background / why these tests exist
----------------------------------
The Settings list is now rendered from the shared ``settings`` catalog container
via the engine (see ``main._build_settings_entries``); the bespoke
``create_settings_entries`` builder/override was removed. The surrounding board
loop still dispatches by entry key, so these tests pin (a) the full ordered key
set the dispatch relies on, with Display and Sound as independent siblings and
Chromecast/About absent, and (b) the two runtime-resolved rows: the Players
summary (a computed label) and the Time Control row (concise label + the
value-dependent timer icon). A fake context supplies the same store/computes
main.py registers, so the catalog wiring is exercised without board hardware.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows

DISPLAY_KEY = "Display"
SOUND_KEY = "Sound"


def _settings_ctx(*, time_control=0, players_summary="Human\nvs Stockfish"):
    """Context mirroring main._build_settings_context for the Settings list.

    The ``game`` store reports time_control (driving the Time Control row's icon),
    ``players_summary`` is the computed Players label, and ``time_control`` is the
    concise computed Time Control label ("Disabled"/"N min").
    """
    state = {"time_control": time_control}

    ctx = BoardMenuContext()
    ctx.register_store("game", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    ctx.register_value("players_summary", lambda node: players_summary)
    ctx.register_value(
        "time_control",
        lambda node: "Disabled" if state["time_control"] == 0 else f"{state['time_control']} min",
    )
    return ctx


def _rows(**kwargs):
    return build_rows("settings", _settings_ctx(**kwargs), platform="board", catalog=load_catalog())


def test_settings_full_entry_order():
    """Settings lists setup first, then appearance, then device groups, in order.

    Why this test exists: the dispatch loop keys off these entry keys in this
    order (Players, Time Control, the Display/Sound appearance pair, Positions,
    then Connectivity/System); Chromecast moved into Connectivity and About into
    System, so neither appears here. How a regression manifests: an item is
    dropped/reordered or Chromecast/About reappears, changing this exact list.
    """
    keys = [r.key for r in _rows()]
    assert keys == [
        "Players",
        "TimeControl",
        DISPLAY_KEY,
        SOUND_KEY,
        "Positions",
        "Connectivity",
        "System",
    ]


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


def test_time_control_row_label_and_icon_track_value():
    """The Time Control row shows a concise label and a value-dependent icon.

    Why this test exists: untimed must read "Time\\nDisabled" with the empty
    timer icon, and a set value "Time\\nN min" with the checked timer icon -- the
    behavior the removed override produced, now from the catalog's computed label
    and state-mapped icon. How a regression manifests: the icon stops tracking
    whether a clock is set, or the label shows the verbose option text.
    """
    untimed = {r.key: r for r in _rows(time_control=0)}["TimeControl"]
    assert untimed.label == "Time\nDisabled"
    assert untimed.icon == "timer"

    timed = {r.key: r for r in _rows(time_control=5)}["TimeControl"]
    assert timed.label == "Time\n5 min"
    assert timed.icon == "timer_checked"
