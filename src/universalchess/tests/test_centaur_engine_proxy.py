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

import pytest

pytest.importorskip("chess")
pytest.importorskip("sqlalchemy")

from universalchess.services.centaur_engine_proxy import (
    GameRecorder,
    PositionTracker,
    ProxyConfig,
    build_config_setoptions,
    load_proxy_config,
    parse_position_command,
    rewrite_setoption_line,
    run_proxy,
)
from universalchess.services.centaur_engine_proxy.options import (
    MEMORY_SAFE_HASH_MAX_MB,
    MEMORY_SAFE_MULTIPV_MAX,
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


def test_tracker_detects_new_game_on_divergent_move_list():
    """A move list that is not an extension of the prior one starts a new game.

    When Centaur begins a new game its move list resets; relying on the stream
    (not just ucinewgame) makes detection robust. Without this, the new game's
    moves would be appended to the previous game's record.
    """
    tracker = PositionTracker()
    tracker.update(None, ["e2e4", "e7e5", "g1f3"])
    again = tracker.update(None, ["d2d4"])
    assert again.is_new_game is True
    assert [m for m, _ in again.moves_added] == ["d2d4"]
    assert again.total_moves == 1


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

    from universalchess.db import models

    engine = create_engine("sqlite:///:memory:")
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
    """A checkmate stamps the game result, and a divergent list opens a new game.

    Guards two things at once: the result is written to the right game, and a
    subsequent new game gets its own row (not appended). If new-game handling
    regressed, both games' moves would collapse into one record.
    """
    session, models = db
    tracker = PositionTracker()
    recorder = GameRecorder(session, source="centaur", models=models)

    recorder.apply(tracker.update(None, ["f2f3", "e7e5", "g2g4", "d8h4"]))
    first_games = session.query(models.Game).all()
    assert len(first_games) == 1
    assert first_games[0].result == "0-1"

    recorder.apply(tracker.update(None, ["d2d4"]))
    games = session.query(models.Game).order_by(models.Game.id).all()
    assert len(games) == 2
    # Second game has its own move rows (initial + the one move).
    second_moves = session.query(models.GameMove).filter_by(gameid=games[1].id).all()
    assert [m.move for m in second_moves] == ["", "d2d4"]


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
