"""Tests for GameSettings persistence of the chess_sprites selection.

Why these tests exist
---------------------
The Display > Board > Sprites selector reads the current sheet from
GameSettings.to_dict() and persists changes via set(). chess_sprites was missing
from the GameSettings dataclass, so to_dict() never returned it and load() never
read it back. The menu therefore always saw "default", so cycling never advanced
past the first step and the on-screen label never changed.

How the regression manifests
----------------------------
- to_dict() lacks the 'chess_sprites' key (KeyError below), or
- load() ignores a stored value and falls back to the default.
"""

import pytest

import universalchess.players.settings as settings_mod
from universalchess.players.settings import (
    ANALYSIS_TIME_PRESETS,
    GameSettings,
    analysis_time_seconds,
)


def test_to_dict_includes_selected_chess_sprites():
    # Guards that an explicitly set sheet survives the to_dict() round-trip the
    # menu uses to read the current selection. Without the field this raises
    # TypeError on construction; with a broken to_dict it KeyErrors below.
    settings = GameSettings(section="game", chess_sprites="staunton")
    assert settings.to_dict()["chess_sprites"] == "staunton"


def test_chess_sprites_defaults_to_default():
    # The default selection must be the bundled "default" sheet so a fresh
    # install renders the standard pieces.
    settings = GameSettings(section="game")
    assert settings.to_dict()["chess_sprites"] == "default"


def test_load_reads_stored_chess_sprites(monkeypatch):
    # load() must surface a persisted selection; otherwise the menu and startup
    # always re-read "default" and the user's choice is silently ignored.
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["chess_sprites"] = "staunton"
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {"chess_sprites": "default"})
    assert settings.chess_sprites == "staunton"
    assert settings.to_dict()["chess_sprites"] == "staunton"


def test_notation_defaults_to_figurine():
    # The move-history notation must default to figurine so a fresh install shows
    # figurine glyphs on both the board and web without any explicit config. A
    # missing field or wrong default would surface as SAN/absent notation.
    settings = GameSettings(section="game")
    assert settings.to_dict()["notation"] == "figurine"


def test_to_dict_includes_selected_notation():
    # Guards the to_dict() round-trip the menu/web read the current notation
    # through. Without the field this raises TypeError on construction; a broken
    # to_dict() KeyErrors here.
    settings = GameSettings(section="game", notation="lan")
    assert settings.to_dict()["notation"] == "lan"


def test_load_reads_stored_notation(monkeypatch):
    # load() must surface a persisted notation; otherwise the widget and web
    # always re-read the default and the user's choice is silently ignored.
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["notation"] = "uci"
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {"notation": "figurine"})
    assert settings.notation == "uci"
    assert settings.to_dict()["notation"] == "uci"


def test_text_size_defaults_to_medium():
    # The display text size must default to medium so a fresh install renders the
    # existing (unscaled) coach and move-list layout. A missing field or wrong
    # default would resize the e-paper text without the user choosing to.
    settings = GameSettings(section="game")
    assert settings.to_dict()["text_size"] == "medium"


def test_to_dict_includes_selected_text_size():
    # Guards the to_dict() round-trip the board menu reads the current text size
    # through (game store -> to_dict). Without the field this raises TypeError on
    # construction; a broken to_dict() KeyErrors here.
    settings = GameSettings(section="game", text_size="large")
    assert settings.to_dict()["text_size"] == "large"


def test_load_reads_stored_text_size(monkeypatch):
    # load() must surface a persisted text size; otherwise the board menu and web
    # always re-read medium and the user's Text Size choice is silently ignored. The
    # fake omits text_size from its explicit defaults to prove load() seeds the read
    # default itself (setdefault), so load_section actually reads the stored key.
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["text_size"] = "small"
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {})
    assert settings.text_size == "small"
    assert settings.to_dict()["text_size"] == "small"


def test_analysis_time_preset_defaults_to_quick():
    # A fresh install must keep the historical 0.3s behavior so nothing changes for
    # existing users until they opt into a longer analysis. A missing field or wrong
    # default would silently lengthen every board analysis (more CPU/battery).
    settings = GameSettings(section="game")
    assert settings.to_dict()["analysis_time_preset"] == "quick"


def test_to_dict_includes_selected_analysis_time_preset():
    # Guards the to_dict() round-trip the board menu and web read the current preset
    # through (game store -> to_dict). Without the field this raises TypeError on
    # construction; a broken to_dict() KeyErrors here.
    settings = GameSettings(section="game", analysis_time_preset="deep")
    assert settings.to_dict()["analysis_time_preset"] == "deep"


def test_load_reads_stored_analysis_time_preset(monkeypatch):
    # load() must surface a persisted preset; otherwise the board and web always
    # re-read "quick" and the user's choice is silently ignored. The fake omits the
    # key from explicit defaults to prove load() seeds the read default itself.
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["analysis_time_preset"] = "standard"
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {})
    assert settings.analysis_time_preset == "standard"
    assert settings.to_dict()["analysis_time_preset"] == "standard"


