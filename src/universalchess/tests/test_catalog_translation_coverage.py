"""Every translatable catalog string is translated, or declared not to need it.

Why these tests exist
---------------------
``localize_catalog`` falls back to English for anything an overlay omits, so a
string added to ``menu.json`` and forgotten in ``translations/es.json`` renders
as a working menu in the wrong language -- nothing fails and nothing is logged.
The Rated row reached the Lichess lobby that way and sat there in English on
both translated boards. Its sibling guard (test_catalog_localization) only
checks the other direction, that no overlay key is stale.

The contract here is that every user-visible catalog string is *addressed*: it
is either present in each shipped overlay, or listed in
:data:`SAME_IN_EVERY_SHIPPED_LOCALE` with the reason it reads the same in
Spanish and French. Presence, not difference, is what is checked -- "Color" is
already Spanish and "Positions" already French, and an overlay entry equal to
its English source is a translator's decision, not a gap.

What this does not check is freshness: an overlay entry translated from English
text that has since been rewritten still counts as present. The overlays record
no source text to compare against, so that drift is caught by reading, not here.
"""

import json
import re
from pathlib import Path

import pytest

from universalchess.menus.catalog.loader import (
    TRANSLATABLE_ACCOUNT_FIELD_KEYS,
    TRANSLATABLE_NODE_KEYS,
    load_catalog,
    load_overlay,
)

TRANSLATIONS_DIR = Path(__file__).resolve().parents[1] / "menus" / "catalog" / "translations"

# Locales with an overlay file. English is the authored source and has none.
SHIPPED_LOCALES = ("es", "fr", "de", "nl", "pl", "it")

# Runtime substitutions -- ``{fn:play_label}``, ``{value}``. A string made only
# of these carries no words to translate.
_PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")

# Strings that read the same in every shipped locale, so no overlay entry is
# owed. Each is here for a stated reason; anything not listed must be translated.
# Adding a locale means re-reading this list, which the shipped-locales test
# forces (a new overlay file fails until SHIPPED_LOCALES names it).
SAME_IN_EVERY_SHIPPED_LOCALE = {
    # Product and protocol names.
    "settings.chess960.label": "Chess960 is the variant's name in every language",
    "settings.chess960.boardLabel": "Chess960 is the variant's name in every language",
    "connectivity.wifi.label": "WiFi is a trademark, not a translated word",
    "connectivity.bluetooth.label": "Bluetooth is a trademark, not a translated word",
    "connectivity.chromecast.label": "Chromecast is a trademark, not a translated word",
    "field.display.pegasus_override_brightness.boardLabel": (
        "DGT Pegasus is a product name and LED is the same initialism in es/fr/de/nl/pl/it"
    ),
    "accountType:lichess.label": "Lichess is the service's name in every language",
    # An example value shown in an empty input, not prose.
    "accountType:lichess.api_token.placeholder": "a token's shape, not words",
    # Whole option sets whose labels carry nothing to translate.
    "optionSet:coach_multipv": "bare numerals",
    "optionSet:ui_language": (
        "each language is named in itself (Espanol, Francais), which is the point"
    ),
    "optionSet:tc_base": "'min' is the SI symbol for minute, spelt so in every shipped locale",
    # Place names spelt identically in every shipped locale. The rest of the set
    # is translated (London -> Londres, Moscow -> Moskau), so these are listed
    # one by one.
    "optionSet:timezones_common[UTC]": "an acronym",
    "optionSet:timezones_common[America/Chicago]": "spelt the same in es/fr/de/nl/pl/it",
    "optionSet:timezones_common[America/Denver]": "spelt the same in es/fr/de/nl/pl/it",
    "optionSet:timezones_common[America/Sao_Paulo]": (
        "spelt the same in es/fr/de/nl/pl; Italian overlays San Paolo, which is allowed"
    ),
    "optionSet:timezones_common[Pacific/Auckland]": "spelt the same in es/fr/de/nl/pl/it",
}


