#!/usr/bin/env python3
"""Tests pinning that reading settings is side-effect free and parses once.

Why these tests exist:
  Loading the app's settings took 32.8 seconds on a Pi Zero, reading a 1.8 KB
  file. ``Settings.read()`` parsed the whole file twice per key -- once inside
  ``ensure_key_exists()`` and once in ``_safe_read()`` -- and the per-key API
  meant ``AllSettings.load()`` issued ~246 reads, so ~492 full parses for what
  is logically one read of one file. ``ensure_key_exists()`` also made a *read*
  write to disk (materializing the key with its default, via an fsync'd atomic
  write), which is both a startup cost and a surprising contract.

  These tests pin three guarantees:

    1. Reading never writes. A read resolves a value; it must not create or
       modify the live config.
    2. A section is read with a single parse, so loading settings scales with
       the number of sections rather than the number of keys.
    3. Value resolution (live -> defaults file -> caller default) and type
       coercion are unchanged by the above.

  Guarantees 1 and 2 fail against the pre-fix implementation. Guarantee 3 is a
  characterization guard: it passes both before and after, and exists so the
  optimization cannot silently change what a setting resolves to.
"""

from __future__ import annotations

import configparser
from typing import Dict, List

import pytest

from universalchess.board.settings import Settings
from universalchess.utils.settings_persistence import load_section


# Live config: deliberately holds only SOME of the keys the tests ask for, so
# every arm of the resolution chain (live / defaults file / caller default) is
# exercised by a single fixture.
_LIVE_CONFIG = (
    "[game]\n"
    "notation = figurine\n"
    "text_size = 14\n"
    "analysis_mode = false\n"
)

# Defaults file: supplies a key absent from live, and deliberately does NOT
# supply `only_caller_default` so that key falls through to the caller default.
_DEFAULTS_CONFIG = (
    "[game]\n"
    "notation = algebraic\n"
    "chess_sprites = classic\n"
    "\n"
    "[sound]\n"
    "sound = on\n"
    "key_press = on\n"
)

# A section read must cost exactly one parse of the live config. The defaults
# file is immutable shipped data, so it must not be re-parsed per read either.
_MAX_LIVE_PARSES_PER_SECTION_READ = 1

# Keys used by the section-read tests, with the caller-supplied defaults that
# `load_section` also uses for type inference (bool / int / str).
_GAME_DEFAULTS: Dict[str, object] = {
    "notation": "algebraic",       # present in live -> live wins
    "text_size": 12,               # present in live -> live wins, coerced to int
    "analysis_mode": True,         # present in live -> live wins, coerced to bool
    "chess_sprites": "default",    # absent from live, present in defaults file
    "only_caller_default": "fallback",  # absent from both -> caller default
}

_EXPECTED_GAME_VALUES: Dict[str, object] = {
    "notation": "figurine",
    "text_size": 14,
    "analysis_mode": False,
    "chess_sprites": "classic",
    "only_caller_default": "fallback",
}


@pytest.fixture
def cfg_paths(tmp_path, monkeypatch):
    """Point Settings at a temp live + defaults file pair."""
    live = tmp_path / "centaur.ini"
    defaults = tmp_path / "defaults" / "centaur.ini"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(_LIVE_CONFIG, encoding="utf-8")
    defaults.write_text(_DEFAULTS_CONFIG, encoding="utf-8")
    monkeypatch.setattr(Settings, "configfile", str(live))
    monkeypatch.setattr(Settings, "defconfigfile", str(defaults))
    return live, defaults


@pytest.fixture
def parse_log(monkeypatch) -> List[str]:
    """Record every path handed to ConfigParser.read.

    Returns the list of parsed paths so a test can assert both how many parses
    happened and which file each one touched (live vs defaults).
    """
    calls: List[str] = []
    original = configparser.ConfigParser.read

    def _counting_read(self, filenames, encoding=None):
        calls.append(str(filenames))
        return original(self, filenames, encoding=encoding)

    monkeypatch.setattr(configparser.ConfigParser, "read", _counting_read)
    return calls


def _live_parses(parse_log: List[str], live_path) -> List[str]:
    return [p for p in parse_log if p == str(live_path)]


def _defaults_parses(parse_log: List[str], defaults_path) -> List[str]:
    return [p for p in parse_log if p == str(defaults_path)]


