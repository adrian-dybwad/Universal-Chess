"""The splash screen shown while the board starts, and progress notes for it.

Startup progress is reported from two places that cannot hold a reference to
each other: :mod:`universalchess.app.bootstrap` creates the splash before the
application module exists, and that module then reports its own slow imports
while it is still being imported. A module-level handle is what both can reach.

Every note is a no-op when no splash is registered, which is the case in tests
and in any process that never brings up a panel, so progress reporting never
becomes a reason a module cannot be imported.
"""

from typing import Optional

from universalchess.epaper import SplashScreen
from universalchess.i18n import t

_splash: Optional[SplashScreen] = None


def set_splash(splash: Optional[SplashScreen]) -> None:
    """Register the splash that startup progress is reported to."""
    global _splash
    _splash = splash


def current() -> Optional[SplashScreen]:
    """Return the startup splash, or None when the panel never came up."""
    return _splash


def note(key: str) -> None:
    """Show a translated startup step on the splash, if there is one."""
    if _splash is not None:
        _splash.set_message(t(key))
