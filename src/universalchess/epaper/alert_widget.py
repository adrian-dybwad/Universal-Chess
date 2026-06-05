"""
Alert widget for displaying CHECK and QUEEN threat warnings.

Observes ChessGameState and displays alerts when:
- CHECK: When a king is in check, with background color of the side in check
- YOUR QUEEN: When a queen is under attack, with background color of the threatened queen

The widget also triggers LED flashing from the attacking piece to the threatened piece.
Uses TextWidget for all text rendering.
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget
from .text import TextWidget, Justify
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from universalchess.state.chess_game import ChessGameState

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class AlertWidget(Widget):
    """Widget displaying CHECK or QUEEN threat alerts with LED flashing.
    
    Observes ChessGameState for check and queen threat events.
    
    Background color indicates which side is threatened:
    - Black background (white text) = Black piece is threatened
    - White background (black text) = White piece is threatened
    
    Uses TextWidget for text rendering.
    Attacker/target squares trigger LED flashing from attacker to target.
    """
    
    # Alert types
    ALERT_CHECK = "check"
    ALERT_QUEEN = "queen"
    ALERT_HINT = "hint"
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 game_state: 'ChessGameState' = None,
                 led_from_to_hint_callback: Optional[Callable[[int, int, int], None]] = None):
        """
        Initialize alert widget.
        
        Args:
            x: X position
            y: Y position
            width: Widget width
            height: Widget height
            update_callback: Callback to trigger display updates. Must not be None.
            game_state: ChessGameState to observe. If None, uses singleton.
            led_from_to_hint_callback: Callback for LED hint display (from_sq, to_sq, repeat).
                                       Uses slow speed and dim intensity. If None, LED
                                       flashing is skipped.
        """
        super().__init__(x, y, width, height, update_callback)
        self._alert_type = None  # "check", "queen", or "hint"
        self._is_black_threatened = False  # True if black piece is threatened
        self._attacker_square = None  # Square index (0-63) of attacking piece
        self._target_square = None  # Square index (0-63) of threatened piece
        self._hint_text_value = ""  # Hint move text (e.g., "e2e4")
        self.visible = False  # Hidden by default (uses base class attribute)
        
        # LED callback for hints (slow, dim)
        self._led_from_to_hint = led_from_to_hint_callback
        
        # Get or use provided game state
        if game_state is None:
            from universalchess.state import get_chess_game
            self._game_state = get_chess_game()
        else:
            self._game_state = game_state
        
        # Subscribe to check/threat events
        self._game_state.on_check(self._on_check)
        self._game_state.on_queen_threat(self._on_queen_threat)
        self._game_state.on_alert_clear(self._on_alert_clear)
        
        # Create TextWidgets - use parent handler for child updates
        # CHECK: single large centered text
        self._check_text = TextWidget(0, 0, width, height, self._handle_child_update,
                                       text="CHECK", font_size=32,
                                       justify=Justify.CENTER, transparent=True)
        # YOUR QUEEN: two lines centered - use wrap text
        self._queen_text = TextWidget(0, 0, width, height, self._handle_child_update,
                                       text="YOUR\nQUEEN", font_size=18,
                                       justify=Justify.CENTER, wrapText=True,
                                       transparent=True)
        # HINT: shows the suggested move
        self._hint_text = TextWidget(0, 0, width, height, self._handle_child_update,
                                      text="", font_size=28,
                                      justify=Justify.CENTER, transparent=True)
    
    def cleanup(self) -> None:
        """Unsubscribe from game state when widget is destroyed."""
        if self._game_state:
            self._game_state.remove_observer(self._on_check)
            self._game_state.remove_observer(self._on_queen_threat)
            self._game_state.remove_observer(self._on_alert_clear)
    
    def _on_check(self, is_black_in_check: bool, attacker_square: int, king_square: int) -> None:
        """Handle check event from game state."""
        self.show_check(is_black_in_check, attacker_square, king_square)
    
    def _on_queen_threat(self, is_black_threatened: bool, attacker_square: int, queen_square: int) -> None:
        """Handle queen threat event from game state."""
        self.show_queen_threat(is_black_threatened, attacker_square, queen_square)
    
    def _on_alert_clear(self) -> None:
        """Handle alert clear event from game state."""
        self.hide()
    
    def _handle_child_update(self, full: bool = False, immediate: bool = False):
        """Handle update requests from child widgets by forwarding to parent callback."""
        return self._update_callback(full, immediate)

    def _show_with_refresh(self) -> None:
        """Make the widget visible and force its sprite to re-render.

        The alert content (type, colors, text) can change while the widget is
        ALREADY visible - e.g. a QUEEN threat is replaced by CHECK on the very
        next move, with no intervening hide(). Widget.show() only invalidates and
        refreshes on a hidden->visible transition, so a content change while
        visible would leave the stale cached sprite on screen (the "in check but
        YOUR QUEEN is shown" bug). Invalidating unconditionally here forces the
        new alert to render.
        """
        self.visible = True
        self.invalidate_and_update(forced=True)
    
    def show_check(self, is_black_in_check: bool, attacker_square: int, king_square: int) -> None:
        """Show CHECK alert and flash LEDs.
        
        Args:
            is_black_in_check: True if black king is in check, False if white
            attacker_square: Square index (0-63) of the piece giving check
            king_square: Square index (0-63) of the king in check
        """
        self._alert_type = self.ALERT_CHECK
        self._is_black_threatened = is_black_in_check
        self._attacker_square = attacker_square
        self._target_square = king_square
        
        log.info(f"[AlertWidget] Showing CHECK: {'black' if is_black_in_check else 'white'} king in check, attacker={attacker_square}, king={king_square}")
        
        # Flash LEDs from attacker to king (hint style: slow, dim)
        self._flash_leds(repeat=2)
        
        # Force re-render: the alert may already be visible (e.g. switching from
        # a QUEEN threat to CHECK), where Widget.show() alone would not refresh.
        self._show_with_refresh()
    
    def show_queen_threat(self, is_black_queen_threatened: bool, attacker_square: int, queen_square: int) -> None:
        """Show YOUR QUEEN alert and flash LEDs.
        
        Args:
            is_black_queen_threatened: True if black queen is threatened, False if white
            attacker_square: Square index (0-63) of the attacking piece
            queen_square: Square index (0-63) of the threatened queen
        """
        self._alert_type = self.ALERT_QUEEN
        self._is_black_threatened = is_black_queen_threatened
        self._attacker_square = attacker_square
        self._target_square = queen_square
        
        log.info(f"[AlertWidget] Showing QUEEN threat: {'black' if is_black_queen_threatened else 'white'} queen threatened, attacker={attacker_square}, queen={queen_square}")
        
        # Flash LEDs from attacker to queen (hint style: slow, dim)
        self._flash_leds(repeat=1)
        
        # Force re-render: the alert may already be visible (e.g. switching from
        # CHECK to a QUEEN threat), where Widget.show() alone would not refresh.
        self._show_with_refresh()
    
    def show_hint(self, move_text: str, from_square: int, to_square: int) -> None:
        """Show move hint with the suggested move.
        
        Args:
            move_text: Move in readable format (e.g., "e2e4" or "Nf3")
            from_square: Square index (0-63) of the piece to move
            to_square: Square index (0-63) of the target square
        """
        self._alert_type = self.ALERT_HINT
        self._hint_text_value = move_text
        self._attacker_square = from_square
        self._target_square = to_square
        self._is_black_threatened = False  # Not used for hints
        
        log.info(f"[AlertWidget] Showing HINT: {move_text} ({from_square} -> {to_square})")
        
        # Flash LEDs from source to target (hint style: slow, dim)
        self._flash_leds(repeat=2)
        
        # Force re-render: the alert may already be visible when the hint content
        # changes, where Widget.show() alone would not refresh.
        self._show_with_refresh()
    
    def hide(self) -> None:
        """Hide the alert widget and clear alert state."""
        if self.visible:
            self._alert_type = None
            self._attacker_square = None
            self._target_square = None
            log.info("[AlertWidget] Hiding alert")
            # Use base class hide() to handle visibility and update
            super().hide()
    
    def _flash_leds(self, repeat: int = 2) -> None:
        """Flash LEDs from attacker square to target square using hint callback.
        
        Uses slow speed and dim intensity via the led_from_to_hint callback.
        Does nothing if callback not set.
        """
        if self._attacker_square is None or self._target_square is None:
            return
        
        if self._led_from_to_hint is None:
            log.warning("[AlertWidget] LED callback not set, skipping LED flash")
            return
        
        try:
            self._led_from_to_hint(self._attacker_square, self._target_square, repeat)
        except Exception as e:
            log.error(f"[AlertWidget] Error flashing LEDs: {e}")
    
    
    def render(self, sprite: Image.Image) -> None:
        """Render alert widget using TextWidgets.
        
        Draws nothing if not visible. Otherwise draws CHECK or YOUR QUEEN with appropriate colors.
        """
        if not self.visible or self._alert_type is None:
            # Just draw white background
            self.draw_background_on_sprite(sprite)
            return
        
        draw = ImageDraw.Draw(sprite)
        
        # Determine colors based on which side is threatened
        # Black threatened = black background (fill=0), white text (fill=255)
        # White threatened = white background (fill=255), black text (fill=0)
        if self._is_black_threatened:
            bg_color = 0  # Black background
            text_color = 255  # White text
        else:
            bg_color = 255  # White background
            text_color = 0  # Black text
        
        # Draw background
        draw.rectangle([(0, 0), (self.width - 1, self.height - 1)], fill=bg_color, outline=0)
        
        if self._alert_type == self.ALERT_CHECK:
            # Draw "CHECK" centered directly onto the sprite
            y_offset = (self.height - self._check_text.height) // 2
            self._check_text.draw_on(sprite, 0, y_offset, text_color=text_color)
            
        elif self._alert_type == self.ALERT_QUEEN:
            # Draw "YOUR\nQUEEN" centered directly onto the sprite
            self._queen_text.draw_on(sprite, 0, 0, text_color=text_color)
        
        elif self._alert_type == self.ALERT_HINT:
            # Draw hint move text centered - always white bg, black text
            draw.rectangle([(0, 0), (self.width - 1, self.height - 1)], fill=255, outline=0)
            self._hint_text.set_text(self._hint_text_value)
            y_offset = (self.height - self._hint_text.height) // 2
            self._hint_text.draw_on(sprite, 0, y_offset, text_color=0)
