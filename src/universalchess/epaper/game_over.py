"""
Game over widget displaying winner, termination reason, and final times.

This widget occupies the same screen area as the clock widget (y=144, height=72).
Both widgets observe ChessGameState and manage their own visibility:
- GameOverWidget shows on game_over, hides on new game
- ChessClockWidget hides on game_over, shows on new game

The analysis widget stays in place (y=216, h=80) to show the
evaluation history graph.

This is the observer pattern - each widget manages its own visibility based on
game state, rather than being externally managed by other widgets.
"""

from PIL import Image, ImageDraw, ImageFont
from .framework.widget import Widget
from .text import TextWidget, Justify
import os
import sys
import logging
from typing import Optional, Tuple

log = logging.getLogger(__name__)


class GameOverWidget(Widget):
    """
    Widget displaying game over information.
    
    Observes ChessGameState and shows/hides itself based on game state:
    - Shows on game_over event with result and termination
    - Hides on position_change when is_game_over becomes False (new game)
    
    Occupies same screen area as clock widget (y=144, h=72). Shows winner,
    termination reason, move count, and final times using TextWidget.
    The clock widget manages its own visibility via game_over observer.
    The analysis widget stays in place (y=216, h=80) to show the
    evaluation history graph.
    """
    
    # Default position: replaces clock widget (board ends at y=144)
    DEFAULT_Y = 144
    DEFAULT_HEIGHT = 72
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 game_state=None,
                 led_off_callback: callable = None):
        """
        Initialize game over widget.
        
        The widget starts hidden and shows itself when it receives a game_over
        event from ChessGameState. It hides itself when a new game starts.
        
        Args:
            x: X position
            y: Y position
            width: Widget width
            height: Widget height
            update_callback: Callback to trigger display updates. Must not be None.
            game_state: Optional ChessGameState to observe. If None, uses singleton.
            led_off_callback: LED callback () to turn off all LEDs. Used on game over.
        """
        super().__init__(x, y, width, height, update_callback)
        self._led_off = led_off_callback
        
        self.result = ""           # "1-0", "0-1", "1/2-1/2"
        self.winner = ""           # "White wins", "Black wins", "Draw"
        self.termination = ""      # "Checkmate", "Stalemate", "Resignation", etc.
        self.move_count = 0        # Number of moves played
        self.white_time: Optional[int] = None  # Final white time in seconds
        self.black_time: Optional[int] = None  # Final black time in seconds
        
        # Start hidden - will show on game_over event
        self.visible = False
        
        # Create TextWidgets for each line - use parent handler for child updates
        self._winner_text = TextWidget(0, 4, width, 18, self._handle_child_update,
                                        text="", font_size=16,
                                        justify=Justify.CENTER, transparent=True)
        self._termination_text = TextWidget(0, 24, width, 16, self._handle_child_update,
                                            text="", font_size=12,
                                            justify=Justify.CENTER, transparent=True)
        self._moves_text = TextWidget(0, 44, width, 14, self._handle_child_update,
                                      text="", font_size=10,
                                      justify=Justify.CENTER, transparent=True)
        self._times_text = TextWidget(0, 58, width, 14, self._handle_child_update,
                                      text="", font_size=10,
                                      justify=Justify.CENTER, transparent=True)
        
        # Subscribe to game state events
        if game_state is None:
            from universalchess.state.chess_game import get_chess_game
            self._game_state = get_chess_game()
        else:
            self._game_state = game_state
        
        self._game_state.on_game_over(self._on_game_over)
        self._game_state.on_position_change(self._on_position_change)
        
        log.debug("[GameOverWidget] Initialized and subscribed to game state")
    
    def cleanup(self) -> None:
        """Unsubscribe from game state when widget is destroyed."""
        if self._game_state:
            self._game_state.remove_observer(self._on_game_over)
            self._game_state.remove_observer(self._on_position_change)
            log.debug("[GameOverWidget] Unsubscribed from game state")
    
    def stop(self) -> None:
        """Stop the widget and clean up subscriptions."""
        self.cleanup()
        super().stop()
    
    def _on_game_over(self, result: str, termination: str) -> None:
        """Handle game_over event from ChessGameState.
        
        Shows the widget with the game result and termination type.
        
        Args:
            result: Game result ('1-0', '0-1', '1/2-1/2')
            termination: How game ended ('checkmate', 'stalemate', etc.)
        """
        log.info(f"[GameOverWidget] Game over: {result} by {termination}")
        
        # Get move count from game state
        move_count = len(self._game_state.move_stack)
        
        # Set result (this also triggers display update)
        self.set_result(result, termination, move_count)
        
        # Show ourselves
        self.show()
    
    def _on_position_change(self) -> None:
        """Handle position_change event from ChessGameState.
        
        If the game is no longer over (new game started), hide ourselves.
        """
        # Only act if we're currently visible and game is no longer over
        if self.visible and not self._game_state.is_game_over:
            log.info("[GameOverWidget] Game reset detected - hiding")
            self.hide()
            
            # Clear our state for the next game
            self.result = ""
            self.winner = ""
            self.termination = ""
            self.move_count = 0
            self.white_time = None
            self.black_time = None
    
    def _handle_child_update(self, full: bool = False, immediate: bool = False):
        """Handle update requests from child widgets by forwarding to parent callback."""
        return self._update_callback(full, immediate)
    
    def set_result(self, result: str, termination: str = None, move_count: int = 0,
                   final_times: Optional[Tuple[int, int]] = None) -> None:
        """
        Set the game result and termination type.
        
        Args:
            result: Game result string ("1-0", "0-1", "1/2-1/2")
            termination: Termination type (e.g., "CHECKMATE", "STALEMATE", "RESIGN")
            move_count: Number of moves played in the game
            final_times: Optional tuple of (white_seconds, black_seconds) for timed games
        """
        changed = False
        
        if self.result != result:
            self.result = result
            changed = True
            
            # Determine winner from result
            if result == "1-0":
                self.winner = "White wins"
            elif result == "0-1":
                self.winner = "Black wins"
            elif result == "1/2-1/2":
                self.winner = "Draw"
            else:
                self.winner = result
        
        if termination is not None and self.termination != termination:
            # Format termination for display
            self.termination = self._format_termination(termination)
            changed = True
        
        if move_count > 0 and self.move_count != move_count:
            self.move_count = move_count
            changed = True
        
        if final_times is not None:
            white_time, black_time = final_times
            if self.white_time != white_time or self.black_time != black_time:
                self.white_time = white_time
                self.black_time = black_time
                changed = True
        
        if changed:
            # request_update() no-ops while hidden, so this is safe when not visible.
            self.invalidate_and_update()
    
    def set_final_times(self, white_seconds: int, black_seconds: int) -> None:
        """Set the final times for display.
        
        Called externally when clock times need to be captured at game end.
        
        Args:
            white_seconds: White's remaining time in seconds.
            black_seconds: Black's remaining time in seconds.
        """
        if self.white_time != white_seconds or self.black_time != black_seconds:
            self.white_time = white_seconds
            self.black_time = black_seconds
            # request_update() no-ops while hidden, so this is safe when not visible.
            self.invalidate_and_update()
    
    def show(self) -> None:
        """Show game over widget and turn off LEDs.
        
        When the game ends, any pending move or check/threat LEDs
        should be turned off to indicate the game is finished.
        """
        if self._led_off:
            self._led_off()
            log.debug("[GameOverWidget] LEDs turned off on game over")
        else:
            log.warning("[GameOverWidget] LED off callback not set, skipping LED off")
        
        super().show()
    
    def _format_termination(self, termination: str) -> str:
        """
        Format termination type for display.
        
        Args:
            termination: Raw termination string (e.g., "CHECKMATE", "Termination.CHECKMATE")
            
        Returns:
            Formatted display string
        """
        if not termination:
            return ""
        
        # Remove "Termination." prefix if present
        term = termination.replace("Termination.", "")
        
        # Convert to readable format - use short forms for compact display
        termination_map = {
            "CHECKMATE": "Checkmate",
            "STALEMATE": "Stalemate",
            "INSUFFICIENT_MATERIAL": "Insuff. material",
            "SEVENTYFIVE_MOVES": "75-move rule",
            "FIVEFOLD_REPETITION": "5x repetition",
            "FIFTY_MOVES": "50-move rule",
            "THREEFOLD_REPETITION": "3x repetition",
            "RESIGN": "Resignation",
            "TIMEOUT": "Time forfeit",
            "TIME_FORFEIT": "Time forfeit",
            "ABANDONED": "Abandoned",
        }
        
        return termination_map.get(term.upper(), term.title())
    
    def _format_time(self, seconds: int) -> str:
        """
        Format time in seconds to display string.
        
        Args:
            seconds: Time in seconds
            
        Returns:
            Formatted time string (M:SS or MM:SS)
        """
        if seconds <= 0:
            return "0:00"
        
        minutes = seconds // 60
        secs = seconds % 60
        
        return f"{minutes}:{secs:02d}"
    
    def render(self, sprite: Image.Image) -> None:
        """
        Render game over widget using TextWidgets.
        
        Layout (72 pixels height):
        - Line 1 (y=4): Winner (e.g., "White wins", "Black wins", "Draw")
        - Line 2 (y=24): Termination reason (e.g., "Checkmate", "Resignation")
        - Line 3 (y=44): Move count (e.g., "42 moves")
        - Line 4 (y=58): Final times if available (e.g., "W:5:23 B:3:17")
        """
        draw = ImageDraw.Draw(sprite)
        
        # Draw background
        self.draw_background_on_sprite(sprite)
        
        # Draw separator line at top
        draw.line([(0, 0), (self.width, 0)], fill=0, width=1)
        
        # Line 1: Winner (centered, large font)
        if self.winner:
            self._winner_text.set_text(self.winner)
            self._winner_text.draw_on(sprite, 0, 4)
        
        # Line 2: Termination reason (centered, medium font)
        if self.termination:
            self._termination_text.set_text(self.termination)
            self._termination_text.draw_on(sprite, 0, 24)
        
        # Line 3: Move count (centered, small font)
        if self.move_count > 0:
            self._moves_text.set_text(f"{self.move_count} moves")
            self._moves_text.draw_on(sprite, 0, 44)
        
        # Line 4: Final times if available (centered, small font)
        if self.white_time is not None and self.black_time is not None:
            white_str = self._format_time(self.white_time)
            black_str = self._format_time(self.black_time)
            self._times_text.set_text(f"W:{white_str}  B:{black_str}")
            self._times_text.draw_on(sprite, 0, 58)