class TestReadIsSideEffectFree:
    """A read must resolve a value without touching the live config."""

    def test_read_of_absent_key_does_not_modify_live_config(self, cfg_paths):
        """Reading a key that is missing from the live config must leave the
        file byte-identical.

        Guards the fsync-on-read defect: ``read()`` called ``ensure_key_exists``,
        which materialized the missing key and wrote it back through an atomic,
        fsync'd write. Regression manifestation: this assertion fails because
        the file has grown a `chess_sprites = classic` line, and on hardware
        every first-touch read costs an SD-card fsync.
        """
        live, _ = cfg_paths
        before = live.read_text(encoding="utf-8")

        Settings.read("game", "chess_sprites", "default")

        assert live.read_text(encoding="utf-8") == before

    def test_read_does_not_create_absent_live_config(self, tmp_path, monkeypatch):
        """Reading when no live config exists must not create one.

        A read is not a migration. Regression manifestation: the file springs
        into existence (and the assertion fails), meaning a read-only operation
        wrote to disk during startup.
        """
        live = tmp_path / "centaur.ini"
        defaults = tmp_path / "defaults" / "centaur.ini"
        defaults.parent.mkdir(parents=True, exist_ok=True)
        defaults.write_text(_DEFAULTS_CONFIG, encoding="utf-8")
        monkeypatch.setattr(Settings, "configfile", str(live))
        monkeypatch.setattr(Settings, "defconfigfile", str(defaults))

        assert Settings.read("sound", "sound", "off") == "on"

        assert not live.exists()

    def test_read_of_absent_section_does_not_modify_live_config(self, cfg_paths):
        """A read naming a section absent from live must not add that section.

        ``ensure_key_exists`` added the whole section before adding the key, so
        an absent section triggered two writes. Regression manifestation: the
        live file gains an empty `[sound]` section and this assertion fails.
        """
        live, _ = cfg_paths
        before = live.read_text(encoding="utf-8")

        Settings.read("sound", "sound", "off")

        assert live.read_text(encoding="utf-8") == before


class TestSectionReadParsesOnce:
    """Loading a section must scale with sections, not keys."""

    def test_read_section_parses_live_config_once(self, cfg_paths, parse_log):
        """Reading a whole section costs exactly one parse of the live config.

        This is the guarantee that turns ~492 parses into 4 at startup.
        Regression manifestation: the live-parse count rises with the number of
        keys requested (pre-fix it was 2 per key), so this fails with a count of
        10 rather than 1 for the five keys below.
        """
        live, _ = cfg_paths

        Settings.read_section("game", {k: str(v) for k, v in _GAME_DEFAULTS.items()})

        assert len(_live_parses(parse_log, live)) == _MAX_LIVE_PARSES_PER_SECTION_READ

    def test_defaults_file_is_not_reparsed_per_missing_key(self, cfg_paths, parse_log):
        """The immutable defaults file must not be re-parsed for every miss.

        Two of the requested keys are absent from live. Pre-fix, each miss built
        a fresh ConfigParser over the defaults file. Regression manifestation:
        the defaults-parse count scales with the number of missing keys instead
        of staying at most one.
        """
        _, defaults = cfg_paths

        Settings.read_section("game", {k: str(v) for k, v in _GAME_DEFAULTS.items()})

        assert len(_defaults_parses(parse_log, defaults)) <= 1

    def test_load_section_parses_live_config_once(self, cfg_paths, parse_log):
        """The public loader used by AllSettings inherits the single-parse cost.

        ``load_section`` is what ``AllSettings.load()`` calls four times; if it
        still fans out to per-key reads the startup win never materializes.
        Regression manifestation: live-parse count is 2x the key count, which is
        exactly the 32.8-second startup block this work removes.
        """
        live, _ = cfg_paths

        load_section("game", _GAME_DEFAULTS)

        assert len(_live_parses(parse_log, live)) == _MAX_LIVE_PARSES_PER_SECTION_READ


