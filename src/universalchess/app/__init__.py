"""Application bring-up: everything that has to happen before the board runs.

The board's startup has a strict order -- the previous-shutdown audit reads the
OS logs before the controller is touched, resources are loaded before any widget
is built, and the panel is driving a splash screen before the slow imports and
the controller handshake begin, so the user sees the board wake rather than a
blank panel. That order used to be expressed as top-level statements in
``main.py``, which made importing the entry point boot the product.

Here it is a function, :func:`universalchess.app.bootstrap.boot`, so the
application modules can be imported without hardware.
"""
