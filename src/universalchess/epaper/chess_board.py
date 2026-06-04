"""
Chess board widget displaying a chess position.

Subscribes to ChessGameState and updates automatically when position changes.
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from universalchess.state.chess_game import ChessGameState

try:
    from universalchess.board.logging import log
except ImportError:
    # Fallback for direct execution
    import logging
    log = logging.getLogger(__name__)


# Module-level chess sprites, set by application at startup
_chess_sprites: Optional[Image.Image] = None


def set_chess_sprites(sprites: Image.Image) -> None:
    """Set the module-level chess sprites.
    
    Called once at application startup to provide chess piece sprites.
    
    Args:
        sprites: PIL Image containing chess piece sprite sheet (1-bit mode)
    """
    global _chess_sprites
    _chess_sprites = sprites


class ChessBoardWidget(Widget):
    """Chess board widget that renders a position from game state.
    
    Subscribes to ChessGameState and updates automatically when position changes.
    Chess sprites can be provided directly via constructor or set at module level.
    """
    
    def __init__(self, x: int, y: int, update_callback, 
                 game_state: 'ChessGameState', flip: bool,
                 sprites: Image.Image = None):
        """Initialize chess board widget.
        
        Subscribes to game_state for automatic position updates.
        
        Args:
            x: X position
            y: Y position
            update_callback: Callback to trigger display updates. Must not be None.
            game_state: ChessGameState to observe for position changes.
            flip: If True, flip board (black at bottom) (required)
            sprites: Optional chess piece sprite sheet. If None, uses module-level sprites.
        """
        super().__init__(x, y, 128, 128, update_callback)
        self._game_state = game_state
        self.fen = game_state.fen
        self.flip = flip
        self._min_square_index = 0  # Start rendering from this square
        self._max_square_index = 64  # Render up to this square
        self._render_only_file = None  # If set, only render squares in this file (0-7)
        self._render_only_rank = None  # If set, only render squares in this rank (0-7)
        
        # Use provided sprites or module-level sprites
        self._chess_font = sprites if sprites is not None else _chess_sprites
        
        if self._chess_font is None:
            log.error("[ChessBoardWidget] No chess sprites provided and none set at module level")
        else:
            self._validate_sprites()
        
        # Subscribe to position changes
        self._game_state.on_position_change(self._on_position_change)
    
    def _on_position_change(self) -> None:
        """Handle position change from game state.
        
        Called automatically when ChessGameState position changes.
        Updates FEN and triggers display refresh.
        """
        new_fen = self._game_state.fen
        if self.fen != new_fen:
            self.fen = new_fen
            self.invalidate_and_update()
    
    def cleanup(self) -> None:
        """Unsubscribe from game state when widget is destroyed."""
        if self._game_state:
            self._game_state.remove_observer(self._on_position_change)
    
    def _validate_sprites(self):
        """Validate chess sprite dimensions."""
        if self._chess_font is None:
            return
            
        width, height = self._chess_font.size
        log.debug(f"[ChessBoardWidget] Chess sprites: {width}x{height}")
        
        # Sprite sheet must be at least 32px height (2 rows) for light and dark squares
        if height < 32:
            log.error(
                f"Chesssprites image height {height}px is insufficient! "
                f"Required: 32px (2 rows of 16px each)."
            )
            self._chess_font = None
        elif height < 48:
            log.warning(
                f"Chesssprites image height {height}px is less than expected 48px. "
                f"Expected 3 rows (48px) but minimum 2 rows (32px) is acceptable."
            )
        
        if width < 208:
            log.warning(
                f"Chesssprites image width {width}px is smaller than expected "
                f"(minimum 208px for all pieces). Some pieces may be missing."
            )
    
    def _expand_fen(self, fen_board: str) -> list:
        """Expand FEN board string to 64 characters."""
        rows = fen_board.split("/")
        expanded = []
        for row in rows:
            for char in row:
                if char.isdigit():
                    expanded.extend([" "] * int(char))
                else:
                    expanded.append(char)
        if len(expanded) != 64:
            raise ValueError(f"Invalid FEN: {fen_board}")
        return expanded
    
    def _piece_x(self, piece: str) -> int:
        """Get x coordinate in sprite sheet for piece."""
        mapping = {
            "P": 16,
            "R": 32,
            "N": 48,
            "B": 64,
            "Q": 80,
            "K": 96,
            "p": 112,
            "r": 128,
            "n": 144,
            "b": 160,
            "q": 176,
            "k": 192,
        }
        return mapping.get(piece, 0)
    
    def _is_dark_square(self, index: int) -> bool:
        """Check if square at index is dark."""
        rank = index // 8
        file = index % 8
        # Invert the formula: (rank + file) % 2 == 1 for dark squares
        # This corrects the color inversion issue where squares were drawn with inverted colors
        # Standard chess: a1 (rank 0, file 0 when flipped) should be dark
        return (rank + file) % 2 == 1
    
    def _validate_crop_coords(self, x1: int, y1: int, x2: int, y2: int) -> bool:
        """Validate crop coordinates are within sprite sheet bounds."""
        if self._chess_font is None:
            log.warning("Cannot validate crop coordinates: chess font not loaded")
            return False
        
        sheet_width, sheet_height = self._chess_font.size
        
        if x1 < 0 or y1 < 0 or x2 > sheet_width or y2 > sheet_height:
            log.warning(
                f"Crop coordinates out of bounds: requested ({x1}, {y1}, {x2}, {y2}), "
                f"sprite sheet size: {sheet_width}x{sheet_height}"
            )
            return False
        
        if x1 >= x2 or y1 >= y2:
            log.warning(
                f"Invalid crop coordinates: x1={x1} >= x2={x2} or y1={y1} >= y2={y2}"
            )
            return False
        
        return True
    
    def set_fen(self, fen: str) -> None:
        """Update the FEN string."""
        if self.fen != fen:
            self.fen = fen
            self.invalidate_and_update()
    
    def set_flip(self, flip: bool) -> None:
        """Set board flip state (whether to render from black's perspective).
        
        Args:
            flip: True to flip board (black at bottom), False for white at bottom
        """
        if self.flip != flip:
            self.flip = flip
            self.invalidate_and_update()
    
    def set_max_square_index(self, max_index: int) -> None:
        """Set maximum square index to render (0-64). Used for incremental rendering."""
        max_index = max(0, min(64, max_index))
        if self._max_square_index != max_index:
            self._max_square_index = max_index
            self.invalidate_cache()  # Invalidate cache
    
    def set_square_range(self, min_index: int, max_index: int) -> None:
        """Set range of squares to render (0-64). Used for reverse order rendering."""
        min_index = max(0, min(64, min_index))
        max_index = max(0, min(64, max_index))
        if self._min_square_index != min_index or self._max_square_index != max_index:
            self._min_square_index = min_index
            self._max_square_index = max_index
            self.invalidate_cache()  # Invalidate cache
    
    def set_render_only_file(self, file: int = None) -> None:
        """Set to only render squares in a specific file (0-7, where 0=a-file). Pass None to clear filter."""
        if file is not None:
            file = max(0, min(7, file))
        if self._render_only_file != file:
            self._render_only_file = file
            self.invalidate_cache()  # Invalidate cache
    
    def set_render_only_rank(self, rank: int = None) -> None:
        """Set to only render squares in a specific rank (0-7, where 0=rank 1). Pass None to clear filter."""
        if rank is not None:
            rank = max(0, min(7, rank))
        if self._render_only_rank != rank:
            self._render_only_rank = rank
            self.invalidate_cache()  # Invalidate cache
    
    def render(self, sprite: Image.Image) -> None:
        """Render chess board onto the sprite image."""
        if self._chess_font is None:
            log.warning("Cannot render chess board: chess font not loaded")
            self.draw_background_on_sprite(sprite)
            return
        
        # Parse FEN
        try:
            fen_board = self.fen.split()[0]
            log.debug(f"Rendering chess board from FEN: {fen_board}")
        except (AttributeError, IndexError) as e:
            log.error(f"Error parsing FEN string '{self.fen}': {type(e).__name__}: {e}")
            self.draw_background_on_sprite(sprite)
            return
        
        # Expand FEN to 64 characters
        try:
            ordered = self._expand_fen(fen_board)
            log.debug(f"FEN expanded to {len(ordered)} squares")
        except ValueError as e:
            log.error(f"Invalid FEN board string '{fen_board}': {e}")
            self.draw_background_on_sprite(sprite)
            return
        except Exception as e:
            log.error(f"Unexpected error expanding FEN '{fen_board}': {type(e).__name__}: {e}")
            self.draw_background_on_sprite(sprite)
            return
        
        draw = ImageDraw.Draw(sprite)
        sheet_width, sheet_height = self._chess_font.size
        
        # Draw board outline first
        try:
            draw.rectangle([(0, 0), (127, 127)], fill=None, outline=0)
            log.debug("Drew board outline")
        except Exception as e:
            log.error(f"Error drawing board outline: {type(e).__name__}: {e}")
        
        # Render each square in the specified range
        squares_rendered = 0
        for idx, symbol in enumerate(ordered):
            # Only render squares in the range [min_square_index, max_square_index)
            if idx < self._min_square_index or idx >= self._max_square_index:
                continue
            
            rank = idx // 8
            file = idx % 8
            
            # If render_only_file is set, only render squares in that file
            if self._render_only_file is not None and file != self._render_only_file:
                continue
            
            # If render_only_rank is set, only render squares in that rank
            if self._render_only_rank is not None and rank != self._render_only_rank:
                continue
            
            squares_rendered += 1
            
            try:
                dest_rank = rank if not self.flip else 7 - rank
                dest_file = file if not self.flip else 7 - file
                
                square_index = dest_rank * 8 + dest_file
                is_dark = self._is_dark_square(square_index)
                py = 16 if is_dark else 0
                
                x = dest_file * 16
                y = dest_rank * 16
                
                # Always draw square background (empty square sprite at x=0)
                # Row 0 (y=0-16): light squares
                # Row 1 (y=16-32): dark squares
                bg_x1, bg_y1, bg_x2, bg_y2 = 0, py, 16, py + 16
                if not self._validate_crop_coords(bg_x1, bg_y1, bg_x2, bg_y2):
                    log.error(
                        f"Invalid background crop coordinates for square {idx} "
                        f"(rank={rank}, file={file}, is_dark={is_dark}): "
                        f"requested ({bg_x1}, {bg_y1}, {bg_x2}, {bg_y2}), "
                        f"sprite sheet: {sheet_width}x{sheet_height}"
                    )
                    # If sprite sheet is too small, this will keep failing - log once per square type
                    if is_dark:
                        log.error(
                            f"Dark square rendering failed - sprite sheet may only have 1 row (16px) "
                            f"instead of required 2 rows (32px). Screen may reset due to invalid operations."
                        )
                    continue
                
                try:
                    square_bg = self._chess_font.crop((bg_x1, bg_y1, bg_x2, bg_y2))
                except Exception as e:
                    log.error(
                        f"Error cropping square background at ({bg_x1}, {bg_y1}, {bg_x2}, {bg_y2}): "
                        f"{type(e).__name__}: {e}"
                    )
                    continue
                
                try:
                    sprite.paste(square_bg, (x, y))
                except Exception as e:
                    log.error(
                        f"Error pasting square background at ({x}, {y}): {type(e).__name__}: {e}"
                    )
                    continue
                
                # Draw piece if it exists
                px = self._piece_x(symbol)
                if px > 0:
                    piece_x1, piece_y1, piece_x2, piece_y2 = px, py, px + 16, py + 16
                    if not self._validate_crop_coords(piece_x1, piece_y1, piece_x2, piece_y2):
                        log.warning(
                            f"Invalid piece crop coordinates for symbol '{symbol}' at square {idx}: "
                            f"({piece_x1}, {piece_y1}, {piece_x2}, {piece_y2})"
                        )
                        continue
                    
                    try:
                        piece = self._chess_font.crop((piece_x1, piece_y1, piece_x2, piece_y2))
                    except Exception as e:
                        log.error(
                            f"Error cropping piece '{symbol}' at ({piece_x1}, {piece_y1}, {piece_x2}, {piece_y2}): "
                            f"{type(e).__name__}: {e}"
                        )
                        continue
                    
                    try:
                        sprite.paste(piece, (x, y))
                    except Exception as e:
                        log.error(
                            f"Error pasting piece '{symbol}' at ({x}, {y}): {type(e).__name__}: {e}"
                        )
                        continue
            except Exception as e:
                log.error(
                    f"Unexpected error rendering square {idx} (symbol='{symbol}'): "
                    f"{type(e).__name__}: {e}"
                )
                continue
        
        log.debug(f"ChessBoardWidget.render(): Rendered {squares_rendered} squares (rank_filter={self._render_only_rank}, file_filter={self._render_only_file}, range=[{self._min_square_index}, {self._max_square_index}))")

