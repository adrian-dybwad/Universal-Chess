"""Round-trip tests for the time-control game settings.

Why these tests exist
---------------------
The structured time control (preset selection plus a custom builder) is
persisted as flat scalar keys in the ``[game]`` section of centaur.ini. These
new keys must (a) default so a fresh install behaves like the legacy
minutes-only clock, (b) survive the to_dict() round-trip the board menu and web
read through, and (c) be seeded into the load() read set from the dataclass so a
stored value is actually read back. A field missing from to_dict()/load() would
make the setting inert -- the board would always re-read the default and the
user's choice would be silently ignored (the same class of bug that previously
hit led_brightness/notation).

The tests also verify build_time_control resolves a real GameSettings instance,
tying the persistence layer to the clock model.
"""

import pytest

import universalchess.players.settings as settings_mod
from universalchess.players.settings import GameSettings
from universalchess.state.time_control import DelayMode, build_time_control


# Each row: (attribute, default) -- the fresh-install defaults that keep the
# clock behaving like the legacy minutes-only control until reconfigured.
_DEFAULTS = [
    ("time_control_preset", ""),
    ("tc_custom_base_seconds", 300),
    ("tc_custom_increment_seconds", 0),
    ("tc_custom_delay_seconds", 0),
    ("tc_custom_delay_mode", "none"),
    ("tc_custom_asymmetric", False),
    ("tc_custom_black_base_seconds", 300),
    ("tc_custom_black_increment_seconds", 0),
]


@pytest.mark.parametrize("attr,default", _DEFAULTS)
def test_time_control_field_defaults(attr, default):
    """Each time-control field defaults so a fresh install is unchanged.

    Why: an empty preset and a "none" delay mode mean build_time_control falls
    back to the legacy minutes, preserving existing behavior. A wrong default
    (e.g. a preset name) would silently change every fresh install's clock.
    """
    settings = GameSettings(section="game")
    assert getattr(settings, attr) == default
    assert settings.to_dict()[attr] == default


@pytest.mark.parametrize("attr,value", [
    ("time_control_preset", "blitz_5_3"),
    ("tc_custom_base_seconds", 180),
    ("tc_custom_increment_seconds", 2),
    ("tc_custom_delay_seconds", 5),
    ("tc_custom_delay_mode", "bronstein"),
    ("tc_custom_asymmetric", True),
    ("tc_custom_black_base_seconds", 60),
    ("tc_custom_black_increment_seconds", 1),
])
def test_to_dict_round_trips_explicit_value(attr, value):
    """An explicitly set field survives the to_dict() round-trip.

    Why: the board menu reads the current selection through to_dict(); a field
    absent from to_dict() would KeyError, and a missing dataclass field would
    TypeError on construction.
    """
    settings = GameSettings(section="game", **{attr: value})
    assert settings.to_dict()[attr] == value


def _faithful_load_section(stored: dict):
    """load_section fake honoring the real contract (reads only known defaults).

    The production loader reads ONLY keys present in the defaults it is given and
    coerces the stored string by the default's type. This fake reproduces that so
    a field is read back only if load() seeds its read default from the dataclass
    -- the exact condition that was broken for led_brightness/notation.
    """

    def fake(section, defaults):
        result = {}
        for key, default in defaults.items():
            if key not in stored:
                result[key] = default
                continue
            raw = stored[key]
            if isinstance(default, bool):
                result[key] = str(raw).strip().lower() == "true"
            elif isinstance(default, int):
                result[key] = int(raw)
            else:
                result[key] = str(raw)
        return result

    return fake


@pytest.mark.parametrize("key,stored,expected", [
    ("time_control_preset", "rapid_10_5", "rapid_10_5"),
    ("tc_custom_base_seconds", "180", 180),
    ("tc_custom_increment_seconds", "2", 2),
    ("tc_custom_delay_seconds", "5", 5),
    ("tc_custom_delay_mode", "simple", "simple"),
    ("tc_custom_asymmetric", "true", True),
    ("tc_custom_black_base_seconds", "60", 60),
    ("tc_custom_black_increment_seconds", "1", 1),
])
def test_load_reads_stored_time_control_field(monkeypatch, key, stored, expected):
    """load() surfaces a persisted value (int/bool coerced) with empty caller defaults.

    Why: the production call passes no per-key defaults for these, so the value
    reaches the instance only if load() seeds the read default from the dataclass
    itself. How a regression manifests: the assertion sees the field's default
    instead of the stored value, meaning the board ignores the saved choice.
    """
    monkeypatch.setattr(settings_mod, "load_section", _faithful_load_section({key: stored}))
    settings = GameSettings.load("game", {})
    assert getattr(settings, key) == expected
    assert settings.to_dict()[key] == expected


def test_build_time_control_from_real_settings_preset():
    """build_time_control resolves a real GameSettings preset selection.

    Why: ties the persistence layer to the clock model -- selecting a preset in
    settings must produce the matching control at game start.
    """
    settings = GameSettings(section="game", time_control_preset="blitz_5_3")
    tc = build_time_control(settings)
    assert tc.initial_seconds("white") == 300
    assert tc.increment_after_move("white", 1) == 3


def test_build_time_control_from_real_settings_custom_asymmetric():
    """build_time_control assembles the custom asymmetric control from settings.

    Why: the board custom builder writes the tc_custom_* fields; the resolver
    must honor the asymmetric toggle and the per-side black fields.
    """
    settings = GameSettings(
        section="game",
        time_control_preset="custom",
        tc_custom_base_seconds=300,
        tc_custom_increment_seconds=0,
        tc_custom_delay_seconds=4,
        tc_custom_delay_mode="simple",
        tc_custom_asymmetric=True,
        tc_custom_black_base_seconds=120,
        tc_custom_black_increment_seconds=2,
    )
    tc = build_time_control(settings)
    assert tc.is_symmetric is False
    assert tc.initial_seconds("white") == 300
    assert tc.initial_seconds("black") == 120
    assert tc.increment_after_move("black", 1) == 2
    assert tc.delay_seconds == 4
    assert tc.delay_mode is DelayMode.SIMPLE


def test_build_time_control_legacy_minutes_fallback():
    """With an empty preset, build_time_control uses legacy time_control minutes.

    Why: existing configs only have time_control set; they must keep working. How
    a regression manifests: ignoring the legacy field would make upgraded boards
    start untimed.
    """
    settings = GameSettings(section="game", time_control=15)
    tc = build_time_control(settings)
    assert tc.initial_seconds("white") == 900
    assert tc.is_symmetric is True
