"""Resource loader for DGTCentaurMods.

Loads and caches resources (fonts, images, sprites) from the resources directory.
Resources are loaded once at application startup and passed to components that need them.

Usage:
    from universalchess.resources import ResourceLoader
    
    # Create loader with resource directories
    loader = ResourceLoader("/opt/universalchess/resources", "~/resources")
    
    # Load resources
    font = loader.get_font(18)
    sprites = loader.get_chess_sprites()
    logo, mask = loader.get_knight_logo(100)
    
    # Pass to widgets
    widget = SomeWidget(font=font, sprites=sprites)
"""

from PIL import Image, ImageFont
from typing import Dict, Optional, Tuple
import logging
import os

from universalchess.utils.safe_path import safe_under_base

log = logging.getLogger(__name__)


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
        if not filename:
            return None

        # Check user directory first for overrides. safe_under_base contains the
        # (potentially untrusted) filename within each resource directory,
        # guarding against path traversal (CWE-22).
        if self.user_dir:
            user_path = safe_under_base(self.user_dir, filename)
            if user_path is not None and os.path.exists(user_path):
                return user_path

        # Fall back to system directory
        system_path = safe_under_base(self.system_dir, filename)
        if system_path is not None and os.path.exists(system_path):
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
            except Exception as e:
                # The file exists but failed to load (corrupt/unsupported format):
                # fall through to the default font below. Log it - silently
                # dropping to the bitmap default makes a broken bundled font hard
                # to diagnose. Broad except is intentional: get_font must always
                # return a usable font, so no load error may escape here.
                log.warning("Failed to load font '%s' at size %d: %s", path, size, e)
        
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
    
    # Prefix and suffixes of selectable chess sprite-sheet resources. A sheet
    # named ``chesssprites_<id>.<ext>`` is exposed in the display menu under
    # ``<id>``. Both BMP (opaque 1-bit sheets) and PNG (which may carry an alpha
    # mask for the COLORWAY layout) are recognised; ``.bmp`` takes precedence
    # when an id exists as both, so shipped defaults win over a stray PNG.
    _SPRITE_SHEET_PREFIX = "chesssprites_"
    _SPRITE_SHEET_SUFFIX = ".bmp"
    _SPRITE_SHEET_SUFFIXES = (".bmp", ".png")
    # The ``default`` id is the sentinel default sheet: listed first, used as the
    # fresh-install/fallback selection, and the value ``chess_sprites`` defaults
    # to. It ships as ``chesssprites_default.png`` (the Cburnett COLORWAY set).
    # Keeping the id stable means a persisted ``chess_sprites = default`` silently
    # resolves to whatever art the default file currently holds -- so the default
    # style can be swapped (the previous Mods set now ships as ``original_mods``)
    # without a settings migration or a broken selection.
    DEFAULT_SPRITE_SHEET = "default"

    @staticmethod
    def sprite_sheet_label(sheet_id: str) -> str:
        """Human-readable label for a sprite-sheet id (``original_mods`` -> "Original Mods").

        Sheet ids come from filenames (``chesssprites_<id>``), so they are
        lower_snake_case. The e-paper Sprites list would otherwise show the raw id
        (``original_mods``); title-casing each ``_``-separated word matches the web
        selector's humanising, so both surfaces label a sheet identically.
        """
        return " ".join(word.capitalize() for word in sheet_id.split("_") if word)

    def list_chess_sprite_sheets(self) -> "list[str]":
        """List the identifiers of all available chess sprite sheets.

        Scans the user and system resource directories for files named
        ``chesssprites_<id>.bmp`` or ``chesssprites_<id>.png`` and returns each
        ``<id>`` once (user overrides and .bmp/.png variants merge to a single
        id). ``default`` is always listed first so the menu cycles from a known
        starting point; the remaining ids are alphabetical.

        Returns:
            Ordered list of sheet identifiers, or empty if none are present.
        """
        ids = set()
        for directory in (self.user_dir, self.system_dir):
            if not directory:
                continue
            try:
                names = os.listdir(directory)
            except OSError as e:
                # One unreadable resource dir must not abort discovery of the
                # others; skip it but record which, so a misconfigured path is
                # visible rather than silently yielding fewer sheets.
                log.debug("Skipping unreadable resource directory '%s': %s", directory, e)
                continue
            for name in names:
                if not name.startswith(self._SPRITE_SHEET_PREFIX):
                    continue
                for suffix in self._SPRITE_SHEET_SUFFIXES:
                    if name.endswith(suffix):
                        identifier = name[len(self._SPRITE_SHEET_PREFIX):-len(suffix)]
                        if identifier:
                            ids.add(identifier)
                        break

        ordered = sorted(ids)
        if self.DEFAULT_SPRITE_SHEET in ids:
            ordered.remove(self.DEFAULT_SPRITE_SHEET)
            ordered.insert(0, self.DEFAULT_SPRITE_SHEET)
        return ordered

    def get_chess_sprites(self, name: str = DEFAULT_SPRITE_SHEET) -> Optional[Image.Image]:
        """Get a chess piece sprite sheet by identifier.

        Resolves ``chesssprites_<name>.<ext>`` (``.bmp`` preferred over ``.png``)
        and returns it ready for the renderer. COLORWAY sheets -- a transparent
        6-column RGBA PNG whose alpha is the piece mask -- are kept in RGBA so the
        mask survives; every other sheet is converted to 1-bit for the e-paper
        display. Results are cached per resolved file so switching sheets does not
        return a stale image.

        Args:
            name: Sheet identifier (e.g. ``"default"``, ``"onebit"``,
                ``"original_mods"``). Defaults to the built-in ``default`` sheet
                (``chesssprites_default.png``, the Cburnett set).

        Returns:
            PIL Image in mode '1' (LEGACY/SPLIT) or 'RGBA' (COLORWAY), or None if
            the sheet is not found.
        """
        from universalchess.epaper.chess_board import detect_sheet_layout, image_has_alpha, SheetMode

        img = None
        filename = None
        for suffix in self._SPRITE_SHEET_SUFFIXES:
            candidate = f"{self._SPRITE_SHEET_PREFIX}{name}{suffix}"
            loaded = self.get_image(candidate)
            if loaded is not None:
                img, filename = loaded, candidate
                break
        if img is None:
            return None

        layout = detect_sheet_layout(img.width, img.height, has_alpha=image_has_alpha(img))
        cache_key = f"{filename}:{layout.mode.value}"
        if cache_key in self._image_cache:
            return self._image_cache[cache_key]

        if layout.mode is SheetMode.COLORWAY:
            # Keep the alpha mask and RGB ink; the board composites from both.
            result = img if img.mode == "RGBA" else img.convert("RGBA")
        elif img.mode != "1":
            # Opaque sheets render as 1-bit on the e-paper panel.
            gray = img if img.mode == "L" else img.convert("L")
            result = gray.point(lambda x: 0 if x < 128 else 255, mode="1")
        else:
            result = img

        self._image_cache[cache_key] = result
        return result
    
    def get_chess_piece_preview(
        self,
        sheet_name: str,
        piece: str = "k",
        size: Optional[int] = None,
        on_dark_square: bool = True,
    ) -> Tuple[Optional[Image.Image], Optional[Image.Image]]:
        """Get a single chess piece preview tile composed from a sprite sheet.

        Returns the whole 16px cell (square plus piece) as an opaque tile. The
        composition depends on the sheet's layout (detected from its dimensions,
        the same trigger the board uses):

        - LEGACY sheets bake the piece onto the square, with light-square pieces
          in row 0 (y=0) and dark-square pieces in row 1 (y=16). The row is
          cropped directly; ``on_dark_square`` selects it.
        - SPLIT and COLORWAY sheets store only the piece (ink + silhouette mask,
          or RGBA ink + alpha mask), so the square is drawn in code (white light
          square or 50% dither dark square) and the piece composited on top.

        Args:
            sheet_name: Sprite sheet identifier (e.g. ``"default"``).
            piece: Piece letter using FEN case (uppercase = white, lowercase =
                black). Defaults to the black king ``"k"``.
            size: Optional square size to scale to, using nearest-neighbour to
                preserve the pixel art. ``None`` keeps the native 16px cell.
            on_dark_square: Show the piece on a dark square when True, else a
                light square.

        Returns:
            ``(image, None)`` in mode '1' - the full opaque tile and no mask
            (the whole square is shown). ``(None, None)`` if the piece letter is
            unknown or the sheet is unavailable.
        """
        from universalchess.epaper.chess_board import (
            TILE, _PIECE_INDEX, detect_sheet_layout, image_has_alpha,
            fill_dither_dark, composite_piece,
        )

        if piece not in _PIECE_INDEX:
            return None, None

        sheet = self.get_chess_sprites(sheet_name)
        if sheet is None:
            return None, None

        layout = detect_sheet_layout(sheet.width, sheet.height,
                                     has_alpha=image_has_alpha(sheet))

        if layout.draws_squares:
            # No baked square: draw the square, then composite the piece
            # (SPLIT ink/mask or COLORWAY alpha/ink).
            piece_img = Image.new("1", (TILE, TILE), 255)  # 255 == white
            if on_dark_square:
                fill_dither_dark(piece_img, 0, 0, TILE)
            if not composite_piece(piece_img, 0, 0, TILE, sheet, layout, piece):
                return None, None
        else:
            x = layout.piece_column_x(piece)
            py = TILE if on_dark_square else 0
            if x < 0 or x + TILE > sheet.width or py + TILE > sheet.height:
                return None, None
            piece_img = sheet.crop((x, py, x + TILE, py + TILE))

        if size is not None and size != TILE:
            piece_img = piece_img.resize((size, size), Image.NEAREST)
        if piece_img.mode != "1":
            piece_img = piece_img.convert("1")

        # Full square tile: shown opaquely, so no transparency mask.
        return piece_img, None

    @staticmethod
    def _resample_lanczos():
        """LANCZOS filter constant, tolerant of the Pillow version in use."""
        try:
            return Image.Resampling.LANCZOS
        except AttributeError:
            return Image.LANCZOS

    @staticmethod
    def _bilevel_and_mask(img: Image.Image) -> Tuple[Image.Image, Image.Image]:
        """Threshold a (grayscale or 1-bit) logo to 1-bit + build its ink mask.

        A hard threshold is used rather than ``convert("1")`` because the latter
        applies Floyd-Steinberg dithering by default, which would speckle the
        line-art knight. The mask marks the black ink (the drawn knight) opaque
        so the surrounding page shows through on the e-paper.
        """
        bilevel = img.point(lambda p: 255 if p >= 128 else 0).convert("1")
        mask = Image.eval(bilevel, lambda p: 255 if p == 0 else 0).convert("1")
        return bilevel, mask

    def get_knight_logo(self, size: int = 100) -> Tuple[Optional[Image.Image], Optional[Image.Image]]:
        """Get the square knight-head logo image and its transparency mask.

        This is the head crop used for square placements (menu/icon buttons).
        For the full piece shown on the splash screen, see ``get_knight_logo_full``.

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

        # Resize the grayscale source with LANCZOS before thresholding so small
        # icon sizes keep clean edges (see _bilevel_and_mask).
        if img.size[0] != size or img.size[1] != size:
            img = img.resize((size, size), self._resample_lanczos())

        bilevel, mask = self._bilevel_and_mask(img)

        self._resized_cache[cache_key] = bilevel
        self._resized_cache[mask_cache_key] = mask

        return bilevel, mask

    def get_knight_logo_full(self, height: int) -> Tuple[Optional[Image.Image], Optional[Image.Image]]:
        """Get the full knight-piece logo scaled to ``height``, plus its mask.

        The full piece is portrait (taller than wide); the width is derived from
        the source aspect ratio so it is never squashed. Used by the splash
        screen, which reserves a tall logo band for it.

        Args:
            height: Target height in pixels. Width follows the source aspect.

        Returns:
            Tuple of (logo_image, mask_image), or (None, None) if not found.
        """
        img = self.get_image("knight_full.bmp")
        if img is None:
            return None, None

        src_w, src_h = img.size
        width = max(1, round(height * src_w / src_h))

        cache_key = ("knight_full.bmp", width, height)
        mask_cache_key = ("knight_full_mask.bmp", width, height)
        if cache_key in self._resized_cache and mask_cache_key in self._resized_cache:
            return self._resized_cache[cache_key], self._resized_cache[mask_cache_key]

        if (src_w, src_h) != (width, height):
            img = img.resize((width, height), self._resample_lanczos())

        bilevel, mask = self._bilevel_and_mask(img)

        self._resized_cache[cache_key] = bilevel
        self._resized_cache[mask_cache_key] = mask

        return bilevel, mask


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


def get_font(size: int, path: str = None) -> ImageFont.FreeTypeFont:
    """Resolve a font via the app-wide ResourceLoader, or PIL's default font.

    Single acquisition point for widgets. When a loader has been registered
    (set_resource_loader, done once at startup), resolution, caching and the
    fallback-to-default all happen inside it, so every caller shares one loader
    and its font cache. When no loader is registered (e.g. off-board or in
    tests) PIL's built-in default font is returned so rendering still works.

    Args:
        size: Font size in points.
        path: Optional explicit font path; defaults to the bundled Font.ttc.

    Returns:
        A PIL ImageFont, never None.
    """
    loader = get_resource_loader()
    if loader is not None:
        return loader.get_font(size, path)
    return ImageFont.load_default()
