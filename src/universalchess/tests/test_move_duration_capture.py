"""Tests for measuring and persisting how long each move took.

Elapsed time per move is recorded by ``ChessGameState.push_move`` -- the single
instant a move is confirmed -- and exposed as ``move_durations_ms``, aligned
one-to-one with the board's move stack. Two consumers read that one measurement:
the database writer persists it as ``GameMove.move_duration_ms``, and the PGN
service annotates the live game with it. Measuring separately in each consumer
would produce two answers for one interval.

It is measured on a *monotonic* clock, because the device's wall clock is
stepped by NTP shortly after boot and a step of a few seconds lands squarely in
the range of a real think time.

The duration is charged to the move that ends the interval. For an engine's move
that deliberately includes the time the human spent transcribing it onto the
physical board: the human is occupied with that move for the whole span, and the
interval must be conserved so consecutive durations sum to the game length. Note
this means an engine move's duration does not reconcile against the [%clk]
deltas, which stop the engine's clock when it displays its move.

``move_at`` is not usable for any of this: it is set by the ORM column default
when the row is inserted, which happens on the background task worker an
unbounded time after the move was confirmed.
"""

import chess
import pytest

pytest.importorskip("sqlalchemy")

# Must precede any import that reaches db.models: importing GameManager kicks off
# a deferred models import, and models runs create_all() against the configured
# database at import time. On a machine where that path is not writable the
# module import fails and leaves a poisoned entry in sys.modules, so every later
# "from universalchess.db import models" raises ImportError instead.
import universalchess.db.uri as _uri  # noqa: E402

_uri.get_database_uri = lambda: "sqlite:///:memory:"

from universalchess.managers.game.game_manager import GameManager  # noqa: E402
from universalchess.managers.game.move_persistence import (  # noqa: E402
    persist_move_and_maybe_create_game,
)
from universalchess.state.chess_game import ChessGameState, reset_chess_game  # noqa: E402
from universalchess.tests.fake_clock import FakeMonotonic  # noqa: E402
from universalchess.utils.led import LedCallbacks  # noqa: E402

FEN_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


# ---------------------------------------------------------------------------
# Measurement (ChessGameState)
# ---------------------------------------------------------------------------


@pytest.fixture
def timed_state():
    """A fresh game state driven by a hand-advanced clock."""
    state = ChessGameState()
    clock = FakeMonotonic()
    state._now_monotonic = clock
    state.start_move_timing()
    return state, clock


def _push(state, uci):
    state.push_move(chess.Move.from_uci(uci))


def test_first_move_is_measured_from_the_start_of_the_game(timed_state):
    """White's first move is timed from when the game started.

    Why: the first move has no preceding move to measure from, and in a casual
    game it is often the longest think. How a regression manifests: the first
    move reports None (or 0) while every later move is measured, so the opening
    move is missing from any time-usage analysis.
    """
    state, clock = timed_state

    clock.advance(12.0)
    _push(state, "e2e4")

    assert state.move_durations_ms == (12000,)


def test_each_move_is_measured_from_the_previous_move_not_the_game_start(timed_state):
    """Durations are per-move intervals, not cumulative elapsed game time.

    Why: [%emt] is defined as the time used *for this move*; cumulative time is
    a different command ([%egt]). How a regression manifests: the anchor is
    never advanced, so durations grow monotonically (12s, 19s, 22.5s) and every
    move after the first is overstated by the whole game so far.
    """
    state, clock = timed_state

    clock.advance(12.0)
    _push(state, "e2e4")
    clock.advance(7.0)
    _push(state, "e7e5")
    clock.advance(3.5)
    _push(state, "g1f3")

    assert state.move_durations_ms == (12000, 7000, 3500)


def test_durations_stay_aligned_with_the_move_stack(timed_state):
    """There is exactly one duration per move on the board, in order.

    Why: consumers index durations by move number to annotate the right ply. A
    length mismatch silently shifts every annotation onto a neighbouring move.
    How a regression manifests: a duration is appended on a non-move position
    change (or dropped), so the lists diverge and Black's think time is printed
    against White's move.
    """
    state, clock = timed_state

    for uci in ("e2e4", "e7e5", "g1f3", "b8c6"):
        clock.advance(2.0)
        _push(state, uci)

    assert len(state.move_durations_ms) == len(state.move_stack)


def test_duration_is_absent_when_the_game_was_never_anchored():
    """With no start instant recorded, the duration is None rather than invented.

    Why: a game resumed from the database, or replayed by "play from here", has
    no measured start for its next move. A fabricated value is worse than a NULL
    because nothing downstream can tell it apart from a real measurement.

    How a regression manifests: the anchor defaults to 0.0 or to "now" at first
    use, so the first move of a resumed game reports either an absurd duration
    or a spurious 0.
    """
    state = ChessGameState()
    state._now_monotonic = FakeMonotonic()
    state._move_timing_anchor = None

    _push(state, "e2e4")

    assert state.move_durations_ms == (None,)
    assert state.last_move_duration_ms is None


