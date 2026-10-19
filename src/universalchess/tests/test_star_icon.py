"""Tests for the 'star' icon used by the display brightness setting."""

import unittest
from unittest.mock import MagicMock

from universalchess.epaper.icon_button import IconButtonWidget


class TestStarIcon(unittest.TestCase):
    """Ensure the star icon can be rendered without errors.

    Expected failure message: Exception during render due to missing icon handler.
    Failure reason: icon_name 'star' not implemented in IconButtonWidget._draw_icon.
    """

    def test_star_icon_draws_polygon(self):
        """Rendering a button with icon_name='star' draws a star polygon."""
        mock_draw = MagicMock()

        widget = IconButtonWidget(
            0, 0, 128, 64,
            update_callback=lambda *a, **k: None,
            key="led_brightness",
            label="LED: 5",
            icon_name="star",
            selected=False,
        )

        # Call the star icon drawing method directly
        widget._draw_star_icon(mock_draw, x=32, y=32, size=45, line_color=0)

        # Verify polygon was called (star is drawn with outline and fill)
        self.assertGreaterEqual(mock_draw.polygon.call_count, 1, 
                                 "polygon should be called at least once")
        
        # Check the first call has 10 vertices (5-pointed star has 5 outer + 5 inner)
        first_call = mock_draw.polygon.call_args_list[0]
        points = first_call[0][0]  # First positional arg is the points list
        self.assertEqual(len(points), 10, "Star should have 10 vertices")


