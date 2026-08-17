"""Manage the device's UI language (locale).

The selected locale is persisted in ``[system] ui_language`` and is the single
device-wide language preference. It selects which translations the board menus,
the e-paper widgets, and the React web app render, *and* the language the AI coach
writes its move commentary in -- there is no separate coach-language setting.
Mirrors the timezone service's persistence pattern, but without an OS apply step:
nothing outside this application's rendering depends on it.

Two facets of a locale:

- **UI translation** (menus/widgets/web): driven by translation bundles/overlays
  keyed by the locale code. Only locales that ship bundles render translated; a
  locale without bundles (or a key missing from one) falls back to English. The
  full :data:`SUPPORTED` set is offered even when its UI bundles are absent,
  because the second facet still applies:
- **Coach commentary**: the coach is an LLM that writes in any of these languages
  from a plain-English language name (see :func:`coach_language_name`), so every
  supported locale yields correctly-localized coach output regardless of whether
  its UI bundle exists yet.

An *unsupported* locale (an unknown or removed value in the ini, or an invalid
value from a caller) resolves to :data:`DEFAULT` on read and raises on write, so
neither the renderer nor the coach is ever handed a locale it cannot honour.
"""

import logging
from typing import Dict, List

from universalchess.board.settings import Settings

log = logging.getLogger(__name__)

_SECTION = "system"
_KEY = "ui_language"

# The supported UI locales (ISO 639-1 codes). Adding a language means adding its
# code here plus its label and coach name below; UI translation bundles/overlays
# are optional (missing ones fall back to English), but the coach can already
# write in the new language from its :func:`coach_language_name`.
#
# The first ten are the world's most-spoken languages, in that order. Dutch is
# listed after them on a different basis: the hardware this runs on is a DGT
# board, made in the Netherlands, so its home market reads Dutch even though the
# language is nowhere near the top ten by speakers.
DEFAULT = "en"
SUPPORTED = {"en", "es", "zh", "hi", "ar", "fr", "ru", "pt", "de", "ja", "nl"}

# Display labels for the selector, in presentation order. Written as endonyms (in
# the language they name) so a speaker recognises their own language regardless of
# the currently-active UI locale.
_LABELS: Dict[str, str] = {
    "en": "English",
    "es": "Español",
    "zh": "中文",
    "hi": "हिन्दी",
    "ar": "العربية",
    "fr": "Français",
    "ru": "Русский",
    "pt": "Português",
    "de": "Deutsch",
    "ja": "日本語",
    "nl": "Nederlands",
}
_ORDER: List[str] = ["en", "es", "zh", "hi", "ar", "fr", "ru", "pt", "de", "ja", "nl"]

# Plain-English language name for each locale, used to instruct the coach LLM
# ("Write your entire response in <name>"). English by design (the model reads the
# instruction reliably in English and the coach's guardrail prompt compares
# against the English name "English" to decide whether to add the directive).
_COACH_NAMES: Dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "zh": "Chinese",
    "hi": "Hindi",
    "ar": "Arabic",
    "fr": "French",
    "ru": "Russian",
    "pt": "Portuguese",
    "de": "German",
    "ja": "Japanese",
    "nl": "Dutch",
}


def list_languages() -> List[dict]:
    """Return the supported locales as ``{"value", "label"}`` option entries.

    The board menu and the web Settings selector render from this one source so
    the offered set cannot drift from :data:`SUPPORTED`.
    """
    return [{"value": code, "label": _LABELS[code]} for code in _ORDER]


def coach_language_name(code: str) -> str:
    """Return the plain-English language name the coach should write in.

    Maps a stored/normalised locale code to the name fed to the coach prompt (e.g.
    ``"es" -> "Spanish"``). An unknown code resolves to the default's name
    (English), matching :func:`get_language`'s fallback so the coach never gets a
    directive for a locale the device does not actually support.
    """
    return _COACH_NAMES.get(code, _COACH_NAMES[DEFAULT])


def get_language() -> str:
    """Return the device's current UI locale as a supported code.

    Reads ``[system] ui_language`` and guarantees a value the renderer and coach
    can honour: an empty or unsupported stored value resolves to :data:`DEFAULT`
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
            keeps the persisted value one the renderer and coach can always honour.
    """
    if code not in SUPPORTED:
        raise ValueError(f"unsupported language: {code!r}")
    Settings.write(_SECTION, _KEY, code, DEFAULT)
