"""Tests for the runtime string localizer (i18n package).

``t(key, **kwargs)`` resolves a key against the active-locale bundle, falling
back to English and then to the key itself, and interpolates via str.format.
The active locale is read from the language service and cached; these tests
force it via monkeypatch + refresh and reset the module cache around each test
so no locale leaks between tests.

Each test states the regression it guards and how it would surface.
"""

import pytest

from universalchess import i18n


@pytest.fixture(autouse=True)
def reset_i18n_state(monkeypatch):
    """Reset the cached active locale and bundles around each test.

    The module memoises the resolved locale and loaded bundles; without a reset
    a locale chosen by one test would leak into the next. Default the language
    to English unless a test overrides it, so lookups are deterministic offline.
    """
    i18n._active_locale = None
    i18n._bundles.clear()
    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: "en"
    )
    yield
    i18n._active_locale = None
    i18n._bundles.clear()


def _set_locale(monkeypatch, code):
    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: code
    )
    i18n.refresh_active_language()


def test_returns_english_for_known_key():
    """t() returns the English string for a known key in the default locale.

    Guards the base lookup path every widget relies on. A regression here (wrong
    bundle path, key mismatch) would surface as the raw key rendering on screen.
    """
    assert i18n.t("common.enabled") == "Enabled"
    assert i18n.t("game_over.result.white_wins") == "White wins"


def test_returns_spanish_when_locale_is_spanish(monkeypatch):
    """t() returns the Spanish string when the active locale is Spanish.

    Guards the whole point of the module: switching the device locale must change
    the rendered string. A regression that ignored the locale would keep showing
    English.
    """
    _set_locale(monkeypatch, "es")
    assert i18n.t("common.enabled") == "Activado"
    assert i18n.t("game_over.result.white_wins") == "Ganan las blancas"


def test_returns_french_when_locale_is_french(monkeypatch):
    """t() returns the French string when the active locale is French.

    Why: French is a shipped UI locale with its own bundle. A regression that
    wired only Spanish would keep showing English (or Spanish) here.
    """
    _set_locale(monkeypatch, "fr")
    assert i18n.t("common.enabled") == "Activé"
    assert i18n.t("game_over.result.white_wins") == "Les blancs gagnent"


def test_returns_german_when_locale_is_german(monkeypatch):
    """t() returns the German string when the active locale is German.

    Why: German is a shipped UI locale with its own bundle, added after Spanish
    and French. A regression that wired only the first two would keep showing
    English here.
    """
    _set_locale(monkeypatch, "de")
    assert i18n.t("common.enabled") == "Aktiviert"
    assert i18n.t("game_over.result.white_wins") == "Weiß gewinnt"


def test_returns_dutch_when_locale_is_dutch(monkeypatch):
    """t() returns the Dutch string when the active locale is Dutch.

    Why: Dutch was the first locale added to the selector itself rather than
    just given a bundle -- the other four were already offered. A regression in
    that registration (a code missing from the supported set, or a bundle the
    loader cannot find) shows up here as English text.
    """
    _set_locale(monkeypatch, "nl")
    assert i18n.t("common.enabled") == "Ingeschakeld"
    assert i18n.t("game_over.result.white_wins") == "Wit wint"


def test_returns_polish_when_locale_is_polish(monkeypatch):
    """t() returns the Polish string when the active locale is Polish.

    Why: Polish is the second locale added to the selector as well as given a
    bundle, and the first with a language whose plural/case endings differ from
    every locale already shipped. A regression in the registration (a code
    missing from the supported set, or a bundle the loader cannot find) shows up
    here as English text.
    """
    _set_locale(monkeypatch, "pl")
    assert i18n.t("common.enabled") == "Włączone"
    assert i18n.t("game_over.result.white_wins") == "Białe wygrywają"


def test_returns_italian_when_locale_is_italian(monkeypatch):
    """t() returns the Italian string when the active locale is Italian.

    Why: Italian is the third original-Centaur language added to the selector
    as well as given a bundle. A regression in that registration (a code missing
    from the supported set, or a bundle the loader cannot find) shows up here as
    English text while every earlier locale still passes.
    """
    _set_locale(monkeypatch, "it")
    assert i18n.t("common.enabled") == "Attivo"
    assert i18n.t("game_over.result.white_wins") == "Vincono i bianchi"


def test_interpolates_named_placeholders():
    """t() fills {name} placeholders from kwargs via str.format.

    Guards the on-screen keyboard's page indicator ("Page {current}/{max}"). A
    regression dropping interpolation would show the literal braces to the user.
    """
    assert i18n.t("keyboard.page", current=2, max=5) == "Page 2/5"


def test_falls_back_to_english_for_untranslated_key(monkeypatch):
    """A key absent from the Spanish bundle falls back to English, not blank.

    Guards graceful degradation for a partially translated locale. The test
    injects a Spanish bundle missing the key, so a regression that returned ""
    (or raised) instead of the English source would surface here.
    """
    _set_locale(monkeypatch, "es")
    # Force a Spanish bundle that lacks the key; English must fill the gap.
    i18n._bundles["es"] = {"common.enabled": "Activado"}
    i18n._bundles["en"] = {"common.enabled": "Enabled", "common.disabled": "Disabled"}
    assert i18n.t("common.disabled") == "Disabled"


def test_missing_key_returns_key_itself():
    """An unknown key returns the key string (visible, logged), never blank.

    Guards against a silent blank on a typo'd key: showing the key makes the
    mistake obvious in the UI and in logs. A regression returning None/"" would
    render nothing.
    """
    assert i18n.t("does.not.exist") == "does.not.exist"


@pytest.mark.parametrize("locale", ["es", "fr", "de", "nl", "pl", "it"])
def test_translated_bundle_covers_every_english_key(locale):
    """Every key in en.json has a translation in each shipped locale bundle.

    Why: a non-English UI must not silently fall back to English for a migrated
    string. This pins the bundles together so adding an English key without its
    counterpart fails here (listing the missing keys) rather than shipping
    mixed-language screens. How a regression manifests: a key present in en.json
    is absent from the locale file.
    """
    en = i18n._load_bundle("en")
    other = i18n._load_bundle(locale)
    missing = sorted(set(en) - set(other))
    assert missing == [], f"{locale}.json missing translations for: {missing}"
