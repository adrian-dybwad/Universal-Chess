"""Tests for the "Your Queen" warning toggle (Settings > Game > Alerts).

Why these tests exist
---------------------
The YOUR QUEEN warning ("the side to move's own queen is attacked") is shown on
three independent surfaces, each of which used to re-derive the rule itself:

1. the e-paper AlertWidget text + LED flash, driven by
   ``ChessGameState.on_queen_threat``;
2. the three-color red highlight, derived in
   ``ChessBoardWidget._compute_red_squares`` from the rendered FEN;
3. the web live board, derived in ``ChessGameService._compute_alert``.

The user-facing setting (``[game] alert_queen_threat``, on by default) must
silence ALL three, otherwise disabling the warning still flashes the LEDs and
reddens the queen -- a half-applied setting is worse than none, because the
board keeps pointing at a square with no explanation on screen.

The single rule set now lives in ``state/alerts.py`` and takes an injected
``AlertPreferences``; these tests pin the policy itself plus each surface's use
of it.

How a regression manifests
--------------------------
- A surface re-deriving the rule locally keeps warning after the toggle is off
  (its assertion below sees a queen alert / red squares / ``alert == "queen"``).
- Gating the CHECK alert by mistake silences a warning the player must act on
  (the check assertions below go None).
- A wrong default (off) silently drops the warning for every existing install.
"""

import sys
import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock

import chess
import pytest

for _mod in ("spidev", "RPi", "RPi.GPIO", "gpiozero"):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

import universalchess.services.chess_game as svc
from universalchess.state.alerts import (
    CHECK,
    QUEEN_THREAT,
    AlertPreferences,
    resolve_alert,
)
from universalchess.state.chess_game import ChessGameState

# White to move, not in check; White's OWN queen d1 is attacked by the black
# rook d8 down the open d-file. Same position the red-highlight suite uses, so
# both suites describe the identical case.
QUEEN_THREAT_FEN = "3rk3/8/8/8/8/8/8/3QK3 w - - 0 1"
QUEEN_D1 = chess.D1
ROOK_D8 = chess.D8

# Black to move, king e8 in check from the white rook a8 (b8-d8 empty), and
# Black's own queen on d4 is simultaneously attacked by the white rook d1 (d2-d3
# empty). Both conditions hold at once, so check must win the priority contest
# whether or not queen warnings are enabled. Black has escape squares, so this is
# a live check rather than mate.
CHECK_AND_QUEEN_FEN = "R3k3/8/8/8/3q4/8/8/3R3K b - - 0 1"

# Kings only: neither check nor queen threat.
QUIET_FEN = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"

ALERTS_ON = AlertPreferences(queen_threat=True)
ALERTS_OFF = AlertPreferences(queen_threat=False)


# --- The policy itself -------------------------------------------------------


def test_queen_threat_is_resolved_when_enabled():
    """An attacked own queen resolves to a QUEEN_THREAT alert naming both squares.

    Why: this is the baseline the toggle switches off; without it the "disabled"
    assertions below would pass for the wrong reason (no alert at all).
    How a regression manifests: None, the wrong kind, or a target/attacker square
    that is not the queen/rook -- which would point the LEDs at the wrong piece.
    """
    alert = resolve_alert(chess.Board(QUEEN_THREAT_FEN), ALERTS_ON)

    assert alert is not None
    assert alert.kind == QUEEN_THREAT
    assert alert.target_square == QUEEN_D1
    assert alert.attacker_square == ROOK_D8
    assert alert.attacker_squares == frozenset({ROOK_D8})
    # White's queen is the threatened one, so the alert is not "black threatened".
    assert alert.is_black_threatened is False


def test_queen_threat_is_suppressed_when_disabled():
    """The same position resolves to no alert once queen warnings are off.

    Why: this is the whole point of the setting -- one rule set, consulted by
    every surface, returns nothing so none of them can warn.
    How a regression manifests: a non-None alert here means the preference is
    ignored and all three surfaces keep warning.
    """
    assert resolve_alert(chess.Board(QUEEN_THREAT_FEN), ALERTS_OFF) is None