def _carries_words(text) -> bool:
    """True when ``text`` holds something to translate once placeholders go.

    ``{fn:players_summary}`` is a computed label and ``{value}`` a substitution;
    a string that is nothing else has no words an overlay could translate.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    return any(char.isalpha() for char in _PLACEHOLDER_RE.sub("", text))


def _untranslated_paths(menu: dict, overlay: dict) -> list:
    """Return the catalog string paths this overlay leaves in English.

    Paths are the keys of :data:`SAME_IN_EVERY_SHIPPED_LOCALE`, so a failure
    names exactly what to add to the overlay -- or to the exemption list.
    """
    missing = []

    node_overlay = overlay.get("nodes", {})
    for node in menu.get("nodes", []):
        translated = node_overlay.get(node["id"], {})
        for key in TRANSLATABLE_NODE_KEYS:
            if _carries_words(node.get(key)) and key not in translated:
                missing.append(f"{node['id']}.{key}")

    option_overlay = overlay.get("optionSets", {})
    for name, options in menu.get("optionSets", {}).items():
        translated = option_overlay.get(name, {})
        for option in options:
            value = str(option.get("value"))
            entry = translated.get(value)
            label_missing = entry is None or (
                isinstance(entry, dict) and "label" not in entry
            )
            if _carries_words(option.get("label")) and label_missing:
                missing.append(f"optionSet:{name}[{value}]")
            description_missing = not isinstance(entry, dict) or "description" not in entry
            if _carries_words(option.get("description")) and description_missing:
                missing.append(f"optionSet:{name}[{value}].description")

    account_overlay = overlay.get("accountTypes", {})
    for account in menu.get("accountTypes", []):
        translated = account_overlay.get(account["id"], {})
        if _carries_words(account.get("label")) and "label" not in translated:
            missing.append(f"accountType:{account['id']}.label")
        field_overlay = translated.get("fields", {})
        for field in account.get("fields", []):
            field_strings = field_overlay.get(field["key"], {})
            for key in TRANSLATABLE_ACCOUNT_FIELD_KEYS:
                if _carries_words(field.get(key)) and key not in field_strings:
                    missing.append(f"accountType:{account['id']}.{field['key']}.{key}")

    return missing


def _exempt(path: str) -> bool:
    """True when ``path`` (or the option set containing it) needs no translation."""
    if path in SAME_IN_EVERY_SHIPPED_LOCALE:
        return True
    if path.startswith("optionSet:"):
        set_name = path.split(":", 1)[1].split("[", 1)[0]
        return f"optionSet:{set_name}" in SAME_IN_EVERY_SHIPPED_LOCALE
    return False


@pytest.fixture
def menu():
    """The parsed English catalog, which the overlays are measured against."""
    return load_catalog().raw_menu()


@pytest.mark.parametrize("locale", SHIPPED_LOCALES)
def test_every_translatable_catalog_string_is_translated(menu, locale):
    """No catalog string reaches a translated board still in English.

    Why: the overlay falls back to English silently, so a string added to
    menu.json and not to the overlay ships as an English row in a Spanish menu
    with nothing raised or logged. How the regression manifests: the offending
    paths are listed by name, to be added to the overlay or, when they read the
    same in both locales, to SAME_IN_EVERY_SHIPPED_LOCALE with the reason.
    """
    overlay = load_overlay(locale)
    assert overlay is not None, f"{locale} overlay must exist"

    missing = sorted(p for p in _untranslated_paths(menu, overlay) if not _exempt(p))
    assert missing == [], f"{locale}.json translates nothing for: {missing}"


def test_exempt_paths_still_exist_in_the_catalog(menu):
    """Every exemption names a string the catalog still has.

    Why: an exemption for a renamed or deleted node stops guarding anything, and
    the next string that inherits the path inherits the exemption with it. How
    the regression manifests: paths that no longer resolve are listed, to be
    dropped from the list along with whatever removed them.
    """
    # An overlay that translates nothing yields every translatable path, which
    # is the full set an exemption may name.
    every_path = set(_untranslated_paths(menu, {}))
    option_sets = {f"optionSet:{name}" for name in menu.get("optionSets", {})}

    stale = sorted(
        path
        for path in SAME_IN_EVERY_SHIPPED_LOCALE
        if path not in every_path and path not in option_sets
    )
    assert stale == [], f"exemptions for strings the catalog no longer has: {stale}"


def test_shipped_locales_match_the_overlay_files_on_disk():
    """SHIPPED_LOCALES names exactly the overlays that exist.

    Why: coverage is only checked for the locales listed here, so a new overlay
    file would otherwise be exempt from every check above, and the reasons in
    SAME_IN_EVERY_SHIPPED_LOCALE -- which speak for the locales they were read
    against -- would silently speak for a language nobody re-read them against.
    How the regression manifests: an overlay file with no entry here, or an
    entry with no file.
    """
    on_disk = sorted(path.stem for path in TRANSLATIONS_DIR.glob("*.json"))
    assert on_disk == sorted(SHIPPED_LOCALES)


@pytest.mark.parametrize("locale", SHIPPED_LOCALES)
def test_overlay_files_are_utf8_json_objects(locale):
    """Each overlay parses as UTF-8 JSON with the sections the loader reads.

    Why: load_overlay swallows a JSON error and returns None, which degrades the
    whole locale to English at runtime rather than failing. How the regression
    manifests: a malformed or re-encoded file (accents mangled by a non-UTF-8
    save) fails here instead of quietly untranslating a board.
    """
    parsed = json.loads((TRANSLATIONS_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    # ``_comment`` heads each file; the rest are the sections the loader applies.
    assert set(parsed) <= {"_comment", "nodes", "optionSets", "sections", "accountTypes"}
    assert parsed.get("nodes"), f"{locale}.json translates no nodes"
