"""Rendering tests for the analysis view layout (quality bar + accuracies).

These pin the reorganised analysis page: a full-width quality bar coloured by the
last mover, the current move's accuracy % on that bar, the two running
per-player accuracies stacked around the score, and the score vertically centred
on the graph's centre line. They render onto a real canvas and inspect pixels,
because a layout regression (wrong bar colour, missing text, or a mis-aligned
score) would otherwise be invisible to the selection/logic tests.
"""

import unittest

from PIL import Image

from universalchess.epaper.game_analysis import GameAnalysisWidget
from universalchess.utils.accuracy import AccuracySummary


def _noop(*args, **kwargs):
    return None


class _FakeAnalysis:
    """AnalysisState stand-in with a fixed accuracy summary and history."""

    def __init__(self, summary, history=None, score=0.0, is_mate=False, mate_in=None):
        self._summary = summary
        self.history = history or []
        self.score = score
        self.score_text = "+0.0"
        self.annotation = ""
        self.is_mate = is_mate
        self.mate_in = mate_in

    def accuracy_summary(self):
        return self._summary

    def on_score_change(self, cb):
        pass

    def on_history_change(self, cb):
        pass

    def remove_observer(self, cb):
        pass


def _widget(summary, *, bottom_color="white", history=None, score=0.0,
            is_mate=False, mate_in=None, height=80):
    return GameAnalysisWidget(
        0, 216, 128, height, _noop,
        bottom_color=bottom_color,
        show_graph=True,
        analysis_state=_FakeAnalysis(summary, history, score, is_mate, mate_in),
    )


def _render(widget):
    # 1-bit canvas matches the e-paper panel: text is thresholded to pure black
    # (0) / white (255) with no antialiased greys, so exact-black pixel counts
    # are meaningful.
    canvas = Image.new("1", (widget.width, widget.height), 255)
    widget.render(canvas)
    return canvas


def _black_in(image, box):
    """Count black (0) pixels within a (left, top, right, bottom) box."""
    return sum(1 for px in image.crop(box).getdata() if px == 0)


class QualityBarTests(unittest.TestCase):
    """The quality bar's fill encodes the last mover's colour."""

    # Bar interior (inside the widget border), where the fill lives.
    BAR_BOX = (1, 1, 126, GameAnalysisWidget.QUALITY_BAR_HEIGHT)
    BAR_AREA = (126 - 1) * (GameAnalysisWidget.QUALITY_BAR_HEIGHT - 1)

    def test_black_move_fills_bar_black(self):
        # After a black move the bar background is black; most of the bar's pixels
        # are ink. Regression: a colour mix-up would leave it white.
        summary = AccuracySummary(80.0, 60.0, 60.0, False, "Blunder")
        black = _black_in(_render(_widget(summary)), self.BAR_BOX)
        self.assertGreater(black, self.BAR_AREA * 0.5)

    def test_white_move_keeps_bar_white(self):
        # After a white move the bar background is white; only the word/percent
        # text is ink, a small fraction of the bar. Regression: an inverted fill
        # would black out the bar.
        summary = AccuracySummary(80.0, 60.0, 60.0, True, "Blunder")
        black = _black_in(_render(_widget(summary)), self.BAR_BOX)
        self.assertGreater(black, 0)                       # the word/percent text
        self.assertLess(black, self.BAR_AREA * 0.5)

    def test_no_move_leaves_bar_blank(self):
        # Before any move there is no last mover, so the bar is blank white with no
        # word or percent. Regression: rendering a MagicMock/None mover as a real
        # colour, or drawing a stray word, would put ink here.
        summary = AccuracySummary(None, None, None, None, "")
        self.assertEqual(_black_in(_render(_widget(summary)), self.BAR_BOX), 0)

    def test_permanent_divider_below_bar_in_every_state(self):
        # A full-width horizontal line always separates the quality bar from the
        # content below it, whatever the last mover. Regression: a white-move or
        # no-move bar would merge into the graph with no separating edge. The
        # divider row must be (almost) entirely ink for each mover state.
        divider_y = GameAnalysisWidget.QUALITY_BAR_HEIGHT + 1
        row = (1, divider_y, 127, divider_y + 1)
        for summary in (
            AccuracySummary(80.0, 60.0, 60.0, True, "Blunder"),   # white move
            AccuracySummary(80.0, 60.0, 60.0, False, "Brilliant"),  # black move
            AccuracySummary(None, None, None, None, ""),            # no move
        ):
            black = _black_in(_render(_widget(summary)), row)
            self.assertGreaterEqual(black, 126 - 2)  # near-full-width line


class ScoreColumnTests(unittest.TestCase):
    """The score column stacks the two accuracies around a centred score."""

    # Score column interior, split into top-accuracy / middle / bottom-accuracy
    # bands. Column is 0..44 wide; the border sits at x=0. Bands start two rows
    # below the bar so the permanent divider line (at BAR_H + 1) is excluded.
    BAR_H = GameAnalysisWidget.QUALITY_BAR_HEIGHT
    TOP_BAND = (2, BAR_H + 2, 43, BAR_H + 16)
    BOTTOM_BAND = (2, 80 - 17, 43, 79)

    def test_both_accuracies_render_when_present(self):
        # With both players' accuracies known, ink appears in both the top and
        # bottom bands of the score column. Regression: dropping one would leave a
        # band blank.
        summary = AccuracySummary(90.0, 60.0, 60.0, False, "Mistake")
        canvas = _render(_widget(summary))
        self.assertGreater(_black_in(canvas, self.TOP_BAND), 0)
        self.assertGreater(_black_in(canvas, self.BOTTOM_BAND), 0)

    def test_top_band_blank_when_opponent_has_no_accuracy(self):
        # Bottom player (white) has moved but the opponent (black) has not, so only
        # the bottom band shows an accuracy. Regression: showing 0% for a player
        # who has not moved would put ink in the top band.
        summary = AccuracySummary(90.0, None, 90.0, True, "Good")
        canvas = _render(_widget(summary, bottom_color="white"))
        self.assertEqual(_black_in(canvas, self.TOP_BAND), 0)
        self.assertGreater(_black_in(canvas, self.BOTTOM_BAND), 0)

    def test_score_is_vertically_centered_on_graph_centerline(self):
        # The score's ink must straddle the graph centre line so the two read as a
        # single row. Regression: the old top-anchored score sat well above the
        # line. Uses an empty summary so only the score is drawn in the column.
        summary = AccuracySummary(None, None, None, None, "")
        widget = _widget(summary, score=3.0)
        canvas = _render(widget)
        chart_y = widget._graph_geometry()["chart_y"]

        # Rows within the score column that contain score ink. Start below the
        # permanent divider line (at BAR_H + 1) so it is not mistaken for score.
        rows = [y for y in range(self.BAR_H + 2, widget.height - 1)
                if _black_in(canvas, (2, y, 43, y + 1)) > 0]
        self.assertTrue(rows, "score drew no ink in the column")
        center = (rows[0] + rows[-1]) / 2
        self.assertLessEqual(abs(center - chart_y), 3)


if __name__ == "__main__":
    unittest.main()