def test_check_is_still_resolved_when_queen_warnings_are_disabled():
    """CHECK is unaffected by the queen preference.

    Why: check is a rules-level fact the player MUST act on (an illegal reply
    follows from ignoring it), so it is deliberately not user-suppressible. The
    two alerts share one code path, which makes over-gating an easy mistake.
    How a regression manifests: None here, i.e. the queen toggle silently
    silences CHECK as well.
    """
    alert = resolve_alert(chess.Board(CHECK_AND_QUEEN_FEN), ALERTS_OFF)

    assert alert is not None
    assert alert.kind == CHECK
    assert alert.target_square == chess.E8
    assert alert.attacker_square == chess.A8
    assert alert.is_black_threatened is True


def test_check_outranks_a_simultaneous_queen_threat():
    """With both conditions present and both enabled, CHECK is reported.

    Why: only one alert is shown at a time and check is the more urgent; the
    position used here is deliberately both (Black's king and queen are attacked
    by the same rook).
    How a regression manifests: kind == QUEEN_THREAT, so the display would show
    YOUR QUEEN while the king is in check.
    """
    alert = resolve_alert(chess.Board(CHECK_AND_QUEEN_FEN), ALERTS_ON)

    assert alert is not None
    assert alert.kind == CHECK


def test_quiet_position_resolves_to_no_alert():
    """A position with neither condition resolves to None regardless of prefs.

    Why: guards against over-alerting during normal play -- the null case for
    both preference states.
    How a regression manifests: a non-None alert, which would leave a permanent
    warning on the display.
    """
    assert resolve_alert(chess.Board(QUIET_FEN), ALERTS_ON) is None
    assert resolve_alert(chess.Board(QUIET_FEN), ALERTS_OFF) is None


def test_preferences_default_to_warning_enabled():
    """A default-constructed AlertPreferences warns about the queen.

    Why: every caller that has no settings to read yet (widgets built before
    settings load, tests, the default ChessGameState) must behave exactly as the
    board did before the setting existed.
    How a regression manifests: queen_threat False by default silently removes
    the warning for everyone.
    """
    assert AlertPreferences().queen_threat is True


@pytest.mark.parametrize(
    "stored,expected",
    [
        # The board/web persist this as a bool on GameSettings; both states must
        # survive the mapping into the policy value object.
        (True, True),
        (False, False),
    ],
)
def test_preferences_read_the_game_setting(stored, expected):
    """from_game_settings maps ``alert_queen_threat`` onto the policy flag.

    Why: this is the only join between persisted settings and the pure policy; a
    typo'd attribute name would make the toggle inert while every other test
    still passed.
    How a regression manifests: the flag does not follow ``stored``.
    """
    from universalchess.players.settings import GameSettings

    settings = GameSettings(section="game", alert_queen_threat=stored)
    assert AlertPreferences.from_game_settings(settings).queen_threat is expected


def test_preferences_default_on_for_settings_without_the_field():
    """A settings object lacking the field yields the warning enabled.

    Why: the policy is also handed partial/stub settings (older configs, test
    doubles). Absent must mean "warn", matching the persisted default, rather
    than raising or silently disabling.
    How a regression manifests: AttributeError, or queen_threat False.
    """

    class _NoAlertFields:
        pass

    assert AlertPreferences.from_game_settings(_NoAlertFields()).queen_threat is True


# --- Surface 1: e-paper alert observers (AlertWidget / LEDs) -----------------


class _AlertRecorder:
    """Records every check / queen-threat / alert-clear the state emits."""

    def __init__(self, state: ChessGameState):
        self.checks = []
        self.queens = []
        self.clears = []
        state.on_check(lambda is_black, atk, king: self.checks.append((is_black, atk, king)))
        state.on_queen_threat(lambda is_black, atk, q: self.queens.append((is_black, atk, q)))
        state.on_alert_clear(lambda: self.clears.append(True))


