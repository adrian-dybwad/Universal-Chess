"""
Chess board widget displaying a chess position.

Subscribes to ChessGameState and updates automatically when position changes.
"""

import chess
from dataclasses import dataclass
from enum import Enum
from PIL import Image, ImageDraw, ImageChops, ImageFilter
from .framework.widget import Widget, DITHER_PATTERNS
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from universalchess.state.chess_game import ChessGameState

try:
    from universalchess.board.logging import log
except ImportError:
    # Fallback for direct execution
    import logging
    log = logging.getLogger(__name__)


# Board geometry: an 8x8 grid of TILE-px cells on the 128x128 board widget.
TILE = 16

# Pieces in FEN letter order: white P R N B Q K, then black p r n b q k. A
# piece's index here maps to its sprite-sheet column (see SheetLayout).
_PIECE_ORDER = ("P", "R", "N", "B", "Q", "K", "p", "r", "n", "b", "q", "k")
_PIECE_INDEX = {piece: i for i, piece in enumerate(_PIECE_ORDER)}

# COLORWAY sheets store one column per piece *type* (colour-agnostic), in the
# order the itch.io one-bit pack ships: King, Queen, Bishop, Knight, Rook, Pawn.
# The two rows carry the colourways (see SheetLayout.COLORWAY).
_COLORWAY_TYPE_COLUMN = {"K": 0, "Q": 1, "B": 2, "N": 3, "R": 4, "P": 5}


class SheetMode(Enum):
    """How a chess sprite sheet encodes board squares and pieces."""
    LEGACY = "legacy"
    SPLIT = "split"
    COLORWAY = "colorway"


@dataclass(frozen=True)
class SheetLayout:
    """Classification of a sprite sheet, derived purely from its dimensions.

    LEGACY (e.g. 208x32): 13 columns x 2 rows. Column 0 is an empty square; the
    remaining 12 columns are pieces baked onto a square background. Row 0 is the
    light-square variant, row 1 the dark-square variant (the 50% dither is baked
    into the artwork). Pieces are pasted opaquely -- the square is part of the
    tile, so the board pattern and the pieces are inseparable.

    SPLIT (e.g. 192x32): 12 columns x 2 rows, no empty-square column. Squares are
    drawn in code (white light squares, 50% dither dark squares) and each piece
    column carries the glyph alone, not a baked square: row 0 is the INK (the
    glyph pixels to stamp in black) and row 1 is the MASK (the silhouette matte
    cleared to white under the piece so the dither does not bleed through). This
    keeps the board pattern and the pieces independent.

    COLORWAY (e.g. 96x32 RGBA): 6 columns x 2 rows, one column per piece *type*
    in the order King, Queen, Bishop, Knight, Rook, Pawn. Row 0 is the black
    colourway, row 1 the white colourway. The alpha channel is the silhouette
    mask and the opaque RGB is the ink (black/white), so a single transparent
    PNG carries both. Squares are drawn in code, like SPLIT.
    """
    mode: SheetMode
    tile: int
    columns: int
    rows: int

    @property
    def is_split(self) -> bool:
        return self.mode is SheetMode.SPLIT

    @property
    def is_colorway(self) -> bool:
        return self.mode is SheetMode.COLORWAY

    @property
    def draws_squares(self) -> bool:
        """True when the board pattern is drawn in code (SPLIT and COLORWAY)."""
        return self.mode in (SheetMode.SPLIT, SheetMode.COLORWAY)

    def piece_column_x(self, piece: str) -> int:
        """X of ``piece``'s sprite column, or -1 when ``piece`` is not a piece.

        LEGACY reserves column 0 for the empty square, so piece columns start at
        tile 1; SPLIT has no empty-square column, so they start at tile 0.
        COLORWAY has no per-colour column (use ``piece_type_column``).
        """
        idx = _PIECE_INDEX.get(piece)
        if idx is None:
            return -1
        return idx * self.tile if self.mode is SheetMode.SPLIT else (idx + 1) * self.tile

    def piece_type_column(self, piece: str) -> int:
        """Column x of ``piece``'s type in a COLORWAY sheet, or -1 if not a piece.

        Colour-agnostic: white and black share a type column; the colourway is
        selected by row, not column.
        """
        idx = _COLORWAY_TYPE_COLUMN.get(piece.upper())
        return idx * self.tile if idx is not None else -1