@pytest.mark.parametrize(
    "preset, expected",
    [("quick", 0.3), ("standard", 0.8), ("deep", 2.0)],
)
def test_analysis_time_seconds_maps_each_preset(preset, expected):
    # The preset name is stored, but AnalysisService needs seconds; this mapping is
    # the single conversion point. A wrong mapping would make the board search for
    # the wrong duration (e.g. "deep" behaving like "quick"), defeating the setting.
    assert analysis_time_seconds(preset) == expected
    # The preset table and the mapping must agree (no drift between the two).
    assert analysis_time_seconds(preset) == ANALYSIS_TIME_PRESETS[preset]


@pytest.mark.parametrize("bad", ["", "turbo", "0.3", None])
def test_analysis_time_seconds_unknown_falls_back_to_quick(bad):
    # A stale/typo'd config value must resolve to the safe historical default rather
    # than raising on the game thread or fabricating an arbitrary number. Regression:
    # a KeyError here would crash game start; a made-up value would search for an
    # undocumented duration.
    assert analysis_time_seconds(bad) == 0.3


def test_ponder_defaults_to_off():
    # Pondering must default off so a fresh install (including battery boards) does
    # not spawn a dedicated engine burning CPU/power without the user opting in. A
    # missing field or wrong default would enable pondering silently.
    settings = GameSettings(section="game")
    assert settings.to_dict()["ponder"] is False


def test_to_dict_includes_ponder():
    # Guards the to_dict() round-trip the board menu/web read the ponder flag
    # through. Without the field this raises TypeError on construction; a broken
    # to_dict() KeyErrors here.
    settings = GameSettings(section="game", ponder=True)
    assert settings.to_dict()["ponder"] is True


def test_load_reads_stored_ponder(monkeypatch):
    # load() must surface a persisted ponder flag; otherwise the setting is
    # inert and the engine never ponders even when the user enabled it. The fake
    # omits ponder from explicit defaults to prove load() seeds the read default
    # itself (setdefault) so load_section actually reads the stored key.
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["ponder"] = True
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {})
    assert settings.ponder is True
    assert settings.to_dict()["ponder"] is True


def test_coach_multipv_defaults_to_one():
    # MultiPV must default to 1 (no alternatives) so a fresh install keeps the
    # current single-line coaching cost/behavior until the user raises it. A
    # missing field or wrong default would run extra analysis unbidden.
    settings = GameSettings(section="game")
    assert settings.to_dict()["coach_multipv"] == 1


def test_to_dict_includes_selected_coach_multipv():
    # Guards the to_dict() round-trip the web reads the current MultiPV through.
    # Without the field this raises TypeError on construction; a broken to_dict()
    # KeyErrors here.
    settings = GameSettings(section="game", coach_multipv=3)
    assert settings.to_dict()["coach_multipv"] == 3


def test_load_reads_stored_coach_multipv(monkeypatch):
    # load() must surface a persisted MultiPV count as an int; otherwise the coach
    # never receives candidate lines even when configured. The fake omits
    # coach_multipv from explicit defaults to prove load() seeds the read default
    # itself (setdefault), and returns an int (load_int coerces the stored string).
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["coach_multipv"] = 4
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {})
    assert settings.coach_multipv == 4
    assert settings.to_dict()["coach_multipv"] == 4


def test_lichess_clock_defaults_to_rapid_10_0():
    """A fresh install must seek a Board API Rapid, not the Game Blitz 5+0.

    How a regression manifests: the default is empty or 5+0, so the first
    Seek New Game is rejected as Invalid time control.
    """
    settings = GameSettings(section="game")
    assert settings.to_dict()["lichess_clock"] == "rapid_10_0"


def test_to_dict_includes_lichess_clock():
    """Guards the to_dict() round-trip the lobby and web read the clock through."""
    settings = GameSettings(section="game", lichess_clock="none")
    assert settings.to_dict()["lichess_clock"] == "none"


def test_load_reads_stored_lichess_clock(monkeypatch):
    """load() must surface a persisted lobby clock; otherwise None is forgotten.

    How a regression manifests: correspondence is saved on the web and the
    board re-reads Rapid on the next boot.
    """
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["lichess_clock"] = "none"
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {})
    assert settings.lichess_clock == "none"
    assert settings.to_dict()["lichess_clock"] == "none"


def test_lichess_color_defaults_to_random():
    """A fresh install seeks random until the lobby Color row is changed.

    How a regression manifests: White or Black is posted on every new seek.
    """
    settings = GameSettings(section="game")
    assert settings.to_dict()["lichess_color"] == "random"


def test_to_dict_includes_lichess_color():
    """Guards the to_dict() round-trip the lobby and web read the color through."""
    settings = GameSettings(section="game", lichess_color="black")
    assert settings.to_dict()["lichess_color"] == "black"


