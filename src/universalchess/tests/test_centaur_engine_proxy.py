"""Tests for the Centaur UCI engine proxy.

The proxy replaces the modified Stockfish Centaur ships: it forwards UCI to any
configured UC engine, enforces a memory-safety floor on Hash/MultiPV, injects
configured options, and reconstructs+records Centaur's games into UC's database.

These tests pin each layer: the pure option/position transforms, the DB recorder
(against an in-memory database), and the end-to-end stream wiring (with fake
Centaur/engine streams) using a transcript shaped like Centaur's real output.
"""

import io
import json
import threading

import pytest

pytest.importorskip("chess")
pytest.importorskip("sqlalchemy")

from universalchess.services.centaur_engine_proxy import (
    CentaurStatePublisher,
    GameRecorder,
    PositionTracker,
    ProxyConfig,
    build_config_setoptions,
    load_proxy_config,
    parse_position_command,
    rewrite_setoption_line,
    run_proxy,
)
from universalchess.services.centaur_engine_proxy.hook import render_launcher
from universalchess.services.centaur_engine_proxy.options import (
    MEMORY_SAFE_HASH_MAX_MB,
    MEMORY_SAFE_MULTIPV_MAX,
    allows_setoption,
    is_uci_engine_output_line,
    parse_advertised_option_name,
)
from universalchess.services.centaur_engine_proxy.tracker import START_FEN


# ---------------------------------------------------------------------------
# Option rewriting (memory-safety floor)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        # Hash above the cap is clamped; this is the crash the floor prevents
        # (Centaur asks for 128 MB, which OOMs NNUE Stockfish on the board).
        ("setoption name Hash value 128", f"setoption name Hash value {MEMORY_SAFE_HASH_MAX_MB}"),
        # MultiPV above the cap is clamped to 1.
        ("setoption name MultiPV value 10", f"setoption name MultiPV value {MEMORY_SAFE_MULTIPV_MAX}"),
        # A value already within the cap is preserved (no needless rewrite).
        ("setoption name Hash value 8", "setoption name Hash value 8"),
        # Case-insensitive: Centaur's casing must not let an oversized value pass.
        ("setoption name hash value 256", f"setoption name hash value {MEMORY_SAFE_HASH_MAX_MB}"),
        # Unrelated options pass through untouched.
        ("setoption name Threads value 4", "setoption name Threads value 4"),
        # Non-setoption lines are returned verbatim.
        ("position startpos moves e2e4", "position startpos moves e2e4"),
    ],
)
def test_rewrite_setoption_enforces_memory_floor(line, expected):
    # Guards the memory-safety contract: only Hash/MultiPV are capped, only when
    # over the cap, regardless of casing; everything else is untouched.
    assert rewrite_setoption_line(line) == expected


def test_build_config_setoptions_renders_clamps_and_skips_none():
    # Config options become sorted setoption lines: booleans lowercased, Hash
    # clamped to the floor even from config, None skipped. Sorted order makes the
    # injected sequence deterministic.
    options = {"UCI_LimitStrength": True, "UCI_Elo": 1500, "Hash": 999, "Threads": None}
    assert build_config_setoptions(options) == [
        f"setoption name Hash value {MEMORY_SAFE_HASH_MAX_MB}",
        "setoption name UCI_Elo value 1500",
        "setoption name UCI_LimitStrength value true",
    ]


# ---------------------------------------------------------------------------
# Position parsing + game reconstruction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("position startpos moves e2e4 e7e5", (None, ["e2e4", "e7e5"])),
        ("position startpos", (None, [])),
        (
            "position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1 moves e2e4",
            ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", ["e2e4"]),
        ),
        ("isready", None),
        # Malformed fen (fewer than 6 fields) is rejected, not mis-parsed.
        ("position fen rnbqkbnr w KQkq - 0", None),
    ],
)
def test_parse_position_command(line, expected):
    # The recorder depends on exact (start_fen, moves) parsing; a wrong split
    # would record the wrong game. None for non/!malformed-position lines.
    assert parse_position_command(line) == expected


def test_tracker_records_incremental_moves_with_fens():
    """A growing move list yields only the newly appended moves each time.

    Centaur resends the full move list every turn; the tracker must emit only the
    delta (with the FEN after each new move) so the recorder does not duplicate
    earlier moves. If it emitted the whole list each time, move rows would
    multiply.
    """
    tracker = PositionTracker()

    first = tracker.update(None, ["e2e4"])
    assert first.is_new_game is True
    assert first.start_fen == START_FEN
    assert [m for m, _ in first.moves_added] == ["e2e4"]
    assert first.total_moves == 1

    second = tracker.update(None, ["e2e4", "e7e5"])
    assert second.is_new_game is False
    assert [m for m, _ in second.moves_added] == ["e7e5"]
    assert second.total_moves == 2
    # The FEN after 1...e5 must reflect black having moved.
    assert " w " in second.moves_added[0][1]  # white to move after black's reply


