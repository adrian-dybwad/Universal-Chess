"""Lightweight runtime string localization for the board's Python UI.

The menu catalog is localized separately (see
:mod:`universalchess.menus.catalog.loader`); this module covers the *other*
user-facing Python strings that are not authored in ``menu.json`` -- e-paper
widget text (game-over result, setup prompt, on-screen keyboard, passkey, about),
board splash messages, and computed menu labels (e.g. Enabled/Disabled).

Design goals and rationale:

- **Flat, dot-keyed JSON bundles** (``locale/<code>.json``): the simplest store
  that supports ``str.format`` interpolation. English (:data:`DEFAULT_LOCALE`) is
  the source and the fallback; a key missing from a non-English bundle falls back
  to English, and a key missing everywhere returns the key itself (so a typo is
  visible and logged, never a blank).
- **No gettext toolchain**: no ``.po``/``.mo`` compilation step to run on the Pi;
  translations are plain JSON edited in place.
- **Cached active locale**: the device locale is read from
  :mod:`universalchess.services.language_service` once and cached, so rendering a
  menu row does not hit the ini file (and its ensure-key write path) per string.
  :func:`refresh_active_language` re-reads it after a language change; the board's
  settings hot-reload calls that so the next render uses the new locale.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

_LOCALE_DIR = Path(__file__).parent / "locale"

# The source locale: keys are authored in English and English is always the
# fallback. Kept in sync with ``language_service.DEFAULT`` but defined locally so
# this leaf utility does not depend on the service for the common path.
DEFAULT_LOCALE = "en"

_bundles: Dict[str, Dict[str, str]] = {}
_active_locale: Optional[str] = None


def _load_bundle(locale: str) -> Dict[str, str]:
    """Return the (cached) flat key->string bundle for ``locale``.

    A missing bundle file yields an empty mapping (so lookups fall back to
    English); an unparseable one is logged and also treated as empty rather than
    crashing the UI. Keys beginning with ``_`` are treated as authoring comments
    and dropped so they can annotate the JSON without leaking as translations.
    """
    cached = _bundles.get(locale)
    if cached is not None:
        return cached
    path = _LOCALE_DIR / f"{locale}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except json.JSONDecodeError as exc:
        log.warning("i18n bundle %s is not valid JSON: %s", path, exc)
        raw = {}
    bundle = {key: value for key, value in raw.items() if not key.startswith("_")}
    _bundles[locale] = bundle
    return bundle


def _read_active_locale() -> str:
    """Read the device UI locale from the language service, defaulting to English.

    Any failure to resolve the service degrades to :data:`DEFAULT_LOCALE` so
    string rendering never breaks the board.
    """
    try:
        from universalchess.services.language_service import get_language
        return get_language()
    except Exception:  # noqa: BLE001 - locale resolution must never break rendering; fall back to English
        log.warning("i18n could not resolve UI language; using %r", DEFAULT_LOCALE, exc_info=True)
        return DEFAULT_LOCALE


def refresh_active_language() -> str:
    """Re-read and cache the device UI locale; return it.

    Called after a language change (board settings hot-reload / the Language
    menu) so the next :func:`t` uses the new locale without a restart.
    """
    global _active_locale
    _active_locale = _read_active_locale()
    return _active_locale


def _current_locale() -> str:
    """Return the active locale, resolving and caching it on first use."""
    global _active_locale
    if _active_locale is None:
        _active_locale = _read_active_locale()
    return _active_locale


def t(key: str, /, **kwargs: object) -> str:
    """Translate ``key`` into the active locale, interpolating ``kwargs``.

    Resolution order: active-locale bundle, then the English bundle, then the key
    itself (logged as missing). When ``kwargs`` are given they are substituted
    with :meth:`str.format`; a malformed template (missing placeholder) returns
    the raw string rather than raising, so a bad interpolation degrades to
    readable text instead of crashing the render.
    """
    locale = _current_locale()
    value = _load_bundle(locale).get(key)
    if value is None and locale != DEFAULT_LOCALE:
        value = _load_bundle(DEFAULT_LOCALE).get(key)
    if value is None:
        log.warning("i18n missing translation key: %r", key)
        return key
    if not kwargs:
        return value
    try:
        return value.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        log.warning("i18n bad interpolation for key %r with %r", key, kwargs)
        return value


__all__ = ["DEFAULT_LOCALE", "refresh_active_language", "t"]