class TestResolutionUnchanged:
    """Characterization: the optimization must not change resolved values.

    These pass before and after the change. They exist so a faster read cannot
    silently resolve a setting differently -- which would be a far worse bug
    than the slowness being fixed.
    """

    @pytest.mark.parametrize(
        "key,expected,why",
        [
            ("notation", "figurine", "present in live config -> live wins"),
            ("chess_sprites", "classic", "absent from live -> defaults file"),
            ("only_caller_default", "fallback", "absent from both -> caller default"),
        ],
    )
    def test_read_resolution_chain(self, cfg_paths, key, expected, why):
        """Each arm of the live -> defaults-file -> caller-default chain.

        Regression manifestation: a wrong value here means a user's configured
        setting is being ignored, or a shipped default is not being honored.
        """
        assert Settings.read("game", key, "fallback") == expected, why

    def test_read_section_matches_individual_reads(self, cfg_paths):
        """A section read returns exactly what N individual reads return.

        The two APIs must not diverge, or behavior would depend on which one a
        caller happened to use. Regression manifestation: a key resolves
        differently in bulk than singly -- e.g. a missing key falls back to the
        defaults file in one path and the caller default in the other.
        """
        raw_defaults = {k: str(v) for k, v in _GAME_DEFAULTS.items()}

        bulk = Settings.read_section("game", raw_defaults)
        individual = {
            key: Settings.read("game", key, default)
            for key, default in raw_defaults.items()
        }

        assert bulk == individual

    def test_load_section_preserves_values_and_types(self, cfg_paths):
        """Type coercion (bool/int/str) is inferred from the caller default.

        Asserts the full shape -- every key, value and type -- because checking
        only values would miss `analysis_mode` coming back as the string
        "false" (which is truthy) instead of the bool False. Regression
        manifestation: a disabled setting reads as enabled.
        """
        loaded = load_section("game", _GAME_DEFAULTS)

        assert loaded == _EXPECTED_GAME_VALUES
        assert loaded.keys() == _GAME_DEFAULTS.keys()
        for key, expected in _EXPECTED_GAME_VALUES.items():
            assert type(loaded[key]) is type(expected), (
                f"{key} coerced to {type(loaded[key]).__name__}, "
                f"expected {type(expected).__name__}")

    def test_empty_section_returns_all_caller_defaults(self, cfg_paths):
        """The empty case: a section present in neither file yields the caller
        defaults for every key, with types intact.

        Regression manifestation: a fresh install (no live config, no matching
        defaults) gets empty strings or None instead of the declared defaults,
        so the app starts up misconfigured rather than with its shipped values.
        """
        loaded = load_section("section_that_does_not_exist", _GAME_DEFAULTS)

        assert loaded == _GAME_DEFAULTS


class TestCorruptConfigStillRecovers:
    """The corruption-recovery contract must survive the optimization."""

    # 151 null bytes - the corruption shape observed on hardware after an
    # unclean shutdown (see test_settings_recovery.py).
    _NULL_CORRUPTION = b"\x00" * 151

    def test_read_section_recovers_from_corrupt_live_config(self, cfg_paths):
        """A zero-filled live config is recovered, not propagated as an error.

        The single-parse path must keep routing through the same recovery as
        ``_safe_read``. Regression manifestation: configparser raises
        MissingSectionHeaderError out of startup and the board crash-loops with
        a blank screen -- the original boot-loop bug.
        """
        live, _ = cfg_paths
        live.write_bytes(self._NULL_CORRUPTION)

        loaded = Settings.read_section("sound", {"sound": "off", "key_press": "off"})

        # Recovered from the bundled defaults, which set both to "on".
        assert loaded == {"sound": "on", "key_press": "on"}


class TestWritesStillPersist:
    """Dropping ensure_key_exists from the write path must not lose writes."""

    def test_write_then_read_roundtrips_a_new_key(self, cfg_paths):
        """Writing a key absent from both files persists and reads back.

        ``write()`` also called ``ensure_key_exists`` (redundantly, since it
        sets the key itself). Regression manifestation: removing that call drops
        the section creation too, so the value never lands and the read returns
        the caller default.
        """
        Settings.write("newsection", "newkey", "newvalue")

        assert Settings.read("newsection", "newkey", "unset") == "newvalue"

    def test_write_overwrites_existing_value(self, cfg_paths):
        """An existing key is updated in place rather than duplicated.

        Regression manifestation: the old value survives (read returns
        "figurine"), meaning a settings change silently does not take effect.
        """
        Settings.write("game", "notation", "algebraic")

        assert Settings.read("game", "notation", "unset") == "algebraic"

    def test_delete_removes_key_and_read_falls_back(self, cfg_paths):
        """Deleting a live key falls back down the resolution chain.

        `notation` exists in both the live and defaults files, so deleting the
        live copy must expose the defaults-file value. Regression manifestation:
        the deleted key is immediately re-materialized (the old
        ensure_key_exists behavior) and the live value appears to persist.
        """
        Settings.delete("game", "notation")

        assert Settings.read("game", "notation", "unset") == "algebraic"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
