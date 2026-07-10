"""Manage the device's UI language (locale).

The selected locale (``en`` / ``es``) is persisted in ``[system] ui_language``
and selects which translations the board menus, the e-paper widgets, and the
React web app render. It is a device-wide preference, mirroring the timezone
service's persistence pattern -- but, unlike the timezone, it has no OS apply
step: nothing outside this application's rendering depends on it, so there is no
privileged helper to invoke.

This is intentionally distinct from ``[game] coach_language`` (the language of
the AI coach's move commentary). Conflating them would let a change to how the
menus read alter the coach's output (or vice versa); they are separate concerns
and separate settings.

An *unsupported* locale (an unknown or removed value in the ini, or an invalid
value from a caller) resolves to :data:`DEFAULT` on read and raises on write, so
the renderer is never handed a locale it has no bundle for.
"""

import logging
from typing import Dict, List

from universalchess.board.settings import Settings

log = logging.getLogger(__name__)

_SECTION = "system"
_KEY = "ui_language"

# The launch set of UI locales. Adding a language means adding its code here and
# providing its translation bundles/overlays for each surface (catalog overlay,
# Python widget strings, and the web app) -- no other code change is required.
DEFAULT = "en"
SUPPORTED = {"en", "es"}

# Display labels for the selector, in presentation order. Labels are written in
# the language they name (an endonym) so a user who cannot read the current UI
# language can still recognise their own -- "Espanol" is legible to a Spanish
# speaker regardless of the active locale.
_LABELS: Dict[str, str] = {
    "en": "English",
    "es": "Espanol",
}
_ORDER: List[str] = ["en", "es"]


def list_languages() -> List[dict]:
    """Return the supported locales as ``{"value", "label"}`` option entries.

    The board menu and the web Settings selector render from this one source so
    the offered set cannot drift from :data:`SUPPORTED`.
    """
    return [{"value": code, "label": _LABELS[code]} for code in _ORDER]


def get_language() -> str:
    """Return the device's current UI locale as a supported code.

    Reads ``[system] ui_language`` and guarantees a value the renderer has a
    bundle for: an empty or unsupported stored value resolves to :data:`DEFAULT`
    rather than being surfaced verbatim (which would select a missing bundle and
    mix languages per-string).
    """
    stored = Settings.read(_SECTION, _KEY, DEFAULT) or DEFAULT
    if stored not in SUPPORTED:
        log.warning("ui_language: unsupported stored value %r; using %r", stored, DEFAULT)
        return DEFAULT
    return stored


def set_language(code: str) -> None:
    """Persist ``code`` as the device UI locale.

    Raises:
        ValueError: if ``code`` is not in :data:`SUPPORTED`, so an unknown locale
            is never written (the API turns this into a 400). Validating here
            keeps the persisted value one the renderer can always honour.
    """
    if code not in SUPPORTED:
        raise ValueError(f"unsupported language: {code!r}")
    Settings.write(_SECTION, _KEY, code, DEFAULT)