def test_takeback_drops_the_duration_and_restarts_the_measurement(timed_state):
    """After a takeback the replayed move is timed from the takeback.

    Why: the retracted move's duration no longer describes anything on the
    board, and the next interval must not span the retracted think plus the
    correction plus the new think.

    How a regression manifests: a player who thinks 60s, takes the move back,
    then plays instantly records ~60s for the replayed move; or the stale
    duration is left behind and every later move is annotated one ply off.
    """
    state, clock = timed_state

    clock.advance(60.0)
    _push(state, "e2e4")
    clock.advance(20.0)  # deliberating the move that is then taken back
    _push(state, "e7e5")

    state.pop_move()
    clock.advance(1.0)
    _push(state, "c7c5")

    assert state.move_durations_ms == (60000, 1000)


def test_reset_discards_durations_with_the_move_stack(timed_state):
    """A new game starts with no durations and a fresh anchor.

    Why: reset() clears the board's move stack, so durations indexed by move
    would describe moves that are no longer there. How a regression manifests:
    the previous game's durations are annotated onto the new game's moves.
    """
    state, clock = timed_state

    clock.advance(5.0)
    _push(state, "e2e4")
    state.reset()

    assert state.move_durations_ms == ()

    clock.advance(3.0)
    _push(state, "d2d4")
    assert state.move_durations_ms == (3000,)


def test_duration_is_a_whole_number_of_milliseconds(timed_state):
    """Sub-millisecond precision is rounded away at capture.

    Why: the column is an INTEGER; handing SQLAlchemy a float would either be
    coerced silently or fail depending on the backend. How a regression
    manifests: the duration is 4321.9876... and the value stored on SQLite is a
    float, so downstream integer arithmetic behaves inconsistently.
    """
    state, clock = timed_state

    clock.advance(4.3219876)
    _push(state, "e2e4")

    duration = state.last_move_duration_ms
    assert isinstance(duration, int)
    assert duration == 4322


# ---------------------------------------------------------------------------
# The database writer reads that measurement
# ---------------------------------------------------------------------------


def _noop_led() -> LedCallbacks:
    return LedCallbacks(
        from_to=lambda *a, **k: None,
        array=lambda *a, **k: None,
        single=lambda *a, **k: None,
        off=lambda *a, **k: None,
        from_to_hint=lambda *a, **k: None,
        array_hint=lambda *a, **k: None,
        array_fast=lambda *a, **k: None,
        from_to_fast=lambda *a, **k: None,
        single_fast=lambda *a, **k: None,
    )


@pytest.fixture
def gm(monkeypatch):
    """A GameManager whose move persistence is recorded instead of executed.

    The database session is a sentinel: the persistence call is replaced with a
    recorder, so these tests isolate which duration reaches the row from the
    (separately tested) SQL write. Analysis lookup and clock reads are stubbed
    for the same reason.
    """
    reset_chess_game()
    manager = GameManager(save_to_database=False)
    manager.set_led_callbacks(_noop_led())
    manager.database_session = object()

    recorded = []
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.persist_move_and_maybe_create_game",
        lambda **kwargs: (recorded.append(kwargs) or (1, True)),
    )
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.get_analysis_service",
        lambda: type("S", (), {"get_position_analysis": staticmethod(lambda fen: None)})(),
    )
    monkeypatch.setattr(manager, "_get_clock_times_for_db", lambda: (None, None))
    manager.recorded = recorded

    yield manager
    manager._stop_event.set()


def _post_move(manager, move_uci="e2e4", is_first_move=False):
    """Enqueue post-move tasks for a move, as execute_complete_move does."""
    manager._enqueue_post_move_tasks(
        target_square=chess.E4,
        move_uci=move_uci,
        fen_before_move=chess.STARTING_FEN,
        fen_after_move=FEN_AFTER_E4,
        is_first_move=is_first_move,
        game_ended=False,
        result_string=None,
        termination=None,
    )


def test_persisted_duration_is_the_one_measured_for_that_move(gm, monkeypatch):
    """The row carries the state's measurement for the move being written.

    Why: this is the seam between the single measurement and the database. How a
    regression manifests: the writer measures its own interval, which starts
    from whenever the previous row was queued rather than from the previous
    move, so the two records of one game disagree.
    """
    monkeypatch.setattr(gm._task_worker, "submit", lambda fn: fn())
    clock = FakeMonotonic()
    gm._game_state._now_monotonic = clock
    gm._game_state.start_move_timing()

    clock.advance(9.0)
    gm._game_state.push_move(chess.Move.from_uci("e2e4"))
    _post_move(gm, is_first_move=True)

    assert gm.recorded[0]["move_duration_ms"] == 9000


