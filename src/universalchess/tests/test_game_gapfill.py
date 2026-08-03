"""Tests for on-demand analysis of a stored game's unanalysed plies.

Why these tests exist
---------------------
The browser no longer runs its own engine, so a game whose plies were never
evaluated by the board -- imported PGNs, games played with ``analysis_mode``
off, games from before the board persisted evaluations -- has nothing to draw
on the review page's eval chart. Gap-fill hands those positions to the board's
existing analysis queue and writes each result back to its own move row.

How a regression manifests
--------------------------
Re-analysing plies that already have a score wastes minutes of Pi CPU on a long
game and, worse, would overwrite stored results. Losing the FEN-to-game mapping
writes an evaluation onto the wrong game's row -- silent data corruption that
only shows up as a nonsensical chart much later.
"""

import chess
import pytest

from universalchess.services.analysis import AnalysisService, PositionAnalysis
from universalchess.services.game_gapfill import GameGapFiller, plies_needing_analysis
from universalchess.state.analysis import reset_analysis
from universalchess.state.chess_game import reset_chess_game


START = chess.STARTING_FEN
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
AFTER_E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
AFTER_NF3 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"

# (move_uci, fen, eval_score) rows in insertion order, initial position first.
GAME_ROWS = [
    ("", START, None),
    ("e2e4", AFTER_E4, None),
    ("e7e5", AFTER_E5, 25),
    ("g1f3", AFTER_NF3, None),
]


# ---------------------------------------------------------------------------
# Selecting the plies that need work
# ---------------------------------------------------------------------------


def test_only_plies_without_an_evaluation_are_selected():
    """An already-scored ply is left alone.

    Regression: re-queuing scored plies turns a 60-move game into minutes of
    redundant search on a Pi Zero 2 W, and overwrites results the board already
    produced -- possibly at a longer time limit than the gap-fill uses.
    """
    boards = plies_needing_analysis(GAME_ROWS, START, chess960=False)

    assert [b.fen() for b in boards] == [AFTER_E4, AFTER_NF3]


def test_the_initial_position_row_is_never_queued():
    """The start position is not a ply and carries no evaluation.

    Regression: queuing it adds a phantom data point before move 1 and writes
    an eval onto the row the resume path reads as the game's start.
    """
    boards = plies_needing_analysis([("", START, None)], START, chess960=False)

    assert boards == []


def test_a_fully_analysed_game_needs_no_work():
    """Nothing is queued when every ply already has a score.

    Regression manifests as a "gap-fill" that always re-analyses everything,
    so the button is never a no-op and always costs a full re-analysis.
    """
    rows = [("", START, None), ("e2e4", AFTER_E4, 20), ("e7e5", AFTER_E5, 5)]

    assert plies_needing_analysis(rows, START, chess960=False) == []


def test_a_zero_evaluation_counts_as_analysed():
    """0 is a real evaluation, not a missing one.

    Regression: treating 0 as absent re-analyses every dead-equal position
    forever, and is the same null-vs-zero confusion the persistence fix
    removed.
    """
    rows = [("", START, None), ("e2e4", AFTER_E4, 0)]

    assert plies_needing_analysis(rows, START, chess960=False) == []


def test_chess960_boards_carry_the_variant_flag():
    """A 960 game is replayed on a board that knows it is 960.

    Regression manifests as a raised or skipped ply: the king-onto-rook castle
    encoding is illegal on a standard board, so replay stops at the castle and
    every later ply silently goes unanalysed.
    """
    start = "4k3/8/8/8/8/8/8/5K1R w K - 0 1"
    after_castle = "4k3/8/8/8/8/8/8/5RK1 b - - 1 1"
    rows = [("", start, None), ("f1h1", after_castle, None)]

    boards = plies_needing_analysis(rows, start, chess960=True)

    assert len(boards) == 1
    assert boards[0].chess960 is True
    assert boards[0].fen() == after_castle


def test_replay_stops_at_a_move_it_cannot_apply():
    """A corrupt move ends the replay instead of raising.

    Regression: an exception here aborts the whole gap-fill, so one bad row in
    an imported game means no ply gets analysed. Stopping keeps the plies
    before it usable.
    """
    rows = [("", START, None), ("not-a-move", AFTER_E4, None), ("e7e5", AFTER_E5, None)]

    boards = plies_needing_analysis(rows, START, chess960=False)

    assert boards == []


# ---------------------------------------------------------------------------
# Queuing and persisting
# ---------------------------------------------------------------------------