def test_load_reads_stored_lichess_color(monkeypatch):
    """load() must surface a persisted lobby color; otherwise White is forgotten.

    How a regression manifests: White is saved on the web and the board
    re-reads Random on the next boot.
    """
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["lichess_color"] = "white"
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {})
    assert settings.lichess_color == "white"
    assert settings.to_dict()["lichess_color"] == "white"


def test_chess960_defaults_to_off():
    # Chess960 must default off so a fresh install starts standard games; a
    # missing field or wrong default would silently randomize every new game.
    settings = GameSettings(section="game")
    assert settings.to_dict()["chess960"] is False


def test_to_dict_includes_chess960():
    # Guards the to_dict() round-trip the board menu/web read the chess960 flag
    # through. Without the field this raises TypeError on construction; a broken
    # to_dict() KeyErrors here.
    settings = GameSettings(section="game", chess960=True)
    assert settings.to_dict()["chess960"] is True


def test_load_reads_stored_chess960(monkeypatch):
    # load() must surface a persisted chess960 flag; otherwise the setting is
    # inert and new games never randomize even when the user enabled it. The fake
    # omits chess960 from explicit defaults to prove load() seeds the read default
    # itself (setdefault) so load_section actually reads the stored key.
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["chess960"] = True
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {})
    assert settings.chess960 is True
    assert settings.to_dict()["chess960"] is True


def test_alert_queen_threat_defaults_to_on():
    # The YOUR QUEEN warning must stay on for a fresh install and for every
    # existing config that predates the setting: it is the behavior the board has
    # always had, and the toggle only exists to opt out. A missing field or a
    # False default would silently remove the warning for everyone.
    settings = GameSettings(section="game")
    assert settings.to_dict()["alert_queen_threat"] is True


def test_to_dict_includes_alert_queen_threat():
    # Guards the to_dict() round-trip the board menu and the web Game tab read the
    # toggle's current position through. Without the field this raises TypeError on
    # construction; a broken to_dict() KeyErrors here.
    settings = GameSettings(section="game", alert_queen_threat=False)
    assert settings.to_dict()["alert_queen_threat"] is False


def test_load_reads_stored_alert_queen_threat(monkeypatch):
    # load() must surface a persisted opt-out; otherwise the toggle appears to save
    # (the web echoes it back) but the board re-reads True on the next boot and
    # keeps warning. The fake omits the key from the caller defaults to prove
    # load() seeds the read default itself, so load_section actually reads it.
    def fake_load_section(section, defaults):
        data = dict(defaults)
        data["alert_queen_threat"] = False
        return data

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    settings = GameSettings.load("game", {})
    assert settings.alert_queen_threat is False
    assert settings.to_dict()["alert_queen_threat"] is False


def _faithful_load_section(stored: dict):
    """Build a load_section fake that honors its real contract.

    The production ``load_section`` reads ONLY keys that appear in the ``defaults``
    it is given (it uses them for both the key set and per-key type inference). The
    other fakes in this file inject a key regardless of ``defaults``, so they can
    pass even when a field is missing from the read set -- masking the exact bug
    below. This fake instead returns a value ONLY for keys present in ``defaults``,
    coercing the stored string by the default's type the way load_bool/load_int do.
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


@pytest.mark.parametrize(
    "key,stored,expected",
    [
        # Regression guards. GAME_SETTINGS_DEFAULTS omitted these three keys, and
        # because load_section only reads keys present in its defaults, a stored
        # value was never read back -- the board stayed on the hardcoded default no
        # matter what the web saved (e.g. LED brightness set to 10 on the web still
        # showed 5 on the board). Each row fails before the fix because load() did
        # not seed the read default for that key from the dataclass.
        ("led_brightness", "10", 10),
        ("pegasus_override_brightness", "False", False),
        ("notation", "uci", "uci"),
        # A stored opt-out from the YOUR QUEEN warning must be read back with the
        # same guarantee: read through a load_section that honors its real contract
        # (only keys present in the defaults are read), so the field has to be in
        # the derived read set. Otherwise the board boots warning again.
        ("alert_queen_threat", "False", False),
    ],
)
def test_load_reads_stored_value_when_caller_omits_default(monkeypatch, key, stored, expected):
    # Passes an empty caller-defaults dict, exactly like the production call path
    # (GAME_SETTINGS_DEFAULTS did not list these keys). With a faithful load_section
    # that only reads keys it has a default for, the stored value reaches the
    # instance only if load() seeds every persisted field's read default from the
    # dataclass. How it manifests otherwise: the assertion sees the default (5 /
    # True / "figurine") instead of the stored value.
    monkeypatch.setattr(settings_mod, "load_section", _faithful_load_section({key: stored}))
    settings = GameSettings.load("game", {})
    assert getattr(settings, key) == expected
    assert settings.to_dict()[key] == expected