def test_tracker_rebranches_when_startpos_list_diverges_without_new_game():
    """A divergent startpos list without ucinewgame is a rebranch, not a new game.

    Why this test exists: after e4 e5 Nf3, a ``position startpos moves d2d4``
    shares the start position, so it is the same game rewound to the opening and
    replayed down a different line -- the tracker truncates the old moves and
    appends the new one. Only ucinewgame (mark_new_game) starts a fresh game; a
    bare divergent list must not, or every rewind-and-rethink would fragment the
    record. Origin-off-the-mainline divergence is covered separately below.

    How a regression manifests: is_new_game flips True (fragmenting the game) or
    the old moves are not removed (illegal e4 e5 Nf3 d4 record).
    """
    tracker = PositionTracker()
    tracker.update(None, ["e2e4", "e7e5", "g1f3"])
    again = tracker.update(None, ["d2d4"])
    assert again.is_new_game is False
    assert again.moves_removed == 3
    assert [m for m, _ in again.moves_added] == ["d2d4"]
    assert again.total_moves == 1
    assert [m.uci() for m in tracker.board.move_stack] == ["d2d4"]


def test_tracker_starts_new_game_when_origin_off_mainline():
    """A command whose origin is not on the current mainline starts a new game.

    Why this test exists: the safety net for a position the tracker has never
    seen (e.g. Centaur jumps to an unrelated setup). With no shared position to
    rejoin, reconciling would be meaningless, so a fresh game is the only correct
    response. Uses a custom FEN that does not occur in the e4 e5 mainline.

    How a regression manifests: is_new_game stays False and the unrelated moves
    are grafted onto the previous game's record.
    """
    tracker = PositionTracker()
    tracker.update(None, ["e2e4", "e7e5"])
    # A position unreachable in the e4/e5 line (different pawn structure).
    unrelated = "rnbqkbnr/pppp1ppp/8/4p3/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1"
    fresh = tracker.update(unrelated, [])
    assert fresh.is_new_game is True
    assert fresh.total_moves == 0
    assert tracker.board.fen() == unrelated


def test_tracker_treats_rolling_fen_delta_as_one_continuous_game():
    """Centaur's rolling ``fen <board> moves <delta>`` extends the game.

    Why this test exists: the first captured stream was ``position startpos moves
    e2e4`` then ``position fen <board-after-e4> moves d7d5``. The second command
    restates a prior board as a FEN and appends the new move. The bug was that a
    changed start_fen looked like a new game, fragmenting the record and
    reverting the web view. This pins continuation: one game, both moves.

    How a regression manifests: ``is_new_game`` flips back to True and
    ``total_moves`` drops to 1 (only the delta).
    """
    tracker = PositionTracker()
    tracker.update(None, [])  # position startpos (game init, no moves)
    tracker.update(None, ["e2e4"])  # position startpos moves e2e4

    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    rolling = tracker.update(fen_after_e4, ["d7d5"])  # fen <after e4> moves d7d5

    assert rolling.is_new_game is False
    assert [m for m, _ in rolling.moves_added] == ["d7d5"]
    assert rolling.total_moves == 2
    # The cumulative board carries both moves, so PGN/FEN are whole (not just d5).
    assert tracker.board.move_stack[0].uci() == "e2e4"
    assert tracker.board.move_stack[1].uci() == "d7d5"
    assert tracker.board.fen().startswith("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w ")


def test_tracker_extends_when_fixed_origin_move_list_grows():
    """A fixed-origin command whose tail grows past the tip extends the game.

    Why this test exists: this is the exact move-3 reset captured on hardware.
    Centaur kept the origin FEN fixed at ``after e4 c5`` and grew the tail:
    ``moves c2c4`` (tip becomes e4 c5 c4), then ``moves c2c4 b8c6``. The current
    tip (after c4) lies one move into the second command's path, so only b8c6 is
    new. The bug treated the second command as a new game (origin != tip and not
    a startpos extension), collapsing the move list to [c2c4, b8c6] from a
    mid-game FEN -- exactly the "third move resets the live board" report.

    How a regression manifests: the final update is is_new_game=True with
    total_moves=2 instead of a single appended b8c6 reaching total_moves=4.
    """
    tracker = PositionTracker()
    tracker.update(None, [])
    tracker.update(None, ["e2e4"])
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    tracker.update(after_e4, ["c7c5"])  # tip: e4 c5
    after_e4_c5 = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    tracker.update(after_e4_c5, ["c2c4"])  # tip: e4 c5 c4 (total 3)

    grown = tracker.update(after_e4_c5, ["c2c4", "b8c6"])  # fixed origin, longer tail

    assert grown.is_new_game is False
    assert [m for m, _ in grown.moves_added] == ["b8c6"]
    assert grown.total_moves == 4
    assert [m.uci() for m in tracker.board.move_stack] == ["e2e4", "c7c5", "c2c4", "b8c6"]


