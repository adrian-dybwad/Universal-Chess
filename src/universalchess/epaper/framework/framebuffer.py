"""
Framebuffer management for e-paper display.
"""

from PIL import Image


class FrameBuffer:
    """Manages the display framebuffer for widget compositing."""
    
    def __init__(self, width: int, height: int, mode: str = "1"):
        self.width = width
        self.height = height
        self.mode = mode
        self._current = Image.new(mode, (width, height), 255)  # White

    def set_mode(self, mode: str) -> None:
        """Switch the compositing mode, discarding the current frame.

        ``L`` holds the four gray levels used by GDEW029I6FD. ``1`` is the
        black/white frame every other panel dithers into. The next render
        repaints the widgets, so the discarded frame is not shown.
        """
        if mode == self.mode:
            return
        self.mode = mode
        self._current = Image.new(mode, (self.width, self.height), 255)
    
    def get_canvas(self, rotation: int = 0) -> Image.Image:
        """Get the current framebuffer for rendering.
        
        Args:
            rotation: Rotation angle in degrees (0, 90, 180, or 270). Default is 0.
        
        Returns:
            Rotated image if rotation is specified, otherwise the original image.
        """
        if rotation == 0:
            return self._current
        return self._current.rotate(-rotation, expand=False)
    
    def snapshot(self, rotation: int = 0) -> Image.Image:
        """Get a snapshot of the current framebuffer.
        
        Args:
            rotation: Rotation angle in degrees (0, 90, 180, or 270). Default is 0.
        
        Returns:
            Rotated snapshot if rotation is specified, otherwise the original snapshot.
        """
        img = self._current.copy()
        if rotation == 0:
            return img
        return img.rotate(-rotation, expand=False)