def image_has_alpha(img: Image.Image) -> bool:
    """Whether ``img`` carries per-pixel transparency (the COLORWAY mask source)."""
    return img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)


def detect_sheet_layout(width: int, height: int, *, has_alpha: bool = False,
                        tile: int = TILE) -> SheetLayout:
    """Classify a sprite sheet from its pixel dimensions (the drawing-path trigger).

    - A COLORWAY sheet has 6 type columns over >=2 rows *and* an alpha channel
      (the mask). Alpha is required: without it there is no silhouette to matte.
    - A SPLIT sheet has 12 piece columns (no empty-square column) over >=2 rows.
    - Everything else -- including the historical 13-column baked sheets -- is
      LEGACY so existing sheets render unchanged.

    Detection is by dimensions (plus the presence of alpha), so a sheet's
    structure is implied by the file itself with no side-car metadata to sync.
    """
    columns = width // tile if tile else 0
    rows = height // tile if tile else 0
    if has_alpha and columns == 6 and rows >= 2:
        return SheetLayout(SheetMode.COLORWAY, tile, columns, rows)
    if columns == 12 and rows >= 2:
        return SheetLayout(SheetMode.SPLIT, tile, columns, rows)
    return SheetLayout(SheetMode.LEGACY, tile, columns, rows)


def fill_dither_dark(sprite: Image.Image, x: int, y: int, tile: int = TILE) -> None:
    """Fill the cell at (x, y) with a 50% Bayer dither (dark square in SPLIT mode).

    The dither phase uses the sprite's global pixel coordinates so adjacent dark
    squares tile seamlessly, matching the ordered dither the UI backgrounds use
    (shade 8 ~= 50%). Matches the look the legacy sheets baked into their row 1.
    """
    pattern = DITHER_PATTERNS[8]
    pixels = sprite.load()
    for yy in range(y, y + tile):
        pattern_row = pattern[yy % 8]
        for xx in range(x, x + tile):
            pixels[xx, yy] = 0 if pattern_row[xx % 8] else 255


def composite_split_piece(sprite: Image.Image, x: int, y: int, tile: int,
                          sheet: Image.Image, layout: SheetLayout, piece: str) -> bool:
    """Composite a SPLIT piece onto ``sprite`` at (x, y), over an already-drawn square.

    Two steps establish the ink + silhouette-mask contract: (1) clear the square
    under the piece silhouette to white using the MASK row, so the dither does
    not show through the piece body; (2) stamp the INK glyph from row 0 in black.
    White pieces read as a white body with a black outline; black pieces read as
    a black body with a white halo -- both crisp on white and dithered squares.

    Returns True when a piece was drawn (False for an empty/unknown square or a
    sheet too small for the piece's tiles).
    """
    px = layout.piece_column_x(piece)
    if px < 0:
        return False
    if px + tile > sheet.width or 2 * tile > sheet.height:
        return False
    ink = sheet.crop((px, 0, px + tile, tile)).convert("1")
    mask = sheet.crop((px, tile, px + tile, 2 * tile)).convert("1")
    box = (x, y, x + tile, y + tile)
    # White matte: invert() turns the black silhouette into 255 (apply white),
    # leaving the background 0 (keep the square's dither/white).
    sprite.paste(255, box, ImageChops.invert(mask))
    # Ink: paste black wherever the ink glyph is black.
    sprite.paste(0, box, ImageChops.invert(ink))
    return True


def composite_colorway_piece(sprite: Image.Image, x: int, y: int, tile: int,
                             sheet: Image.Image, layout: SheetLayout, piece: str) -> bool:
    """Composite a COLORWAY piece onto ``sprite`` at (x, y), over a drawn square.

    The RGBA sheet supplies both halves of the ink + silhouette-mask contract:
    the alpha channel is the silhouette (mask) and the opaque RGB is the ink.
    The colourway is picked by row (black pieces in row 0, white in row 1) and
    the column by piece type. Two steps: (1) matte the square to white under the
    silhouette, dilated 1px so a black piece keeps a thin white halo on a
    dithered square; (2) stamp black ink where the piece is opaque and dark
    (the outline for white pieces, the body for black pieces).

    Returns True when a piece was drawn (False for an empty/unknown square or a
    sheet too small for the piece's tiles).
    """
    sx = layout.piece_type_column(piece)
    if sx < 0:
        return False
    row_y = 0 if piece.islower() else tile  # row 0 = black colourway, row 1 = white
    if sx + tile > sheet.width or row_y + tile > sheet.height:
        return False

    cell = sheet.crop((sx, row_y, sx + tile, row_y + tile))
    if cell.mode != "RGBA":
        cell = cell.convert("RGBA")
    alpha = cell.getchannel("A")
    luminance = cell.convert("L")
    box = (x, y, x + tile, y + tile)

    # Matte: white over the silhouette, dilated 1px for a halo. MaxFilter grows
    # the opaque region so a black body reads separate from a dithered square.
    opaque = alpha.point(lambda a: 255 if a >= 128 else 0)
    matte = opaque.filter(ImageFilter.MaxFilter(3)).convert("1")
    sprite.paste(255, box, matte)

    # Ink: black where the piece is opaque and dark. multiply() keeps only the
    # pixels that are both inside the silhouette and dark in the source art.
    dark = luminance.point(lambda v: 255 if v < 128 else 0)
    ink = ImageChops.multiply(opaque, dark).convert("1")
    sprite.paste(0, box, ink)
    return True


