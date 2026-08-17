"""Tests for GameAnalysisWidget eval/graph rendering.

Why these tests exist
---------------------
The analysis widget is the eval panel only; the played-move scoresheet lives
on MoveListWidget. These pin that rendering a changed eval score does not
trigger an extra display refresh (the analysis view draws directly onto the
sprite). Forwarding a refresh from render would double the analysis refresh
rate and starve the clock.

How a regression manifests
--------------------------
The update_callback would be invoked purely as a side effect of rendering.
"""

from unittest.mock import MagicMock

from PIL import Image

from universalchess.epaper.game_analysis import GameAnalysisWidget
from universalchess.utils.accuracy import AccuracySummary


def test_render_score_change_does_not_request_a_refresh():
    # Rendering a changed eval score must not trigger an extra display refresh:
    # the analysis view draws its text directly onto the sprite, so a render must
    # never invoke the update_callback. Forwarding a refresh from render would
    # double the analysis refresh rate and starve the clock. Regression: the
    # update_callback would be invoked purely as a side effect of rendering.
    analysis_state = MagicMock()
    analysis_state.is_mate = False
    analysis_state.mate_in = None
    analysis_state.annotation = ""
    analysis_state.history = []
    analysis_state.score = 1.0
    analysis_state.score_text = "+1.0"
    analysis_state.accuracy_summary.return_value = AccuracySummary(
        None, None, None, None, "")

    update_callback = MagicMock()
    widget = GameAnalysisWidget(
        0, 216, 128, 80, update_callback,
        analysis_state=analysis_state,
    )

    img = Image.new("1", (widget.width, widget.height), 255)
    widget.render(img)              # first render sets the score text to "+1.0"

    analysis_state.score = 2.0      # change so the next set_text() actually changes
    update_callback.reset_mock()

    widget.render(img)
    update_callback.assert_not_called()