def test_state_emits_queen_threat_by_default():
    """A fresh ChessGameState still warns about the queen.

    Why: the observers (and therefore the e-paper text and LED flash) must be
    unchanged for any state that was never given preferences.
    How a regression manifests: queens stays empty -- the on-board warning
    disappears for everyone, not just users who disabled it.
    """
    state = ChessGameState()
    recorder = _AlertRecorder(state)

    state.set_position(QUEEN_THREAT_FEN)

    assert recorder.queens == [(False, ROOK_D8, QUEEN_D1)]
    assert recorder.clears == []


def test_state_clears_instead_of_warning_when_queen_alerts_are_disabled():
    """With the setting off, the same position emits alert_clear.

    Why: suppressing the warning is not enough -- the widget must be told to
    hide, or a warning raised before the toggle was flipped would stay on the
    e-paper (and its LEDs would keep flashing on the next refresh).
    How a regression manifests: queens is non-empty (still warning), or both
    lists are empty (warning suppressed but a stale one never cleared).
    """
    state = ChessGameState()
    state.set_alert_preferences(ALERTS_OFF)
    recorder = _AlertRecorder(state)

    state.set_position(QUEEN_THREAT_FEN)

    assert recorder.queens == []
    assert recorder.clears == [True]


def test_state_still_emits_check_when_queen_alerts_are_disabled():
    """The CHECK observer fires with queen warnings disabled.

    Why: the same notification path serves both alerts, so a gate placed too
    early would take CHECK down with it.
    How a regression manifests: checks stays empty and the board stops showing
    CHECK once a user disables queen warnings.
    """
    state = ChessGameState()
    state.set_alert_preferences(ALERTS_OFF)
    recorder = _AlertRecorder(state)

    state.set_position(CHECK_AND_QUEEN_FEN)

    assert recorder.checks == [(True, chess.A8, chess.E8)]
    assert recorder.clears == []


def test_disabling_queen_alerts_hides_a_showing_alert_on_refresh():
    """After the toggle is flipped mid-position, refresh_alerts() clears the alert.

    Why: settings changes arrive while a game is in progress (web save -> board
    hot reload -> widget refresh). The already-visible YOUR QUEEN must come down
    on that refresh instead of lingering until the next move.
    How a regression manifests: the refresh re-emits a queen threat (queens
    non-empty), so the warning stays on screen after being switched off.
    """
    state = ChessGameState()
    state.set_position(QUEEN_THREAT_FEN)
    recorder = _AlertRecorder(state)

    state.set_alert_preferences(ALERTS_OFF)
    state.refresh_alerts()

    assert recorder.queens == []
    assert recorder.clears == [True]


# --- Surface 2: three-color red highlight ------------------------------------


class RedHighlightGatingTests(unittest.TestCase):
    """The red queen/attacker highlight follows the same preference."""

    def _widget(self, state):
        from universalchess.epaper.chess_board import ChessBoardWidget

        # All-white synthetic sheet: the piece glyphs are blank, matching the
        # harness in test_red_highlight_content.
        sprites = Image.new("1", (208, 32), 255)
        widget = ChessBoardWidget(
            0, 0, MagicMock(return_value=Future()), state, flip=False, sprites=sprites
        )
        widget.fen = QUEEN_THREAT_FEN
        return widget

    def test_queen_and_attacker_are_red_by_default(self):
        # Baseline: with the warning enabled the threatened queen and its attacker
        # are highlighted, so the "disabled" case below cannot pass vacuously.
        # Regression: an empty set means three-color mode stopped highlighting the
        # threat for everyone.
        state = ChessGameState()
        squares = self._widget(state)._compute_red_squares()
        self.assertEqual(squares, {QUEEN_D1, ROOK_D8})

    def test_no_red_when_queen_alerts_are_disabled(self):
        # With the setting off, nothing is reddened for a queen threat. Regression:
        # the widget re-derives the rule from the FEN on its own, so the queen and
        # rook still glow red while no alert text explains why.
        state = ChessGameState()
        state.set_alert_preferences(ALERTS_OFF)
        self.assertEqual(self._widget(state)._compute_red_squares(), set())

    def test_check_is_still_red_when_queen_alerts_are_disabled(self):
        # Check highlighting must survive the queen toggle: the checked king and
        # the checking rook stay red. Regression: an empty set here means the gate
        # was applied to the shared path instead of the queen branch.
        state = ChessGameState()
        state.set_alert_preferences(ALERTS_OFF)
        widget = self._widget(state)
        widget.fen = CHECK_AND_QUEEN_FEN
        self.assertEqual(widget._compute_red_squares(), {chess.E8, chess.A8})


