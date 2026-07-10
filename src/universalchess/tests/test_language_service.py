"""Tests for the UI language service (services/language_service.py).

The service persists the chosen UI locale in ``[system] ui_language`` and reads
it back, mirroring the timezone service's persistence pattern (but without an OS
apply step -- the locale only selects which translations the board and web
render). Settings persistence is patched so the tests don't touch ``/opt``.

The UI locale is deliberately distinct from ``[game] coach_language`` (the
language of the AI coach's move commentary); a regression conflating the two
would surface as changing one affecting the other.

Each test states the regression it guards and how it would surface.
"""

import pytest

from universalchess.services import language_service as ls


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
    blank. English and Spanish are the launch set.
    """
    langs = ls.list_languages()
    values = {entry["value"] for entry in langs}
    assert values == {"en", "es"}
    assert all(entry.get("label") for entry in langs)
    labels = {entry["value"]: entry["label"] for entry in langs}
    assert labels["en"] == "English"
    assert labels["es"] == "Espanol"


def test_supported_matches_listed_values():
    """SUPPORTED and list_languages() agree.

    Guards drift between the validation set and the presented options: a locale
    accepted by set_language but not offered (or vice versa) would let the UI
    show an unselectable value or reject a value it just displayed.
    """
    assert ls.SUPPORTED == {entry["value"] for entry in ls.list_languages()}


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

    Guards the read path the board and web use to pick translations.
    """
    monkeypatch.setattr(ls.Settings, "read", lambda section, key, default="": "es")
    assert ls.get_language() == "es"


def test_get_language_falls_back_when_stored_value_unsupported(monkeypatch):
    """An unsupported persisted value resolves to the default, not itself.

    Why: a stale/corrupt ini value (e.g. a locale we removed) must not select a
    non-existent translation set. How a regression manifests: get_language leaks
    "fr" and the renderer has no bundle for it, falling back per-string in a way
    that mixes languages instead of cleanly using the default.
    """
    monkeypatch.setattr(ls.Settings, "read", lambda section, key, default="": "fr")
    assert ls.get_language() == "en"


def test_set_valid_language_persists(captured_writes):
    """A supported locale is written to [system] ui_language.

    Guards the happy path and the exact section/key/default written. Manifests
    as a missing write or a wrong section/key.
    """
    ls.set_language("es")
    assert captured_writes == [("system", "ui_language", "es", "en")]


def test_set_invalid_language_raises_and_does_not_persist(captured_writes):
    """An unsupported locale raises ValueError with no write.

    This is the validation boundary the API turns into a 400: a regression would
    let an arbitrary string be written and later select a missing bundle.
    Manifests as a recorded write for the bad value.
    """
    with pytest.raises(ValueError):
        ls.set_language("fr")
    assert captured_writes == []
