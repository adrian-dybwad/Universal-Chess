"""Tests for the UI language service (services/language_service.py).

The service persists the chosen UI locale in ``[system] ui_language`` and reads
it back, mirroring the timezone service's persistence pattern (but without an OS
apply step). Settings persistence is patched so the tests don't touch ``/opt``.

The locale is the single device-wide language preference: it selects UI
translations *and* the language the AI coach writes in (via
``coach_language_name``). These tests pin the supported set, the label/coach-name
tables, the default fallback, and the persistence round-trip.

Each test states the regression it guards and how it would surface.
"""

import pytest

from universalchess.services import language_service as ls

# The launch set of locales offered by the selector. Pinned here so a change to
# SUPPORTED is a deliberate edit to this expectation, not a silent drift.
EXPECTED_LOCALES = {"en", "es", "zh", "hi", "ar", "fr", "ru", "pt", "de", "ja"}


@pytest.fixture
def captured_writes(monkeypatch):
    """Capture Settings.write calls and stub Settings.read, off the /opt store."""
    writes = []
    monkeypatch.setattr(ls.Settings, "write", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(ls.Settings, "read", lambda section, key, default="": default)
    return writes


def test_list_languages_returns_supported_with_labels():
    """list_languages() returns every supported locale with a display label.

    Guards the option-list source the board menu and web selector render. A
    regression dropping a locale or a label would leave the dropdown short or
    blank. Labels are endonyms; English and Spanish are checked explicitly and
    every entry must have a non-empty label.
    """
    langs = ls.list_languages()
    values = {entry["value"] for entry in langs}
    assert values == EXPECTED_LOCALES
    assert all(entry.get("label") for entry in langs)
    labels = {entry["value"]: entry["label"] for entry in langs}
    assert labels["en"] == "English"
    assert labels["es"] == "Español"


def test_supported_matches_listed_values():
    """SUPPORTED and list_languages() agree.

    Guards drift between the validation set and the presented options: a locale
    accepted by set_language but not offered (or vice versa) would let the UI
    show an unselectable value or reject a value it just displayed.
    """
    assert ls.SUPPORTED == EXPECTED_LOCALES
    assert ls.SUPPORTED == {entry["value"] for entry in ls.list_languages()}


def test_every_supported_locale_has_a_coach_language_name():
    """Each supported locale maps to a plain-English coach language name.

    Why: the coach follows the device locale, so every offerable locale must yield
    a name for the coach prompt. A regression adding a locale without a coach name
    would fall back to English commentary for that language while the UI claims to
    support it. Manifests as a missing key here.
    """
    for code in ls.SUPPORTED:
        name = ls.coach_language_name(code)
        assert name and name[0].isupper()
    # Representative mappings the coach prompt depends on.
    assert ls.coach_language_name("en") == "English"
    assert ls.coach_language_name("es") == "Spanish"
    assert ls.coach_language_name("de") == "German"
    assert ls.coach_language_name("ja") == "Japanese"


def test_coach_language_name_unknown_code_falls_back_to_english():
    """An unknown locale code maps to English, matching get_language's fallback.

    Why: a stale/removed code must not produce a directive for a language the
    device does not support; the coach then writes English, consistent with the
    UI's own fallback. A regression raising KeyError would break the coach for a
    corrupt ini value.
    """
    assert ls.coach_language_name("xx") == "English"


def test_get_language_defaults_to_english(monkeypatch):
    """get_language() falls back to the default when the store is empty.

    Guards the fresh-device/empty-key case: an unset ``ui_language`` must resolve
    to a real locale ("en"), not "" (which the renderer would treat as a missing
    locale and the selector would show blank).
    """
    monkeypatch.setattr(ls.Settings, "read", lambda section, key, default="": "")
    assert ls.get_language() == "en"
    assert ls.DEFAULT == "en"


def test_get_language_returns_persisted_supported_value(monkeypatch):
    """get_language() returns a persisted, supported locale.

    Guards the read path the board, web, and coach use. Uses a non-default locale
    (German) so a regression hard-coding "en" is caught.
    """
    monkeypatch.setattr(ls.Settings, "read", lambda section, key, default="": "de")
    assert ls.get_language() == "de"


def test_get_language_falls_back_when_stored_value_unsupported(monkeypatch):
    """An unsupported persisted value resolves to the default, not itself.

    Why: a stale/corrupt ini value (e.g. a locale we removed) must not select a
    non-existent translation set. How a regression manifests: get_language leaks
    "xx" and the renderer has no bundle for it, falling back per-string in a way
    that mixes languages instead of cleanly using the default. Uses "xx" (never a
    real locale) since the previously-invalid "fr" is now supported.
    """
    monkeypatch.setattr(ls.Settings, "read", lambda section, key, default="": "xx")
    assert ls.get_language() == "en"


def test_set_valid_language_persists(captured_writes):
    """A supported locale is written to [system] ui_language.

    Guards the happy path and the exact section/key/default written. Uses a
    non-default locale so a regression writing "en" is caught.
    """
    ls.set_language("de")
    assert captured_writes == [("system", "ui_language", "de", "en")]


def test_set_invalid_language_raises_and_does_not_persist(captured_writes):
    """An unsupported locale raises ValueError with no write.

    This is the validation boundary the API turns into a 400: a regression would
    let an arbitrary string be written and later select a missing bundle.
    Manifests as a recorded write for the bad value. Uses "xx" since "fr" is now a
    supported locale.
    """
    with pytest.raises(ValueError):
        ls.set_language("xx")
    assert captured_writes == []