# --- Surface 3: web broadcast -----------------------------------------------


class _Players:
    """Minimal players stand-in for the broadcast payload."""

    white_name = "White"
    black_name = "Black"


@pytest.fixture
def service_env(monkeypatch):
    """A ChessGameService bound to a fresh, isolated ChessGameState.

    Mirrors the fixture in test_chess_game_service_broadcast: the web boundary is
    captured and the side channels stubbed so broadcast_state() completes without
    touching global state or the filesystem.
    """
    calls = []
    monkeypatch.setattr(svc, "broadcast_game_state", lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(svc, "get_players_state", lambda: _Players())
    monkeypatch.setattr(svc, "write_fen_log", lambda fen: None)
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.get_pending_move", lambda: None
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.set_pending_move", lambda v: None
    )

    state = ChessGameState()
    monkeypatch.setattr(svc, "get_game_state", lambda: state)
    service = svc.ChessGameService()
    return service, state, calls


def test_broadcast_reports_queen_alert_by_default(service_env):
    """The web broadcast carries alert='queen' when the warning is enabled.

    Why: baseline for the suppression test below, and a guard that the shared
    policy did not change what the web already showed.
    How a regression manifests: alert is None (or 'check') for a plain queen
    threat, so the web banner disappears.
    """
    _service, state, calls = service_env
    state.set_position(QUEEN_THREAT_FEN)

    assert calls[-1]["alert"] == "queen"
    assert calls[-1]["alert_square"] == chess.square_name(QUEEN_D1)


def test_broadcast_omits_queen_alert_when_disabled(service_env):
    """No web alert is broadcast once queen warnings are off.

    Why: the setting is device-wide, not board-only; the web banner must obey the
    same preference or the two surfaces disagree about the same position.
    How a regression manifests: alert == 'queen' here, because _compute_alert
    queried the raw board query instead of the shared policy.
    """
    _service, state, calls = service_env
    state.set_alert_preferences(ALERTS_OFF)
    state.set_position(QUEEN_THREAT_FEN)

    assert calls[-1]["alert"] is None
    assert calls[-1]["alert_square"] is None


def test_broadcast_still_reports_check_when_queen_alerts_disabled(service_env):
    """The web check banner is unaffected by the queen preference.

    Why: same over-gating risk as the board surfaces, on the path the web reads.
    How a regression manifests: alert is None for a live check.
    """
    _service, state, calls = service_env
    state.set_alert_preferences(ALERTS_OFF)
    state.set_position(CHECK_AND_QUEEN_FEN)

    assert calls[-1]["alert"] == "check"
    assert calls[-1]["alert_square"] == chess.square_name(chess.E8)


# --- The catalog entry (board menu + web Game tab) ---------------------------


def test_catalog_exposes_the_queen_alert_toggle_under_game():
    """menu.json carries the toggle, bound to the game setting, on both platforms.

    Why: the shared catalog is what renders the row on the e-paper Game menu and
    in the web Game tab; a node that is missing, mis-bound, or platform-limited
    means the setting exists but cannot be changed on one of the surfaces.
    How a regression manifests: a KeyError/None node, a bind pointing at another
    key (so the toggle moves an unrelated setting), or a missing platform.
    """
    from universalchess.menus.catalog.loader import load_catalog

    catalog = load_catalog()
    node = catalog.get_node("alerts.queen_threat")

    assert node is not None
    assert node["type"] == "toggle"
    assert node["bind"] == {"store": "game", "key": "alert_queen_threat"}
    assert set(node["platforms"]) == {"board", "web"}
    assert node["section"] == "game"

    # The row must be reachable: its group is a child of the Game container.
    assert "group.game.alerts" in catalog.child_ids("settings.game")
    assert "alerts.queen_threat" in catalog.child_ids("group.game.alerts")


if __name__ == "__main__":
    unittest.main()
