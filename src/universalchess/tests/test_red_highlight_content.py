"""Tests for the red highlight content rules (three-color mode, Phase 4).

Why these tests exist:
    Three-color mode highlights specific chess events in red. These pin WHICH
    pixels go red and guard the square geometry (a red highlight on the wrong
    square is worse than none):
      - chess board: the checked king + checker; else the side-to-move's own
        threatened queen + its attacker -- mapped to the same 16x16 cells
        render() draws;
      - game over: the winner/result line;
      - analysis graph: the losing-side (negative) evaluation bars only.

How a regression manifests:
    - Board geometry drift: the red cell no longer aligns with the piece (e.g. a
      flip or rank-mirror bug puts red on the opposite square).
    - Over/under-highlight: a quiet position paints red, or a check paints none.
    - Graph polarity bug: positive (winning) bars turn red, or negative bars do
      not.
    - Game-over leak: red appears before a result is set.
"""

import sys
import unittest
from unittest.mock import MagicMock
from concurrent.futures import Future

for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image


def _count_red(image, box=None):
    """Count red pixels (value 0) in the whole image or a (l,t,r,b) sub-box."""
    region = image.crop(box) if box else image
    return sum(1 for px in region.getdata() if px == 0)


# --- Chess board -----------------------------------------------------------

class ChessBoardRedTests(unittest.TestCase):
    """The board reddens check/queen-threat squares at the correct cells."""

    # Fool's mate end: white king e1 in check from the black queen on h4.
    CHECK_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    # White to move, not in check; white's OWN queen d1 is attacked by the black
    # rook d8 down the open d-file. The alert warns the side to move about its own
    # queen (parallel to CHECK), not the enemy queen it could capture.
    QUEEN_THREAT_FEN = "3rk3/8/8/8/8/8/8/3QK3 w - - 0 1"
    START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    def _widget(self, fen, flip=False):
        from universalchess.state.chess_game import reset_chess_game
        from universalchess.epaper.chess_board import ChessBoardWidget
        # All-white synthetic sprite sheet: piece glyphs are blank, so red shows
        # purely as the per-square cell outline -> deterministic geometry checks.
        sprites = Image.new('1', (208, 32), 255)
        widget = ChessBoardWidget(0, 0, MagicMock(return_value=Future()),
                                  reset_chess_game(), flip=flip, sprites=sprites)
        widget.fen = fen
        return widget

    def _render_red(self, fen, flip=False):
        sprite = Image.new('1', (128, 128), 255)
        self._widget(fen, flip=flip).render_red(sprite)
        return sprite

    def test_check_marks_king_and_checker_cells(self):
        # King e1 -> cell (64,112); checker h4 -> cell (112,64). A control empty
        # corner (a8 -> (0,0)) must stay clear. Regression: wrong/zero cells.
        sprite = self._render_red(self.CHECK_FEN)
        self.assertGreater(_count_red(sprite, (64, 112, 80, 128)), 0, "king e1 cell")
        self.assertGreater(_count_red(sprite, (112, 64, 128, 80)), 0, "checker h4 cell")
        self.assertEqual(_count_red(sprite, (0, 0, 16, 16)), 0, "a8 control cell")

    def test_check_flip_mirrors_cells(self):
        # With the board flipped, e1 maps to cell (48,0) and h4 to (0,48). Guards
        # the flip transform in _square_to_cell against render() drift.
        sprite = self._render_red(self.CHECK_FEN, flip=True)
        self.assertGreater(_count_red(sprite, (48, 0, 64, 16)), 0, "king e1 flipped")
        self.assertGreater(_count_red(sprite, (0, 48, 16, 64)), 0, "checker h4 flipped")

    def test_queen_threat_marks_own_queen_and_attacker(self):
        # Side-to-move's own queen d1 -> cell (48,112); attacker rook d8 -> cell
        # (48,0). Quiet elsewhere. Regression: if the branch flags the OPPONENT's
        # queen (the old "opportunity" logic) or maps the wrong squares, the red
        # would land on the wrong cell (or nothing, since there is no enemy queen).
        sprite = self._render_red(self.QUEEN_THREAT_FEN)
        self.assertGreater(_count_red(sprite, (48, 112, 64, 128)), 0, "own queen d1 cell")
        self.assertGreater(_count_red(sprite, (48, 0, 64, 16)), 0, "attacker d8 cell")

    def test_quiet_position_has_no_red(self):
        # The opening position is neither check nor queen-threat -> no red at all.
        # Regression: over-highlighting paints red during normal play.
        self.assertEqual(_count_red(self._render_red(self.START_FEN)), 0)