def test_tracker_truncates_line_on_takeback():
    """A command restating a position behind the tip is a takeback: shorten it.

    Why this test exists: this is the exact two-takeback stream captured on
    hardware. The line reached e4 e6 c4 d5 (total 4); Centaur then sent
    ``fen <after e4 e6> moves c2c4`` (d5 taken back -> total 3) and ``fen <after
    e4> moves e7e6`` (c4 taken back -> total 2). The physical board went back but
    the web stayed at d5, because the tracker ignored behind-restatements. This
    pins that each takeback removes the trailing move(s) and the board regresses.

    How a regression manifests: moves_removed stays 0 and total_moves stays 4
    (web frozen ahead of the board), or is_new_game flips True (reset).
    """
    tracker = PositionTracker()
    tracker.update(None, ["e2e4"])
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    tracker.update(after_e4, ["e7e6"])
    after_e4_e6 = "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    tracker.update(after_e4_e6, ["c2c4"])
    after_e4_e6_c4 = "rnbqkbnr/pppp1ppp/4p3/8/2P1P3/8/PP1P1PPP/RNBQKBNR b KQkq - 0 2"
    tracker.update(after_e4_e6_c4, ["d7d5"])  # tip: e4 e6 c4 d5 (total 4)

    takeback_d5 = tracker.update(after_e4_e6, ["c2c4"])  # d5 taken back
    assert takeback_d5.is_new_game is False
    assert takeback_d5.moves_added == []
    assert takeback_d5.moves_removed == 1
    assert takeback_d5.total_moves == 3
    assert [m.uci() for m in tracker.board.move_stack] == ["e2e4", "e7e6", "c2c4"]

    takeback_c4 = tracker.update(after_e4, ["e7e6"])  # c4 taken back
    assert takeback_c4.is_new_game is False
    assert takeback_c4.moves_added == []
    assert takeback_c4.moves_removed == 1
    assert takeback_c4.total_moves == 2
    assert [m.uci() for m in tracker.board.move_stack] == ["e2e4", "e7e6"]


def test_tracker_takeback_then_new_continuation_truncates_and_appends():
    """A takeback that continues down a different line truncates then appends.

    Why this test exists: after taking a move back the player usually plays a
    *different* move. One command then both removes the old tail and adds the new
    move (``fen <after e4 e6> moves d2d4`` when the line was e4 e6 c4). The single
    update must report both the removal and the addition so the record matches.

    How a regression manifests: the new move is appended without removing the
    taken-back one, leaving an illegal e4 e6 c4 d4 sequence in the record.
    """
    tracker = PositionTracker()
    tracker.update(None, ["e2e4"])
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    tracker.update(after_e4, ["e7e6"])
    after_e4_e6 = "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    tracker.update(after_e4_e6, ["c2c4"])  # tip: e4 e6 c4 (total 3)

    diverge = tracker.update(after_e4_e6, ["d2d4"])  # take back c4, play d4

    assert diverge.is_new_game is False
    assert diverge.moves_removed == 1
    assert [m for m, _ in diverge.moves_added] == ["d2d4"]
    assert diverge.total_moves == 3
    assert [m.uci() for m in tracker.board.move_stack] == ["e2e4", "e7e6", "d2d4"]


def test_tracker_mark_new_game_forces_fresh_game_not_takeback():
    """ucinewgame starts a fresh game even when the position rewinds to start.

    Why this test exists: a bare ``position startpos`` is ambiguous -- it could be
    a new game or a full takeback to the opening of the current one. ``ucinewgame``
    disambiguates: mark_new_game() must force is_new_game on the next position so
    a genuine new game opens a new record instead of truncating the old one.

    How a regression manifests: is_new_game stays False after ucinewgame, so the
    new game's moves accrue onto (or truncate) the previous game's record.
    """
    tracker = PositionTracker()
    tracker.update(None, ["e2e4", "e7e5"])  # a game is in progress

    tracker.mark_new_game()
    fresh = tracker.update(None, [])  # position startpos for the new game

    assert fresh.is_new_game is True
    assert fresh.total_moves == 0
    assert tracker.board.fen() == START_FEN


def test_tracker_ignores_requery_of_current_position():
    """Re-sending the current position (eval re-query) adds no moves.

    Why this test exists: Centaur may restate the same position to ask for an
    eval again. That must not duplicate move rows or emit a spurious delta. Uses
    the rolling form resolving to the position we are already at.

    How a regression manifests: moves_added is non-empty (duplicate move row) or
    is_new_game flips True, corrupting the record.
    """
    tracker = PositionTracker()
    tracker.update(None, ["e2e4", "e7e5"])  # after 1.e4 e5
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    requery = tracker.update(fen_after_e4, ["e7e5"])  # resolves to the same board

    assert requery.is_new_game is False
    assert requery.moves_added == []
    assert requery.total_moves == 2


def test_recorder_keeps_rolling_fen_delta_in_one_game(db):
    """The recorder appends rolling-form moves to one game, not a game per move.

    Why this test exists: the reported DB symptom was ``game N=[e4]``,
    ``game N+1=[d5]`` -- one game per move -- because the rolling fen+delta form
    was misread as a new game each time. This asserts a single game accrues the
    initial row plus both moves.

    How a regression manifests: two Game rows appear (one per move) instead of
    one game with ['', 'e2e4', 'd7d5'].
    """
    session, models = db
    tracker = PositionTracker()
    recorder = GameRecorder(session, source="centaur", models=models)

    recorder.apply(tracker.update(None, []))  # position startpos -> opens game
    recorder.apply(tracker.update(None, ["e2e4"]))  # startpos moves e2e4
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    recorder.apply(tracker.update(fen_after_e4, ["d7d5"]))  # rolling fen+delta

    games = session.query(models.Game).all()
    assert len(games) == 1
    moves = session.query(models.GameMove).order_by(models.GameMove.id).all()
    assert [m.move for m in moves] == ["", "e2e4", "d7d5"]


def test_tracker_sets_result_on_checkmate():
    """Reaching checkmate populates result/termination from the replayed board.

    The engine never tells the proxy the result, so the proxy derives it from the
    reconstructed position. Fool's mate (1.f3 e5 2.g4 Qh4#) ends 0-1 by
    checkmate. If outcome detection regressed, result would stay None and the
    recorded game would have no result.
    """
    tracker = PositionTracker()
    update = tracker.update(None, ["f2f3", "e7e5", "g2g4", "d8h4"])
    assert update.result == "0-1"
    assert update.termination == "checkmate"