def composite_piece(sprite: Image.Image, x: int, y: int, tile: int,
                    sheet: Image.Image, layout: SheetLayout, piece: str) -> bool:
    """Composite ``piece`` onto an already-drawn square, dispatching by layout.

    Only the code-drawn layouts (SPLIT, COLORWAY) composite here; LEGACY bakes
    the piece into the square tile and blits it elsewhere.
    """
    if layout.mode is SheetMode.COLORWAY:
        return composite_colorway_piece(sprite, x, y, tile, sheet, layout, piece)
    if layout.mode is SheetMode.SPLIT:
        return composite_split_piece(sprite, x, y, tile, sheet, layout, piece)
    return False


def piece_silhouette(sheet: Image.Image, layout: SheetLayout, piece: str) -> Optional[Image.Image]:
    """A mode-'1' glyph where black (0) marks the piece area, for the red overlay.

    The silhouette source depends on layout: COLORWAY uses the alpha channel,
    SPLIT the MASK row, LEGACY the light-square row (a clean white background
    whose black pixels are the piece shape). Returns None for a non-piece or a
    sheet too small for the piece's tiles.
    """
    tile = layout.tile
    if layout.mode is SheetMode.COLORWAY:
        sx = layout.piece_type_column(piece)
        if sx < 0:
            return None
        row_y = 0 if piece.islower() else tile
        if sx + tile > sheet.width or row_y + tile > sheet.height:
            return None
        cell = sheet.crop((sx, row_y, sx + tile, row_y + tile))
        if cell.mode != "RGBA":
            cell = cell.convert("RGBA")
        # Occupied (opaque) -> black (0) so the shared invert()+paste reddens it.
        return cell.getchannel("A").point(lambda a: 0 if a >= 128 else 255, mode="1")

    px = layout.piece_column_x(piece)
    if px < 0:
        return None
    sy1 = tile if layout.is_split else 0
    sy2 = sy1 + tile
    if px + tile > sheet.width or sy2 > sheet.height:
        return None
    return sheet.crop((px, sy1, px + tile, sy2)).convert("1")


