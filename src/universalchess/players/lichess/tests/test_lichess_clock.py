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
    ticking_color_from_lichess_moves,
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


def test_ticking_color_follows_the_move_list_in_the_packet():
    """After N plies Lichess is counting the side that is to move.

    Why: remaining and whose clock runs arrive on the same gameState. The
    board turn still belongs to the opponent until the ply is transcribed, so
    the packet's move list is the authority for which side ticks.
    How the regression manifests: after e2e4 e7e5 the helper still says
    black, so White's remaining is frozen while Black is charged for our think.
    """
    assert ticking_color_from_lichess_moves("") == "white"
    assert ticking_color_from_lichess_moves("e2e4") == "black"
    assert ticking_color_from_lichess_moves("e2e4 e7e5") == "white"


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
    player.set_clock_callback(lambda w, b, ticking=None: clocks.append((w, b)))

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
    player.set_clock_callback(lambda w, b, ticking=None: clocks.append((w, b)))

    player._process_time_update(
        {"wtime": timedelta(seconds=120), "btime": timedelta(seconds=90)}
    )

    assert clocks == [(120, 90)]


def test_player_clock_callback_includes_ticking_side_from_the_same_packet():
    """gameState remaining must name whose clock Lichess started with those times.

    Why: the ply is still pending on the physical board, so board turn is the
    opponent. Remaining without the ticking side charges them for our think.
    How the regression manifests: the callback is only (w, b), or ticking is
    black after e2e4 e7e5.
    """
    player = LichessPlayer()
    clocks = []
    player.set_clock_callback(lambda w, b, ticking=None: clocks.append((w, b, ticking)))

    player._process_time_update(
        {
            "moves": "e2e4 e7e5",
            "wtime": timedelta(seconds=590),
            "btime": timedelta(seconds=600),
        }
    )

    assert clocks == [(590, 600, "white")]


def test_player_configures_time_control_from_game_full_before_ready():
    """gameFull must install the Lichess control, not keep the Game menu spec.

    How the regression manifests: time_control callback is empty and the
    widget is built from the local 30 min control.
    """
    player = LichessPlayer()
    specs = []
    clocks = []
    player._time_control_callback = lambda spec: specs.append(spec)
    player.set_clock_callback(lambda w, b, ticking=None: clocks.append((w, b)))
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
    assert player.is_correspondence is False


def test_game_full_correspondence_is_leaveable():
    """gameFull with speed correspondence must mark the game as untimed leave.

    Why: leave_remote_game used to abort then resign every attached id. The
    Board API names correspondence on gameFull; without that flag a later
    BACK or new seek still ended the game on Lichess.

    How the regression manifests: is_correspondence stays False after gameFull.
    """
    player = LichessPlayer()
    player._on_game_connected = lambda: None
    player._game_info_callback = lambda *a: None
    player._username = "adriandyb"

    player._process_game_state(
        {
            "type": "gameFull",
            "speed": "correspondence",
            "daysPerTurn": 3,
            "white": {"name": "adriandyb", "rating": 1500},
            "black": {"name": "adriantest", "rating": 1500},
            "state": {
                "type": "gameState",
                "moves": "e2e4",
                "wtime": LICHESS_UNLIMITED_MILLIS,
                "btime": LICHESS_UNLIMITED_MILLIS,
                "winc": 0,
                "binc": 0,
                "status": "started",
            },
        }
    )

    assert player.is_correspondence is True


def test_days_per_turn_without_speed_is_correspondence():
    """daysPerTurn names correspondence when the speed field is omitted.

    How the regression manifests: is_correspondence stays False, so leave
    abort/resigns a days-per-move game.
    """
    player = LichessPlayer()
    player._record_game_speed({"daysPerTurn": 2})
    assert player.is_correspondence is True
