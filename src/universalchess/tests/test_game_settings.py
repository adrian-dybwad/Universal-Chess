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

import universalchess.players.settings as settings_mod
from universalchess.players.settings import GameSettings


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