# ---------------------------------------------------------------------------
# Recorder (in-memory database)
# ---------------------------------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    """In-memory database with the UC schema, plus a session and the models.

    Redirects the DB URI before importing models (whose import builds an engine),
    then binds a fresh in-memory engine so the recorder writes are isolated and
    assertable.
    """
    import universalchess.db.uri as uri

    monkeypatch.setattr(uri, "get_database_uri", lambda: "sqlite:///:memory:")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from universalchess.db import models

    # StaticPool + check_same_thread=False so a single in-memory database is
    # shared across threads (the default per-thread pool would hand each thread
    # its own empty DB). This mirrors the proxy's recorder, whose game state is
    # mutated from the position (main) thread and the terminal-bestmove (pump)
    # thread, serialized by run_proxy's lock -- never truly concurrent.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = Session(bind=engine)
    yield session, models
    session.close()


def test_recorder_writes_game_initial_position_and_moves(db):
    """A new-game update creates a game with an initial-position row, then moves.

    Asserts the full recorded shape (source, an empty-move start row, one row per
    move with the resulting FEN, in order) -- the same shape UC records for its
    own games, so Centaur games show up identically. A presence-only check would
    miss a missing initial row or a wrong/duplicated move.
    """
    session, models = db
    tracker = PositionTracker()
    recorder = GameRecorder(session, source="centaur", models=models)

    recorder.apply(tracker.update(None, ["e2e4"]))
    recorder.apply(tracker.update(None, ["e2e4", "e7e5"]))

    games = session.query(models.Game).all()
    assert len(games) == 1
    assert games[0].source == "centaur"

    moves = session.query(models.GameMove).order_by(models.GameMove.id).all()
    assert [m.move for m in moves] == ["", "e2e4", "e7e5"]
    assert moves[0].fen == START_FEN
    # Each move row carries the FEN *after* that move (white to move after black).
    assert " b " in moves[1].fen  # black to move after 1.e4
    assert " w " in moves[2].fen  # white to move after 1...e5


def test_recorder_stamps_result_once_and_starts_second_game(db):
    """A checkmate stamps the game result, and ucinewgame opens a second game.

    Guards two things at once: the result is written to the right game, and a new
    game (signaled by mark_new_game, i.e. Centaur's ucinewgame) gets its own row
    rather than rebranching the previous one. If new-game handling regressed, both
    games' moves would collapse into one record.
    """
    session, models = db
    tracker = PositionTracker()
    recorder = GameRecorder(session, source="centaur", models=models)

    recorder.apply(tracker.update(None, ["f2f3", "e7e5", "g2g4", "d8h4"]))
    first_games = session.query(models.Game).all()
    assert len(first_games) == 1
    assert first_games[0].result == "0-1"

    tracker.mark_new_game()
    recorder.apply(tracker.update(None, ["d2d4"]))
    games = session.query(models.Game).order_by(models.Game.id).all()
    assert len(games) == 2
    # Second game has its own move rows (initial + the one move).
    second_moves = session.query(models.GameMove).filter_by(gameid=games[1].id).all()
    assert [m.move for m in second_moves] == ["", "d2d4"]


def test_recorder_deletes_trailing_rows_on_takeback(db):
    """A takeback update deletes the trailing move rows it removed.

    Why this test exists: the recorded game must match what is actually on the
    board after a takeback (the captured two-takeback stream). After e4 e6 c4 d5,
    taking back d5 then c4 must leave the game as ['', e4, e6], not keep the
    taken-back rows. The initial empty-move row must always survive.

    How a regression manifests: the d5/c4 rows linger (record longer than the
    board) or the initial start-FEN row is deleted by an over-broad delete.
    """
    session, models = db
    tracker = PositionTracker()
    recorder = GameRecorder(session, source="centaur", models=models)

    recorder.apply(tracker.update(None, ["e2e4"]))
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    recorder.apply(tracker.update(after_e4, ["e7e6"]))
    after_e4_e6 = "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    recorder.apply(tracker.update(after_e4_e6, ["c2c4"]))
    after_e4_e6_c4 = "rnbqkbnr/pppp1ppp/4p3/8/2P1P3/8/PP1P1PPP/RNBQKBNR b KQkq - 0 2"
    recorder.apply(tracker.update(after_e4_e6_c4, ["d7d5"]))  # ['', e4, e6, c4, d5]

    recorder.apply(tracker.update(after_e4_e6, ["c2c4"]))  # take back d5
    recorder.apply(tracker.update(after_e4, ["e7e6"]))  # take back c4

    games = session.query(models.Game).all()
    assert len(games) == 1
    moves = session.query(models.GameMove).order_by(models.GameMove.id).all()
    assert [m.move for m in moves] == ["", "e2e4", "e7e6"]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_proxy_config_parses_engine_and_options():
    # The card persists engine + JSON options; the proxy must read them back.
    store = {
        ("centaur_engine", "engine"): "maia",
        ("centaur_engine", "options"): json.dumps({"UCI_Elo": 1200}),
    }
    cfg = load_proxy_config(lambda s, k, d: store.get((s, k), d))
    assert cfg == ProxyConfig(engine_name="maia", options={"UCI_Elo": 1200})


def test_load_proxy_config_tolerates_bad_options_json():
    # A corrupt options value must not break launch; it yields no options so the
    # engine runs at defaults (still under the memory floor).
    cfg = load_proxy_config(lambda s, k, d: "not json" if k == "options" else d)
    assert cfg.engine_name == "stockfish"
    assert cfg.options == {}


# ---------------------------------------------------------------------------
# End-to-end stream wiring
# ---------------------------------------------------------------------------


class _Capture:
    """Writable stream that records lines and tolerates close() (unlike a closed
    StringIO, whose getvalue() would raise after run_proxy closes engine stdin)."""

    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass

    def close(self):
        pass

    def text(self):
        return "".join(self.lines)


def test_run_proxy_forwards_rewrites_injects_and_records(db):
    """Full pass: Centaur's stream is forwarded with the floor + injected options,
    the engine's replies reach Centaur, and the game is recorded.

    This is the spike in test form -- it exercises the same path Centaur drives:
    uci/isready handshake, an oversized Hash setoption, a position, then go. It
    asserts (1) the engine receives the clamped Hash and the injected option
    placed immediately before go, (2) Centaur receives the engine's bestmove, and
    (3) the move is recorded. A regression in any of the three shows here.
    """
    session, models = db
    tracker = PositionTracker()
    recorder = GameRecorder(session, source="centaur", models=models)

    centaur_in = [
        "uci\n",
        "setoption name Hash value 128\n",
        "isready\n",
        "position startpos moves e2e4\n",
        "go movetime 100\n",
    ]
    engine_out = ["uciok\n", "readyok\n", "bestmove e7e5\n"]
    engine_in = _Capture()
    centaur_out = _Capture()

    run_proxy(
        centaur_in,
        centaur_out,
        engine_in,
        engine_out,
        tracker=tracker,
        recorder=recorder,
        inject_options=["setoption name UCI_Elo value 1500"],
    )

    forwarded = engine_in.text()
    # Hash clamped to the floor on the way to the engine.
    assert f"setoption name Hash value {MEMORY_SAFE_HASH_MAX_MB}" in forwarded
    assert "setoption name Hash value 128" not in forwarded
    # Injected option appears immediately before the first go.
    assert "setoption name UCI_Elo value 1500\ngo movetime 100\n" in forwarded
    # Position forwarded verbatim.
    assert "position startpos moves e2e4\n" in forwarded

    # Engine replies are pumped back to Centaur.
    assert "bestmove e7e5" in centaur_out.text()
    assert "uciok" in centaur_out.text()

    # The move was recorded.
    moves = session.query(models.GameMove).order_by(models.GameMove.id).all()
    assert [m.move for m in moves] == ["", "e2e4"]


@pytest.mark.parametrize(
    "line,expected",
    [
        ("id name ct800", True),
        ("option name Hash type spin default 16", True),
        ("uciok", True),
        ("info depth 1 score cp 12", True),
        ("bestmove e7e5", True),
        ("Starting engine...", False),
        ("No such option: MultiPV", False),
        ("optionally logging", False),
        ("", False),
    ],
)
def test_is_uci_engine_output_line(line, expected):
    """Only UCI tokens may be forwarded to Centaur.

    Why this test exists: non-Stockfish engines print banners and "No such
    option" on stdout; Centaur was written against Stockfish and dies on those
    lines. How the regression manifests: a banner is classified True (forwarded)
    or a real ``info``/``uciok`` is classified False (Centaur never handshakes).
    """
    assert is_uci_engine_output_line(line) is expected


def test_parse_advertised_option_name_handles_spaces():
    """UCI option names may contain spaces; the name stops at `` type ``.

    Why this test exists: ``Skill Level`` must be recorded as that name, not
    ``Skill``. How the regression manifests: the parsed name is truncated or
    a non-option line returns a string instead of None.
    """
    assert parse_advertised_option_name(
        "option name Skill Level type spin default 20"
    ) == "Skill Level"
    assert parse_advertised_option_name("uciok") is None


def test_allows_setoption_drops_unknown_names_once_engine_advertised():
    """Centaur's Stockfish setoptions must not reach an engine that lacks them.

    Why this test exists: Original Centaur always sends Hash/MultiPV/Skill;
    engines that do not implement those options have been observed to exit,
    taking the session down. An empty advertised set must still forward (the
    handshake was not observed -- existing stream tests send only uciok).
    How the regression manifests: MultiPV is allowed for an engine that only
    advertised Hash, or Hash is dropped when nothing was advertised.
    """
    advertised = {"hash"}
    assert allows_setoption(advertised, "setoption name Hash value 16") is True
    assert allows_setoption(advertised, "setoption name MultiPV value 1") is False
    assert allows_setoption(set(), "setoption name MultiPV value 1") is True


def test_run_proxy_drops_unknown_setoptions_and_engine_banners():
    """A non-Stockfish engine must not see Stockfish-only setoptions or banners.

    Why this test exists: this is the Original Centaur "any engine other than
    Stockfish crashes" failure. The engine advertises only Hash; Centaur still
    sends MultiPV. The banner must not reach Centaur; MultiPV must not reach
    the engine; Hash still must (clamped). How the regression manifests: MultiPV
    appears in engine_in, or the banner appears in centaur_out.
    """
    centaur_in = [
        "uci\n",
        "setoption name Hash value 128\n",
        "setoption name MultiPV value 10\n",
        "isready\n",
        "go movetime 100\n",
    ]
    engine_out = [
        "Starting up...\n",
        "id name ct800\n",
        "option name Hash type spin default 16 min 1 max 128\n",
        "uciok\n",
        "readyok\n",
        "bestmove e7e5\n",
    ]
    engine_in = _Capture()
    centaur_out = _Capture()

    run_proxy(
        centaur_in,
        centaur_out,
        engine_in,
        engine_out,
        inject_options=["setoption name UCI_Elo value 1500"],
    )

    forwarded = engine_in.text()
    assert f"setoption name Hash value {MEMORY_SAFE_HASH_MAX_MB}" in forwarded
    assert "MultiPV" not in forwarded
    assert "UCI_Elo" not in forwarded

    to_centaur = centaur_out.text()
    assert "Starting up" not in to_centaur
    assert "id name ct800" in to_centaur
    assert "uciok" in to_centaur
    assert "bestmove e7e5" in to_centaur


# ---------------------------------------------------------------------------
# Engine resolution (no SD fallback)
# ---------------------------------------------------------------------------


def test_resolve_engine_command_uses_configured_uc_engine(monkeypatch):
    """The proxy runs the configured UC engine when it resolves.

    The hook's purpose is that Centaur plays a UC engine; resolution must return
    the resolved path as the command to exec. A regression here would launch the
    wrong (or no) engine.
    """
    from universalchess.services.centaur_engine_proxy import proxy as proxy_mod

    monkeypatch.setattr(
        "universalchess.paths.get_engine_path",
        lambda name: "/opt/universalchess/engines/stockfish" if name == "stockfish" else None,
    )
    cfg = ProxyConfig(engine_name="stockfish", options={})
    assert proxy_mod._resolve_engine_command(cfg) == ["/opt/universalchess/engines/stockfish"]


def test_resolve_engine_command_returns_none_when_engine_missing(monkeypatch):
    """With no SD fallback, an unresolved engine yields None (caller fails loudly).

    The stockfish_pi.real fallback was removed on purpose: UC always ships
    Stockfish, so an unresolved engine means a real misconfiguration and must
    surface as a clear error, not silently play the SD's old engine. This pins
    that there is no fallback path -- if one were reintroduced, this returns a
    command instead of None and the test fails.
    """
    from universalchess.services.centaur_engine_proxy import proxy as proxy_mod

    monkeypatch.setattr("universalchess.paths.get_engine_path", lambda name: None)
    cfg = ProxyConfig(engine_name="does-not-exist", options={})
    assert proxy_mod._resolve_engine_command(cfg) is None


# ---------------------------------------------------------------------------
# Engine hook launcher (env hygiene for Centaur's cwd)
# ---------------------------------------------------------------------------


def test_render_launcher_sets_pythonsafepath_and_strips_ld_preload():
    """The launcher must neutralize Centaur's cwd and inherited LD_PRELOAD.

    Why this test exists: Centaur execs the proxy with cwd ``~/centaur``, which
    holds the original app's Python 3.5 ``.so`` files. Without PYTHONSAFEPATH
    that cwd lands on sys.path and ``import sqlite3`` loads Centaur's 3.5
    ``_sqlite3.so``, which fails and silently disabled DB recording. LD_PRELOAD
    (the display shim) is likewise inherited and must be dropped for the engine.

    How a regression manifests: if either token is missing the launcher reverts
    to the broken behavior -- recording silently off (no PYTHONSAFEPATH) or the
    shim loaded into the engine/proxy (no ``-u LD_PRELOAD``).
    """
    script = render_launcher(python_bin="/venv/python", pythonpath="/opt")

    assert "PYTHONSAFEPATH=1" in script
    assert "-u LD_PRELOAD" in script
    # Still resolves the package and execs the proxy module under the venv python.
    assert 'PYTHONPATH="/opt"' in script
    assert "-m universalchess.services.centaur_engine_proxy" in script
    assert script.startswith("#!/bin/sh\n")
    assert "exec env" in script


# ---------------------------------------------------------------------------
# Web-state publisher (mirrors the reconstructed game to fen.log + broadcast)
# ---------------------------------------------------------------------------


class _PublishCapture:
    """Records the fen.log writes and broadcast payloads the publisher emits."""

    def __init__(self):
        self.fens = []
        self.broadcasts = []

    def write_fen_log(self, fen):
        self.fens.append(fen)

    def broadcast(self, **kwargs):
        self.broadcasts.append(kwargs)
        return True


def test_publisher_mirrors_board_to_fen_log_and_broadcast():
    """A reconstructed board is published as full FEN + derived game metadata.

    Why this test exists: this is the bridge that keeps the web control page in
    sync during Centaur play; nothing else feeds it. Asserts the full payload
    (FEN, turn, move number, last move) so a wrong derivation -- which would draw
    the wrong board or move list on the web -- is caught, not just presence.

    How a regression manifests: e.g. last_move drops or turn flips, so the web
    highlights the wrong move / side to move.
    """
    cap = _PublishCapture()
    publisher = CentaurStatePublisher(cap.write_fen_log, cap.broadcast)
    tracker = PositionTracker()
    tracker.update(None, ["e2e4", "e7e5"])  # 1.e4 e5, white to move on move 2

    publisher.publish(tracker.board)

    assert len(cap.fens) == 1
    # Full FEN (with fields) is written so /video and Chromecast get the position.
    assert cap.fens[0].startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w ")
    assert len(cap.broadcasts) == 1
    payload = cap.broadcasts[0]
    assert payload["fen"].startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w ")
    assert payload["turn"] == "w"
    assert payload["move_number"] == 2
    assert payload["last_move"] == "e7e5"
    assert payload["game_over"] is False
    assert payload["result"] is None
    # PGN is standard SAN (what the web move list renders), not UCI.
    assert "1. e4 e5" in payload["pgn"]


def test_publisher_reports_game_over_with_result_and_termination():
    """A terminal position publishes game_over with result and termination.

    Why this test exists: the web shows the end state for Centaur games too. Uses
    Fool's mate (1.f3 e5 2.g4 Qh4#), the same fixture the tracker test uses, so a
    regression in outcome plumbing surfaces here as a missing result/termination.

    How a regression manifests: game_over stays False or result is None, so the
    web never shows the Centaur game ending.
    """
    cap = _PublishCapture()
    publisher = CentaurStatePublisher(cap.write_fen_log, cap.broadcast)
    tracker = PositionTracker()
    tracker.update(None, ["f2f3", "e7e5", "g2g4", "d8h4"])

    publisher.publish(tracker.board)

    payload = cap.broadcasts[0]
    assert payload["game_over"] is True
    assert payload["result"] == "0-1"
    assert payload["termination"] == "checkmate"


def test_publisher_is_noop_without_a_board():
    """publish(None) does nothing (no position seen yet), emitting no side effects.

    Why this test exists: run_proxy calls publish(tracker.board) and the board is
    None before the first position; a no-op avoids a spurious empty broadcast.

    How a regression manifests: a None board would raise or broadcast garbage,
    pushing an invalid state to the web.
    """
    cap = _PublishCapture()
    publisher = CentaurStatePublisher(cap.write_fen_log, cap.broadcast)

    publisher.publish(None)

    assert cap.fens == []
    assert cap.broadcasts == []


def test_publisher_swallows_sink_errors():
    """A broadcast/IO failure is logged, not raised, so engine play is never hit.

    Why this test exists: the publisher runs inline on Centaur's UCI thread; a
    raised exception would propagate into run_proxy's loop. The contract is
    best-effort mirroring. Asserts the error is captured via log_fn and publish
    returns normally.

    How a regression manifests: an unguarded sink error would bubble up and could
    stall or kill the proxy mid-game.
    """
    logs = []

    def boom(fen):
        raise OSError("disk full")

    publisher = CentaurStatePublisher(boom, lambda **k: True, log_fn=logs.append)
    tracker = PositionTracker()
    tracker.update(None, ["e2e4"])

    publisher.publish(tracker.board)  # must not raise

    assert any("web state publish error" in m for m in logs)


def test_run_proxy_publishes_after_each_position(db):
    """run_proxy feeds the reconstructed board to the publisher per position.

    Why this test exists: wiring is what actually keeps the web live; the
    publisher being correct is useless if run_proxy never calls it. Asserts a
    broadcast carrying the position after the forwarded move.

    How a regression manifests: dropping the publisher call (or calling it with
    the wrong board) leaves the web frozen exactly as the bug reported.
    """
    session, models = db
    tracker = PositionTracker()
    recorder = GameRecorder(session, source="centaur", models=models)
    cap = _PublishCapture()
    publisher = CentaurStatePublisher(cap.write_fen_log, cap.broadcast)

    centaur_in = ["position startpos moves e2e4\n", "go movetime 10\n"]
    engine_out = ["bestmove e7e5\n"]

    run_proxy(
        centaur_in,
        _Capture(),
        _Capture(),
        engine_out,
        tracker=tracker,
        recorder=recorder,
        publisher=publisher,
    )

    assert len(cap.broadcasts) == 1
    assert cap.broadcasts[0]["last_move"] == "e2e4"
    assert cap.broadcasts[0]["turn"] == "b"


def test_run_proxy_debug_traces_position_stream_only_when_enabled():
    """The per-position debug trace is emitted only when debug=True.

    Why this test exists: the trace is the diagnostic kept in the code to validate
    Centaur's position forms (e.g. takebacks) on-device via UC_CENTAUR_PROXY_DEBUG.
    It must stay silent by default (it is per-move noise) and, when enabled, log
    the command with the tracker's classification. Parameterized over the flag in
    one test so the on/off contract is pinned together.

    How a regression manifests: log lines appear with debug off (noise in the
    modmenu log) or are missing/incorrect with debug on (no diagnostic to read).
    """
    tracker = PositionTracker()
    logs: list[str] = []

    run_proxy(
        ["position startpos moves e2e4\n", "go movetime 10\n"],
        _Capture(),
        _Capture(),
        ["bestmove e7e5\n"],
        tracker=tracker,
        log_fn=logs.append,
        debug=False,
    )
    assert [m for m in logs if "[debug]" in m] == []

    tracker_on = PositionTracker()
    logs_on: list[str] = []
    run_proxy(
        ["position startpos moves e2e4\n", "go movetime 10\n"],
        _Capture(),
        _Capture(),
        ["bestmove e7e5\n"],
        tracker=tracker_on,
        log_fn=logs_on.append,
        debug=True,
    )
    traces = [m for m in logs_on if "[debug]" in m]
    assert len(traces) == 1
    assert "position startpos moves e2e4" in traces[0]
    assert "added=['e2e4']" in traces[0]
    assert "total=1" in traces[0]


# ---------------------------------------------------------------------------
# Terminal engine move (the game-ending bestmove that gets no follow-up position)
# ---------------------------------------------------------------------------


def test_tracker_commits_engine_bestmove_only_when_it_ends_game():
    """A bestmove is committed to the mainline only if it ends the game.

    Why this test exists: this is the exact bug reported on hardware -- the
    engine's mating move (the player's final M1) was missing from the live board
    and the DB. Centaur sends no ``position`` after a game-ending move, so the
    tracker must commit that move from the engine's ``bestmove`` instead. A
    non-terminal bestmove must NOT be committed (it re-arrives in the next
    ``position``; committing early would diverge and double-count). Uses Fool's
    mate: after 1.f3 e5 2.g4 it is Black to move with Qh4#.

    How a regression manifests: the mating bestmove returns None (final move lost
    again) or a non-terminal bestmove is committed (total jumps / record diverges
    from the confirmed position stream).
    """
    tracker = PositionTracker()
    tracker.update(None, ["f2f3", "e7e5", "g2g4"])  # Black to move, Qh4 is mate

    # A legal but non-terminal reply must be ignored (handled via position stream).
    non_terminal = tracker.apply_terminal_engine_move("b8c6")
    assert non_terminal is None
    assert [m.uci() for m in tracker.board.move_stack] == ["f2f3", "e7e5", "g2g4"]

    # An illegal move (wrong side / not legal here) is ignored, not raised.
    assert tracker.apply_terminal_engine_move("e2e4") is None
    # A null/no-move token from the engine is ignored.
    assert tracker.apply_terminal_engine_move("0000") is None

    # The mating move ends the game, so it is committed with result/termination.
    mate = tracker.apply_terminal_engine_move("d8h4")
    assert mate is not None
    assert mate.is_new_game is False
    assert [m for m, _ in mate.moves_added] == ["d8h4"]
    assert mate.moves_removed == 0
    assert mate.total_moves == 4
    assert mate.result == "0-1"
    assert mate.termination == "checkmate"
    assert [m.uci() for m in tracker.board.move_stack] == ["f2f3", "e7e5", "g2g4", "d8h4"]


def test_tracker_apply_terminal_engine_move_noop_before_any_game():
    """Before a game starts, a bestmove cannot be committed (no board to apply to).

    Why this test exists: the pump thread may read engine output before the first
    position is processed; apply_terminal_engine_move must be a safe no-op then,
    not raise or fabricate a board.

    How a regression manifests: an AttributeError on the None board, or a spurious
    game being created from a stray bestmove.
    """
    tracker = PositionTracker()
    assert tracker.apply_terminal_engine_move("e2e4") is None
    assert tracker.board is None


def test_run_proxy_records_and_publishes_engine_mate(db):
    """run_proxy records + publishes the engine's mating move (no trailing position).

    Why this test exists: the end-to-end guard for the reported bug. Centaur sends
    the position before the mate and ``go``, the engine returns the mating
    ``bestmove``, and NO further position arrives (the game is over). The proxy
    must still record the final move and publish the terminal board. The engine
    stream is gated so the bestmove is only emitted after ``go`` is pulled (which
    happens only after the preceding position is fully processed), reproducing the
    real ordering deterministically without a sleep.

    How a regression manifests: the DB game stops at g2g4 (mate lost) exactly as
    reported, or no game-over broadcast is published to the web.
    """
    session, models = db
    tracker = PositionTracker()
    recorder = GameRecorder(session, source="centaur", models=models)
    cap = _PublishCapture()
    publisher = CentaurStatePublisher(cap.write_fen_log, cap.broadcast)

    go_pulled = threading.Event()

    def centaur_stream():
        yield "position startpos moves f2f3 e7e5 g2g4\n"
        # Reached only when the loop pulls the next line, i.e. after the position
        # above has been fully processed (tracker updated, recorded, published).
        go_pulled.set()
        yield "go movetime 10\n"

    def engine_stream():
        # Hold the mating bestmove until the position has been processed, matching
        # Centaur: the engine only answers a go that follows the position.
        assert go_pulled.wait(timeout=5)
        yield "bestmove d8h4\n"

    centaur_out = _Capture()
    run_proxy(
        centaur_stream(),
        centaur_out,
        _Capture(),
        engine_stream(),
        tracker=tracker,
        recorder=recorder,
        publisher=publisher,
    )

    # One game, ending on the engine's mate, with the result stamped.
    games = session.query(models.Game).all()
    assert len(games) == 1
    assert games[0].result == "0-1"
    moves = session.query(models.GameMove).order_by(models.GameMove.id).all()
    assert [m.move for m in moves] == ["", "f2f3", "e7e5", "g2g4", "d8h4"]

    # The terminal board is published to the web as game over.
    assert cap.broadcasts[-1]["last_move"] == "d8h4"
    assert cap.broadcasts[-1]["game_over"] is True
    assert cap.broadcasts[-1]["result"] == "0-1"
    # The bestmove is still forwarded to Centaur (recording is a side channel).
    assert "bestmove d8h4" in centaur_out.text()
