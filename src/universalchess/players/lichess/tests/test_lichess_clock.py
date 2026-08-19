"""The e-paper clock during a Lichess game must be the server's clock.

Why these tests exist
---------------------
Game start configured the clock from the local Game menu (30 min, 1 min, …)
even when a Lichess player was in a slot. Remaining times from the Board API
were applied only when ``wtime``/``btime`` were millisecond ints, so berserk's
``timedelta`` on later ``gameState`` events was ignored. Correspondence
unlimited (``2147483647`` ms) then ran that local clock to flag.

How a regression manifests
--------------------------
The widget shows the Game-menu control; a ``timedelta`` update never calls
the clock callback; unlimited correspondence stays timed.
"""

from datetime import timedelta

from universalchess.players.lichess.clock import (
    LICHESS_UNLIMITED_MILLIS,
    lichess_millis_to_seconds,
    remaining_from_lichess_state,
    time_control_from_lichess_event,
)
from universalchess.players.lichess.player import LichessPlayer
from universalchess.state.time_control import TimeControl


def test_millis_int_converts_to_whole_seconds():
    """Board API remaining is milliseconds.

    How the regression manifests: 180000 is treated as 180000 seconds (50 h).
    """
    assert lichess_millis_to_seconds(180000) == 180
    assert lichess_millis_to_seconds(0) == 0


def test_timedelta_from_berserk_converts_to_seconds():
    """Later gameState events deliver datetime.timedelta, not ints.

    How the regression manifests: isinstance(wtime, int) skips the update, so
    the local clock never snaps to Lichess remaining.
    """
    assert lichess_millis_to_seconds(timedelta(seconds=180)) == 180
    assert lichess_millis_to_seconds(timedelta(0)) == 0


def test_unlimited_millis_is_not_a_remaining_pair():
    """Correspondence unlimited must not load ~24 days onto the widget.

    How the regression manifests: 2147483647 // 1000 is shown as a live clock.
    """
    assert remaining_from_lichess_state(
        {"wtime": LICHESS_UNLIMITED_MILLIS, "btime": LICHESS_UNLIMITED_MILLIS}
    ) is None
    assert remaining_from_lichess_state(
        {
            "wtime": timedelta(milliseconds=LICHESS_UNLIMITED_MILLIS),
            "btime": timedelta(milliseconds=LICHESS_UNLIMITED_MILLIS),
        }
    ) is None


def test_real_time_remaining_pair_from_ints_and_timedeltas():
    """A live 3+2 remaining pair must survive both encodings.

    How the regression manifests: timedelta path returns None and the clock
    is not updated after the first move.
    """
    assert remaining_from_lichess_state(
        {"wtime": 179000, "btime": 180000}
    ) == (179, 180)
    assert remaining_from_lichess_state(
        {"wtime": timedelta(seconds=179), "btime": timedelta(seconds=180)}
    ) == (179, 180)


def test_game_full_clock_object_is_the_fischer_control():
    """gameFull.clock is the Lichess pair, not the Game menu.

    How the regression manifests: 300000/8000 is ignored and the widget stays
    on the local 30+0 (or untimed) spec.
    """
    spec = time_control_from_lichess_event(
        {
            "speed": "blitz",
            "clock": {"initial": 300000, "increment": 8000},
            "state": {"wtime": 300000, "btime": 300000, "winc": 8000, "binc": 8000},
        }
    )
    assert spec == TimeControl.fischer_minutes(5, 8)


def test_unlimited_correspondence_is_untimed():
    """No clock object plus unlimited remaining is correspondence unlimited.

    How the regression manifests: the local Game clock keeps counting.
    """
    spec = time_control_from_lichess_event(
        {
            "speed": "correspondence",
            "state": {
                "wtime": LICHESS_UNLIMITED_MILLIS,
                "btime": LICHESS_UNLIMITED_MILLIS,
                "winc": 0,
                "binc": 0,
            },
        }
    )
    assert spec is not None
    assert spec.is_timed is False


def test_player_skips_unlimited_remaining_callback():
    """Correspondence unlimited must not load ~24 days onto set_clock.

    How the regression manifests: the callback fires with 2147483 seconds and
    the widget runs a live clock to flag.
    """
    player = LichessPlayer()
    clocks = []
    player.set_clock_callback(lambda w, b: clocks.append((w, b)))

    player._process_time_update(
        {"wtime": LICHESS_UNLIMITED_MILLIS, "btime": LICHESS_UNLIMITED_MILLIS}
    )

    assert clocks == []


def test_player_applies_timedelta_remaining_to_the_clock_callback():
    """A live gameState with timedelta remaining must reach set_clock.

    How the regression manifests: the callback is not called, so the e-paper
    never leaves the local remaining.
    """
    player = LichessPlayer()
    clocks = []
    player.set_clock_callback(lambda w, b: clocks.append((w, b)))

    player._process_time_update(
        {"wtime": timedelta(seconds=120), "btime": timedelta(seconds=90)}
    )

    assert clocks == [(120, 90)]


def test_player_configures_time_control_from_game_full_before_ready():
    """gameFull must install the Lichess control, not keep the Game menu spec.

    How the regression manifests: time_control callback is empty and the
    widget is built from the local 30 min control.
    """
    player = LichessPlayer()
    specs = []
    clocks = []
    player._time_control_callback = lambda spec: specs.append(spec)
    player.set_clock_callback(lambda w, b: clocks.append((w, b)))
    player._on_game_connected = lambda: None
    player._game_info_callback = lambda *a: None
    player._username = "adriandyb"

    player._process_game_state(
        {
            "type": "gameFull",
            "speed": "blitz",
            "clock": {"initial": 180000, "increment": 2000},
            "white": {"name": "adriandyb", "rating": 1500},
            "black": {"name": "adriantest", "rating": 1500},
            "state": {
                "type": "gameState",
                "moves": "",
                "wtime": 180000,
                "btime": 180000,
                "winc": 2000,
                "binc": 2000,
                "status": "started",
            },
        }
    )

    assert specs == [TimeControl.fischer_minutes(3, 2)]
    assert clocks == [(180, 180)]