def compose_preview_strip(sheet: Image.Image, layout: SheetLayout) -> Image.Image:
    """A 12x2 preview for code-drawn sheets: each FEN piece on light/dark squares.

    Row 0 shows every piece on a white light square, row 1 on a 50% dither dark
    square, so the web/menu selector shows exactly what the board composites.
    """
    tile = layout.tile
    strip = Image.new("1", (len(_PIECE_ORDER) * tile, 2 * tile), 255)
    for column, piece in enumerate(_PIECE_ORDER):
        px = column * tile
        composite_piece(strip, px, 0, tile, sheet, layout, piece)
        fill_dither_dark(strip, px, tile, tile)
        composite_piece(strip, px, tile, tile, sheet, layout, piece)
    return strip


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
        # Layout (LEGACY vs SPLIT) is derived from the sheet dimensions in
        # _validate_sprites; None until a valid sheet is present.
        self._layout: Optional[SheetLayout] = None

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
        """Validate chess sprite dimensions and classify the sheet layout."""
        self._layout = None
        if self._chess_font is None:
            return

        width, height = self._chess_font.size
        self._layout = detect_sheet_layout(width, height,
                                           has_alpha=image_has_alpha(self._chess_font))
        log.debug(f"[ChessBoardWidget] Chess sprites: {width}x{height} ({self._layout.mode.value})")

        # Every layout needs two rows of 16px (LEGACY: light/dark square variants;
        # SPLIT: ink over mask; COLORWAY: black/white colourways). Without them
        # the crops below would be invalid.
        if height < 2 * TILE:
            log.error(
                f"Chesssprites image height {height}px is insufficient! "
                f"Required: {2 * TILE}px (2 rows of {TILE}px each)."
            )
            self._chess_font = None
            self._layout = None
            return

        # LEGACY needs its 13th (piece) column; SPLIT packs 12 piece columns;
        # COLORWAY packs 6 type columns. Each draws/holds squares accordingly.
        min_width = {
            SheetMode.LEGACY: 208,
            SheetMode.SPLIT: 12 * TILE,
            SheetMode.COLORWAY: 6 * TILE,
        }[self._layout.mode]
        if width < min_width:
            log.warning(
                f"Chesssprites image width {width}px is smaller than expected "
                f"(minimum {min_width}px for all pieces in {self._layout.mode.value} "
                f"layout). Some pieces may be missing."
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
    
    def _render_square_legacy(self, sprite: Image.Image, x: int, y: int, is_dark: bool,
                              symbol: str, idx: int, rank: int, file: int,
                              sheet_width: int, sheet_height: int) -> None:
        """Render one cell from a LEGACY sheet: paste the baked square, then the piece.

        Row 0 (y=0..15) holds light-square tiles, row 1 (y=16..31) dark-square
        tiles (dither baked in); column 0 is the empty square. The piece tile is
        pasted opaquely on top -- square and piece are inseparable in this sheet.
        """
        py = TILE if is_dark else 0

        # Empty-square background sprite lives in column 0 of the matching row.
        bg_x1, bg_y1, bg_x2, bg_y2 = 0, py, TILE, py + TILE
        if not self._validate_crop_coords(bg_x1, bg_y1, bg_x2, bg_y2):
            log.error(
                f"Invalid background crop coordinates for square {idx} "
                f"(rank={rank}, file={file}, is_dark={is_dark}): "
                f"requested ({bg_x1}, {bg_y1}, {bg_x2}, {bg_y2}), "
                f"sprite sheet: {sheet_width}x{sheet_height}"
            )
            if is_dark:
                log.error(
                    "Dark square rendering failed - sprite sheet may only have 1 row (16px) "
                    "instead of required 2 rows (32px). Screen may reset due to invalid operations."
                )
            return

        square_bg = self._chess_font.crop((bg_x1, bg_y1, bg_x2, bg_y2))
        sprite.paste(square_bg, (x, y))

        # Draw the piece, if any (column 0 is the empty square, so px > 0 == piece).
        px = self._layout.piece_column_x(symbol)
        if px > 0:
            piece_x1, piece_y1, piece_x2, piece_y2 = px, py, px + TILE, py + TILE
            if not self._validate_crop_coords(piece_x1, piece_y1, piece_x2, piece_y2):
                log.warning(
                    f"Invalid piece crop coordinates for symbol '{symbol}' at square {idx}: "
                    f"({piece_x1}, {piece_y1}, {piece_x2}, {piece_y2})"
                )
                return
            piece = self._chess_font.crop((piece_x1, piece_y1, piece_x2, piece_y2))
            sprite.paste(piece, (x, y))

    def _render_square_composited(self, sprite: Image.Image, draw: ImageDraw.ImageDraw,
                                  x: int, y: int, is_dark: bool, symbol: str) -> None:
        """Render one cell from a code-drawn sheet (SPLIT/COLORWAY): square + piece.

        Light squares are filled white and dark squares get a 50% Bayer dither;
        the piece (if any) is composited via the layout's ink + silhouette-mask
        contract so it reads cleanly over either background.
        """
        if is_dark:
            fill_dither_dark(sprite, x, y, TILE)
        else:
            draw.rectangle([x, y, x + TILE - 1, y + TILE - 1], fill=255)

        if symbol in _PIECE_INDEX:
            composite_piece(sprite, x, y, TILE, self._chess_font, self._layout, symbol)

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

                x = dest_file * TILE
                y = dest_rank * TILE

                if self._layout is not None and self._layout.draws_squares:
                    self._render_square_composited(sprite, draw, x, y, is_dark, symbol)
                else:
                    self._render_square_legacy(sprite, x, y, is_dark, symbol, idx,
                                               rank, file, sheet_width, sheet_height)
            except Exception as e:
                log.error(
                    f"Unexpected error rendering square {idx} (symbol='{symbol}'): "
                    f"{type(e).__name__}: {e}"
                )
                continue
        
        log.debug(f"ChessBoardWidget.render(): Rendered {squares_rendered} squares (rank_filter={self._render_only_rank}, file_filter={self._render_only_file}, range=[{self._min_square_index}, {self._max_square_index}))")

    # -------------------------------------------------------------------------
    # Three-color (red) highlighting
    # -------------------------------------------------------------------------

    def _square_to_fen_index(self, square: int) -> int:
        """python-chess square (a1=0) -> this widget's FEN expansion index (a8=0).

        The FEN expansion lists rank 8 first; python-chess numbers rank 1 first,
        so the rank is mirrored while the file is preserved.
        """
        return (7 - (square // 8)) * 8 + (square % 8)

    def _square_to_cell(self, square: int) -> tuple:
        """python-chess square -> top-left (x, y) of its 16x16 cell on the board.

        Uses the exact same file/rank-to-pixel mapping as render() (including the
        ``flip`` transform) so a highlighted square lines up pixel-for-pixel with
        the piece drawn there. A drift between this and render() would paint the
        red highlight on the wrong square.
        """
        file = square % 8
        fen_rank = 7 - (square // 8)
        dest_rank = fen_rank if not self.flip else 7 - fen_rank
        dest_file = file if not self.flip else 7 - file
        return dest_file * 16, dest_rank * 16

    def _compute_red_squares(self) -> set:
        """Squares to highlight red for the position currently being drawn.

        Derived from ``self.fen`` (the exact rendered position) so the highlight
        cannot disagree with the board image due to state-mutation timing. Mirrors
        ChessGameState.get_queen_threat_info / get_check_info -- check outranks
        queen-threat, one alert at a time, and both flag the SIDE-TO-MOVE's own
        royalty (not the opponent's):
          - in check: the side-to-move's king and every checking piece;
          - else if the side-to-move's own queen is attacked by the opponent:
            that queen and every attacker of it.
        Returns an empty set for a quiet position (no red).
        """
        try:
            board = chess.Board(self.fen)
        except (ValueError, AttributeError):
            return set()

        squares = set()
        if board.is_check():
            king_square = board.king(board.turn)
            if king_square is not None:
                squares.add(king_square)
            squares.update(board.checkers())
            return squares

        side_to_move = board.turn
        queens = board.pieces(chess.QUEEN, side_to_move)
        if queens:
            queen_square = next(iter(queens))
            attackers = board.attackers(not side_to_move, queen_square)
            if attackers:
                squares.add(queen_square)
                squares.update(attackers)
        return squares

    def render_red(self, sprite: Image.Image) -> None:
        """Render the RED overlay: outline highlighted squares and redden pieces.

        For each square returned by _compute_red_squares draws a red 16x16 cell
        outline (the "square" highlight) and reddens the piece glyph on it. The
        piece silhouette comes from the sheet's silhouette source -- the alpha
        channel for COLORWAY sheets, the MASK row for SPLIT, the clean light-
        square row for LEGACY -- so the king/queen/attacker renders red while
        non-piece areas of the cell stay as the B/W plane drew them.
        """
        if self._chess_font is None:
            return

        squares = self._compute_red_squares()
        if not squares:
            return

        try:
            ordered = self._expand_fen(self.fen.split()[0])
        except (ValueError, AttributeError, IndexError):
            ordered = None

        draw = ImageDraw.Draw(sprite)
        for square in squares:
            x, y = self._square_to_cell(square)
            # Square highlight: red cell outline.
            draw.rectangle([(x, y), (x + 15, y + 15)], outline=0)

            if ordered is None or self._layout is None:
                continue
            symbol = ordered[self._square_to_fen_index(square)]
            # Silhouette source is layout-aware (COLORWAY alpha, SPLIT mask row,
            # LEGACY light-square row); a mode-'1' glyph with black == piece.
            glyph = piece_silhouette(self._chess_font, self._layout, symbol)
            if glyph is None:
                continue
            # Paste red (0) where the glyph is black (mask = inverted glyph). A
            # 4-tuple box sizes the region from the box, not from the mask, which
            # is robust to PIL-mock pollution from other test modules.
            sprite.paste(0, (x, y, x + TILE, y + TILE), ImageChops.invert(glyph))