def test_duration_is_read_when_the_move_is_confirmed_not_when_the_row_is_written(
    gm, monkeypatch
):
    """The value is captured synchronously, before the task is queued.

    Why: this is the entire reason the duration is not derived from move_at. The
    database write runs on the task worker behind board validation and engine
    work, and further moves can land before it does.

    How a regression manifests: reading inside the queued task picks up whatever
    move is current by then. Here a second move (2s) lands before the first
    move's row is written, so a regression records 2000 instead of 9000.
    """
    deferred = []
    monkeypatch.setattr(gm._task_worker, "submit", deferred.append)
    clock = FakeMonotonic()
    gm._game_state._now_monotonic = clock
    gm._game_state.start_move_timing()

    clock.advance(9.0)
    gm._game_state.push_move(chess.Move.from_uci("e2e4"))
    _post_move(gm, is_first_move=True)

    clock.advance(2.0)
    gm._game_state.push_move(chess.Move.from_uci("e7e5"))
    deferred[0]()

    assert gm.recorded[0]["move_duration_ms"] == 9000


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def session(private_db_session, monkeypatch):
    """A private database, with its models bound into move_persistence."""
    from universalchess.db import models

    import universalchess.managers.game.move_persistence as mp

    monkeypatch.setattr(mp, "_get_models", lambda: models)

    private_db_session.models = models
    return private_db_session


def _persist(session, **overrides):
    kwargs = dict(
        session=session,
        is_first_move=True,
        current_game_db_id=-1,
        source_file="test",
        game_info={},
        fen_before_move=chess.STARTING_FEN,
        move_uci="e2e4",
        fen_after_move=FEN_AFTER_E4,
        white_clock=None,
        black_clock=None,
    )
    kwargs.update(overrides)
    return persist_move_and_maybe_create_game(**kwargs)


def test_duration_round_trips_through_the_move_row(session):
    """A measured duration is stored on the move it belongs to.

    Why: the value has to survive to export; the PGN builder reads it back from
    this column. How a regression manifests: the parameter is accepted but never
    assigned to the model, so every row is NULL and no [%emt] is ever emitted.
    """
    models = session.models

    _persist(session, move_duration_ms=12000)

    row = session.query(models.GameMove).filter_by(move="e2e4").one()
    assert row.move_duration_ms == 12000


def test_duration_defaults_to_null_when_not_measured(session):
    """A move persisted without a measurement leaves the column NULL.

    Why: NULL is the honest representation of "not measured" -- it is what every
    row written before this feature holds, and what "play from here" produces.
    How a regression manifests: a 0 default makes historical games export as a
    full set of instantaneous moves.
    """
    models = session.models

    _persist(session)

    row = session.query(models.GameMove).filter_by(move="e2e4").one()
    assert row.move_duration_ms is None


def test_time_control_is_stored_on_the_game_record(session):
    """The control is recorded once, on the game, when the game is created.

    Why: the exporter needs it both for [TimeControl] and to decide whether
    [%clk] is meaningful. How a regression manifests: the column stays NULL, so
    every timed game exports without a control and its clock annotations are
    suppressed as though it were untimed.
    """
    models = session.models

    _persist(session, time_control="300+5")

    game = session.query(models.Game).one()
    assert game.time_control == "300+5"


def test_migration_adds_the_new_columns_to_an_existing_database(tmp_path):
    """An upgraded database gains move_duration_ms and time_control.

    Why: create_all() only creates missing *tables*, never missing columns, so
    without a migration entry the first move written on an existing install
    raises OperationalError ("no such column") and move persistence stops
    working -- a failure that only appears on upgrade, never on a fresh install.

    The second run guards the idempotence of the column-existence check.
    """
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE gameMove ("
            "id INTEGER PRIMARY KEY, gameid INTEGER, move_at DATETIME, "
            "move VARCHAR(10), fen VARCHAR(255), white_clock INTEGER, "
            "black_clock INTEGER, eval_score INTEGER, best_move VARCHAR(10), "
            "coach_statement TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE game ("
            "id INTEGER PRIMARY KEY, created_at DATETIME, source VARCHAR(255), "
            "event VARCHAR(255), site VARCHAR(255), round VARCHAR(255), "
            "white VARCHAR(255), black VARCHAR(255), result VARCHAR(255), "
            "termination VARCHAR(255), start_fen VARCHAR(255), chess960 BOOLEAN)"
        ))
        conn.commit()

    from universalchess.db.models import apply_pending_migrations

    for _ in range(2):
        apply_pending_migrations(engine)
        inspector = inspect(engine)
        assert "move_duration_ms" in [c["name"] for c in inspector.get_columns("gameMove")]
        assert "time_control" in [c["name"] for c in inspector.get_columns("game")]