class FakeService:
    """AnalysisService stand-in recording queued positions and listeners."""

    def __init__(self):
        self.queued = []
        self.listeners = []

    def analyze_position(self, board):
        self.queued.append(board.fen())

    def on_position_analysed(self, callback):
        self.listeners.append(callback)

    def remove_position_listener(self, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def emit(self, result):
        for callback in list(self.listeners):
            callback(result)


@pytest.fixture
def filler():
    service = FakeService()
    persisted = []
    gap_filler = GameGapFiller(
        service, lambda game_db_id, result: persisted.append((game_db_id, result)))
    return gap_filler, service, persisted


def test_every_unanalysed_ply_is_queued(filler):
    """Gap-fill hands each missing ply to the board's analysis queue.

    Regression: queuing none (or only the first) leaves the chart with the same
    gaps the user pressed the button to fill.
    """
    gap_filler, service, _persisted = filler

    queued = gap_filler.fill(7, GAME_ROWS, START, chess960=False)

    assert queued == 2
    assert service.queued == [AFTER_E4, AFTER_NF3]


def test_results_are_persisted_against_the_requesting_game(filler):
    """Each result is written to the game it was requested for.

    Regression: opening positions and transpositions recur across games, so a
    result matched by FEN alone lands on an unrelated game's row -- corruption
    that surfaces only as a wrong chart much later.
    """
    gap_filler, service, persisted = filler
    gap_filler.fill(7, GAME_ROWS, START, chess960=False)

    service.emit(PositionAnalysis(AFTER_E4, 30, None, "e7e5"))

    assert len(persisted) == 1
    game_db_id, result = persisted[0]
    assert game_db_id == 7
    assert result.fen == AFTER_E4
    assert result.best_move == "e7e5"


def test_a_result_for_a_position_we_did_not_queue_is_ignored(filler):
    """Only the plies this fill asked for are written back.

    The live game keeps analysing while a gap-fill runs, so unrelated results
    flow through the same listener. Regression manifests as the live game's
    evaluations being written onto the stored game under review.
    """
    gap_filler, service, persisted = filler
    gap_filler.fill(7, GAME_ROWS, START, chess960=False)

    service.emit(PositionAnalysis("some-other-position", 30, None, "e2e4"))

    assert persisted == []


def test_an_already_scored_ply_is_not_written_again(filler):
    """A result for a ply that was not queued does not overwrite its score.

    Regression: the live board may re-analyse a position this game also
    contains; accepting it would replace a stored evaluation with one produced
    for a different game.
    """
    gap_filler, service, persisted = filler
    gap_filler.fill(7, GAME_ROWS, START, chess960=False)

    service.emit(PositionAnalysis(AFTER_E5, 999, None, "g1f3"))

    assert persisted == []


def test_the_listener_is_removed_once_every_ply_is_filled(filler):
    """Gap-fill detaches itself when its work is done.

    Regression manifests as an accumulating listener per gap-fill request: each
    one keeps inspecting every later analysis result for the rest of the
    process's life.
    """
    gap_filler, service, _persisted = filler
    gap_filler.fill(7, GAME_ROWS, START, chess960=False)
    assert service.listeners

    service.emit(PositionAnalysis(AFTER_E4, 30, None, "e7e5"))
    assert service.listeners            # one ply still outstanding

    service.emit(PositionAnalysis(AFTER_NF3, -5, None, "b8c6"))
    assert service.listeners == []


def test_a_game_with_nothing_to_fill_registers_no_listener(filler):
    """A fully analysed game does no work at all.

    Regression: registering a listener that can never fire leaks it forever.
    """
    gap_filler, service, _persisted = filler
    rows = [("", START, None), ("e2e4", AFTER_E4, 20)]

    assert gap_filler.fill(7, rows, START, chess960=False) == 0
    assert service.queued == []
    assert service.listeners == []


# ---------------------------------------------------------------------------
# Queuing an arbitrary position on the real service
# ---------------------------------------------------------------------------


def test_analyze_position_queues_a_board_outside_the_live_game():
    """A stored game's position can be analysed without disturbing live play.

    Regression: routing gap-fill through the live-game queue path would analyse
    whatever is on the board instead, and would append the result to the
    e-paper's running eval graph for the game actually in progress.
    """
    reset_chess_game()
    reset_analysis()
    service = AnalysisService()
    board = chess.Board(AFTER_E4)

    service.analyze_position(board)

    assert service._analysis_queue.qsize() == 1
    queued_board, fen, add_to_history, _limit, _generation, is_new_ply = \
        service._analysis_queue.get_nowait()
    assert fen == AFTER_E4
    assert queued_board.fen() == AFTER_E4
    assert add_to_history is False    # must not extend the live game's graph
    assert is_new_ply is False        # must not count towards accuracy