# --- Game over -------------------------------------------------------------

class GameOverRedTests(unittest.TestCase):
    """The game-over widget reddens the winner line only once a result is set."""

    def _widget(self):
        from universalchess.state.chess_game import reset_chess_game
        from universalchess.epaper.game_over import GameOverWidget
        return GameOverWidget(0, 144, 128, 72, MagicMock(return_value=Future()),
                              game_state=reset_chess_game(),
                              led_off_callback=MagicMock())

    def test_result_line_is_red(self):
        # After a result, the winner line (y 4..22) must contain red glyph pixels.
        widget = self._widget()
        widget.set_result("1-0", "CHECKMATE", 10)
        sprite = Image.new('1', (128, 72), 255)
        widget.render_red(sprite)
        self.assertGreater(_count_red(sprite, (0, 4, 128, 22)), 0)
        widget.cleanup()

    def test_no_result_no_red(self):
        # Before any result the widget contributes no red (it is hidden anyway);
        # regression would flash stale red at the start of a game.
        widget = self._widget()
        sprite = Image.new('1', (128, 72), 255)
        widget.render_red(sprite)
        self.assertEqual(_count_red(sprite), 0)
        widget.cleanup()


# --- Analysis graph --------------------------------------------------------

class _FakeAnalysis:
    """Minimal AnalysisState stand-in exposing what the widget reads/subscribes."""

    def __init__(self, history):
        self.history = history
        self.score = history[-1] if history else 0.0
        self.score_text = "+0.0"
        self.annotation = ""
        self.is_mate = False
        self.mate_in = None

    def accuracy_summary(self):
        # Red rendering does not depend on accuracy; an empty summary keeps the
        # quality bar blank so these tests exercise only the graph's red bars.
        from universalchess.utils.accuracy import summarize
        return summarize([])

    def on_score_change(self, cb):
        pass

    def on_history_change(self, cb):
        pass

    def remove_observer(self, cb):
        pass


class AnalysisGraphRedTests(unittest.TestCase):
    """Only the losing-side (negative) evaluation bars are reddened."""

    def _widget(self, history, show_graph=True, bottom_color="white"):
        from universalchess.epaper.game_analysis import GameAnalysisWidget
        return GameAnalysisWidget(0, 216, 128, 80, MagicMock(return_value=Future()),
                                  bottom_color=bottom_color, show_graph=show_graph,
                                  analysis_state=_FakeAnalysis(history))

    def test_negative_bars_are_red(self):
        # History with negative entries (bottom player worse) must produce red.
        sprite = Image.new('1', (128, 80), 255)
        self._widget([-3.0, 2.0, -5.0]).render_red(sprite)
        self.assertGreater(_count_red(sprite), 0)

    def test_all_positive_bars_have_no_red(self):
        # All-winning history -> no negative bars -> no red. Regression: a
        # polarity flip would redden the winning bars.
        sprite = Image.new('1', (128, 80), 255)
        self._widget([1.0, 2.0, 3.0]).render_red(sprite)
        self.assertEqual(_count_red(sprite), 0)

    def test_graph_hidden_has_no_red(self):
        # With the graph hidden, no bars are drawn, so no red regardless of score.
        sprite = Image.new('1', (128, 80), 255)
        self._widget([-3.0, -5.0], show_graph=False).render_red(sprite)
        self.assertEqual(_count_red(sprite), 0)


if __name__ == "__main__":
    unittest.main()
