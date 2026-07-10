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


def test_spanish_bundle_covers_every_english_key():
    """Every key in en.json has a Spanish translation in es.json.

    Why: the Spanish UI must not silently fall back to English for a migrated
    string. This pins the two bundles together so adding an English key without
    its Spanish counterpart fails here (listing the missing keys) rather than
    shipping mixed-language screens. How a regression manifests: a key present in
    en.json is absent from es.json.
    """
    en = i18n._load_bundle("en")
    es = i18n._load_bundle("es")
    missing = sorted(set(en) - set(es))
    assert missing == [], f"es.json missing translations for: {missing}"
