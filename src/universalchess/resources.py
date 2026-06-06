"""Resource loader for DGTCentaurMods.

Loads and caches resources (fonts, images, sprites) from the resources directory.
Resources are loaded once at application startup and passed to components that need them.

Usage:
    from universalchess.resources import ResourceLoader
    
    # Create loader with resource directories
    loader = ResourceLoader("/opt/universalchess/resources", "/home/pi/resources")
    
    # Load resources
    font = loader.get_font(18)
    sprites = loader.get_chess_sprites()
    logo, mask = loader.get_knight_logo(100)
    
    # Pass to widgets
    widget = SomeWidget(font=font, sprites=sprites)
"""

from PIL import Image, ImageFont
from typing import Dict, Optional, Tuple
import os


class ResourceLoader:
    """Loads and caches resources from the filesystem.
    
    Resources are loaded lazily on first access and cached for reuse.
    Checks user directory first (for overrides), then system directory.
    """
    
    def __init__(self, system_dir: str, user_dir: str = None):
        """Initialize resource loader with resource directories.
        
        Args:
            system_dir: Path to system resources directory (e.g., /opt/universalchess/resources)
            user_dir: Optional path to user resources directory (checked first, for overrides)
        """
        self.system_dir = system_dir
        self.user_dir = user_dir
        
        # Font cache: {(path, size): ImageFont}
        self._font_cache: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
        
        # Image cache: {name: Image}
        self._image_cache: Dict[str, Image.Image] = {}
        
        # Resized image cache: {(name, width, height): Image}
        self._resized_cache: Dict[Tuple[str, int, int], Image.Image] = {}
        
        # Default font path (resolved on first use)
        self._default_font_path: Optional[str] = None
    
    def get_resource_path(self, filename: str) -> Optional[str]:
        """Get full path to a resource file.
        
        Checks user directory first (for overrides), then system directory.
        
        Args:
            filename: Name of the resource file
            
        Returns:
            Full path to the file, or None if not found
        """
        if ".." in filename:
            return None
        
        # Check user directory first for overrides
        if self.user_dir:
            user_path = os.path.join(self.user_dir, filename)
            if os.path.exists(user_path):
                return user_path
        
        # Fall back to system directory
        system_path = os.path.join(self.system_dir, filename)
        if os.path.exists(system_path):
            return system_path
        
        return None
    
    def get_font(self, size: int, path: str = None) -> ImageFont.FreeTypeFont:
        """Get a font at the specified size.
        
        Uses caching to avoid loading the same font multiple times.
        
        Args:
            size: Font size in points
            path: Optional path to font file. If None, uses default Font.ttc
            
        Returns:
            PIL ImageFont object
        """
        # Resolve font path
        if path is None:
            if self._default_font_path is None:
                self._default_font_path = self.get_resource_path("Font.ttc")
            path = self._default_font_path
        
        # Check cache
        cache_key = (path, size)
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]
        
        # Load font
        font = None
        if path and os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
            except Exception:
                pass
        
        if font is None:
            font = ImageFont.load_default()
        
        # Cache and return
        self._font_cache[cache_key] = font
        return font
    
    def get_image(self, name: str) -> Optional[Image.Image]:
        """Get an image resource by name.
        
        Images are cached after first load.
        
        Args:
            name: Filename of the image (e.g., "knight_logo.bmp")
            
        Returns:
            PIL Image object, or None if not found
        """
        if name in self._image_cache:
            return self._image_cache[name]
        
        path = self.get_resource_path(name)
        if not path:
            return None
        
        try:
            img = Image.open(path)
            # Load the image data into memory (detach from file)
            img.load()
            self._image_cache[name] = img
            return img
        except Exception:
            return None
    
    # Prefix and suffix of selectable chess sprite-sheet resources. A sheet named
    # ``chesssprites_<id>.bmp`` is exposed in the display menu under ``<id>``.
    _SPRITE_SHEET_PREFIX = "chesssprites_"
    _SPRITE_SHEET_SUFFIX = ".bmp"
    DEFAULT_SPRITE_SHEET = "default"

    def list_chess_sprite_sheets(self) -> "list[str]":
        """List the identifiers of all available chess sprite sheets.

        Scans the user and system resource directories for files named
        ``chesssprites_<id>.bmp`` and returns each ``<id>`` once (user overrides
        merge with system sheets). ``default`` is always listed first so the menu
        cycles from a known starting point; the remaining ids are alphabetical.

        Returns:
            Ordered list of sheet identifiers, or empty if none are present.
        """
        ids = set()
        for directory in (self.user_dir, self.system_dir):
            if not directory:
                continue
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if name.startswith(self._SPRITE_SHEET_PREFIX) and name.endswith(self._SPRITE_SHEET_SUFFIX):
                    identifier = name[len(self._SPRITE_SHEET_PREFIX):-len(self._SPRITE_SHEET_SUFFIX)]
                    if identifier:
                        ids.add(identifier)

        ordered = sorted(ids)
        if self.DEFAULT_SPRITE_SHEET in ids:
            ordered.remove(self.DEFAULT_SPRITE_SHEET)
            ordered.insert(0, self.DEFAULT_SPRITE_SHEET)
        return ordered

    def get_chess_sprites(self, name: str = DEFAULT_SPRITE_SHEET) -> Optional[Image.Image]:
        """Get a chess piece sprite sheet by identifier.

        Loads ``chesssprites_<name>.bmp`` and converts it to 1-bit mode for the
        e-paper display. Results are cached per-name so switching sheets does not
        return a stale image.

        Args:
            name: Sheet identifier (e.g. ``"default"``, ``"fen"``). Defaults to
                the built-in ``default`` sheet.

        Returns:
            PIL Image in mode '1', or None if the sheet is not found.
        """
        filename = f"{self._SPRITE_SHEET_PREFIX}{name}{self._SPRITE_SHEET_SUFFIX}"
        cache_key = f"{filename}:1bit"
        if cache_key in self._image_cache:
            return self._image_cache[cache_key]

        img = self.get_image(filename)
        if img is None:
            return None

        # Convert to 1-bit mode
        if img.mode != "1":
            if img.mode != "L":
                img = img.convert("L")
            img = img.point(lambda x: 0 if x < 128 else 255, mode="1")

        self._image_cache[cache_key] = img
        return img
    
    # Column x-position of each piece in the 16px sprite sheet. Row 0 (y=0..15)
    # renders the piece on a light-square background, which composites cleanly
    # onto the white menu. Matches ChessBoardWidget._piece_x.
    _PIECE_SHEET_X = {
        "P": 16, "R": 32, "N": 48, "B": 64, "Q": 80, "K": 96,
        "p": 112, "r": 128, "n": 144, "b": 160, "q": 176, "k": 192,
    }
    _PIECE_CELL = 16

    def get_chess_piece_preview(
        self,
        sheet_name: str,
        piece: str = "k",
        size: Optional[int] = None,
        on_dark_square: bool = True,
    ) -> Tuple[Optional[Image.Image], Optional[Image.Image]]:
        """Get a single chess piece preview tile cropped from a sprite sheet.

        Crops ``piece`` from ``chesssprites_<sheet_name>.bmp`` and returns the
        whole 16px cell as an opaque tile (the square plus the piece). The sheet
        has light-square pieces in row 0 (y=0) and dark-square pieces in row 1
        (y=16); ``on_dark_square`` selects the row, defaulting to the dark square
        so the black king is shown on its black square.

        Args:
            sheet_name: Sprite sheet identifier (e.g. ``"default"``).
            piece: Piece letter using FEN case (uppercase = white, lowercase =
                black). Defaults to the black king ``"k"``.
            size: Optional square size to scale to, using nearest-neighbour to
                preserve the pixel art. ``None`` keeps the native 16px cell.
            on_dark_square: Crop the dark-square row (row 1) when True, else the
                light-square row (row 0).

        Returns:
            ``(image, None)`` in mode '1' - the full opaque tile and no mask
            (the whole square is shown). ``(None, None)`` if the piece letter is
            unknown or the sheet/row is unavailable.
        """
        if piece not in self._PIECE_SHEET_X:
            return None, None

        sheet = self.get_chess_sprites(sheet_name)
        if sheet is None:
            return None, None

        x = self._PIECE_SHEET_X[piece]
        cell = self._PIECE_CELL
        py = cell if on_dark_square else 0
        if x + cell > sheet.width or py + cell > sheet.height:
            return None, None

        piece_img = sheet.crop((x, py, x + cell, py + cell))
        if size is not None and size != cell:
            piece_img = piece_img.resize((size, size), Image.NEAREST)
        if piece_img.mode != "1":
            piece_img = piece_img.convert("1")

        # Full square tile: shown opaquely, so no transparency mask.
        return piece_img, None

    def get_knight_logo(self, size: int = 100) -> Tuple[Optional[Image.Image], Optional[Image.Image]]:
        """Get knight logo image and its transparency mask.
        
        Args:
            size: Target size (width and height) for the logo
            
        Returns:
            Tuple of (logo_image, mask_image), or (None, None) if not found.
            The mask has 255 where the knight is (black pixels) and 0 elsewhere.
        """
        cache_key = ("knight_logo.bmp", size, size)
        mask_cache_key = ("knight_logo_mask.bmp", size, size)
        
        if cache_key in self._resized_cache and mask_cache_key in self._resized_cache:
            return self._resized_cache[cache_key], self._resized_cache[mask_cache_key]
        
        img = self.get_image("knight_logo.bmp")
        if img is None:
            return None, None
        
        # Resize if needed
        if img.size[0] != size or img.size[1] != size:
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS
            img = img.resize((size, size), resample)
        
        # Ensure 1-bit mode
        if img.mode != '1':
            img = img.convert('1')
        
        # Create mask where black pixels (knight) are opaque
        mask = Image.new("1", img.size, 0)
        img_pixels = img.load()
        mask_pixels = mask.load()
        for y in range(img.height):
            for x in range(img.width):
                if img_pixels[x, y] == 0:  # Black pixel
                    mask_pixels[x, y] = 255  # Opaque
        
        # Cache both
        self._resized_cache[cache_key] = img
        self._resized_cache[mask_cache_key] = mask
        
        return img, mask


# ---------------------------------------------------------------------------
# Module-level singleton
#
# The application builds one ResourceLoader at startup. Exposing it here lets
# components that run after startup (the display menu's sprite selector, the
# DisplayManager's hot-reload of the selected sheet) reuse that same loader -
# and its caches - instead of re-instantiating it.
# ---------------------------------------------------------------------------

_resource_loader: Optional[ResourceLoader] = None


def set_resource_loader(loader: ResourceLoader) -> None:
    """Register the application-wide ResourceLoader singleton."""
    global _resource_loader
    _resource_loader = loader


def get_resource_loader() -> Optional[ResourceLoader]:
    """Get the application-wide ResourceLoader, or None if not yet set."""
    return _resource_loader
