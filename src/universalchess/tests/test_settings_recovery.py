#!/usr/bin/env python3
"""Tests for Settings config corruption recovery and atomic writes.

Why these tests exist:
  An unclean shutdown (power loss) on the SD card can leave the live
  ``centaur.ini`` zero-filled. configparser.read() raises ParsingError on such a
  file, and because the config is read at import time, the whole app crash-loops
  on boot with a blank screen. These tests pin two guarantees:

    1. Recovery: a corrupt live config is transparently restored from the
       bundled defaults (or, if those are unavailable, reset to empty) so the app
       boots instead of crashing.
    2. Prevention: config writes are atomic (temp file + os.replace), so an
       interrupted write can never leave the live file truncated/zero-filled -
       the root cause of the corruption.
"""

from __future__ import annotations

import configparser
import os

import pytest

from universalchess.board.settings import Settings


_VALID_DEFAULTS = (
    "[system]\ninactivity_timeout = 900\n\n"
    "[sound]\nsound = on\n\n"
    "[update]\nchannel = stable\n"
)

# 151 null bytes - the exact corruption shape observed on hardware after an
# unclean shutdown (the live centaur.ini was 151 bytes of 0x00).
_NULL_CORRUPTION = b"\x00" * 151


@pytest.fixture
def cfg_paths(tmp_path, monkeypatch):
    """Point Settings at a temp live + defaults file pair."""
    live = tmp_path / "centaur.ini"
    defaults = tmp_path / "defaults" / "centaur.ini"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(_VALID_DEFAULTS, encoding="utf-8")
    monkeypatch.setattr(Settings, "configfile", str(live))
    monkeypatch.setattr(Settings, "defconfigfile", str(defaults))
    return live, defaults


class TestCorruptConfigRecovery:

    def test_get_config_recovers_from_null_filled_file(self, cfg_paths):
        """A zero-filled live config is restored from defaults instead of
        raising.

        Regression manifestation: without recovery, get_config() propagates
        configparser.MissingSectionHeaderError at import time and the service
        crash-loops (the blank-screen boot loop seen on hardware).
        """
        live, _ = cfg_paths
        live.write_bytes(_NULL_CORRUPTION)

        config = Settings.get_config()  # must not raise

        # Restored content carries the default sections...
        assert config.has_section("sound")
        assert config["sound"]["sound"] == "on"
        # ...and the on-disk file is now parseable (corruption replaced).
        reparsed = configparser.ConfigParser()
        reparsed.read(str(live))
        assert reparsed.has_section("sound")

    def test_read_recovers_and_returns_value(self, cfg_paths):
        """Settings.read on a corrupt config recovers and returns the value
        from the restored defaults rather than raising.

        Regression manifestation: a raised ParsingError here aborts whatever
        feature called read() (e.g. board startup reading a system setting).
        """
        live, _ = cfg_paths
        live.write_bytes(_NULL_CORRUPTION)

        assert Settings.read("sound", "sound") == "on"

    def test_recovery_without_defaults_starts_empty(self, tmp_path, monkeypatch):
        """If the defaults file is also unavailable, a corrupt live config is
        reset to an empty (parseable) file so the app still boots and keys can
        be rebuilt by ensure_key_exists.

        Regression manifestation: a corrupt live config plus missing defaults
        would otherwise leave read() raising forever with no escape.
        """
        live = tmp_path / "centaur.ini"
        live.write_bytes(_NULL_CORRUPTION)
        monkeypatch.setattr(Settings, "configfile", str(live))
        monkeypatch.setattr(
            Settings, "defconfigfile", str(tmp_path / "nonexistent.ini"))

        config = Settings.get_config()  # must not raise

        assert config.sections() == []  # empty but valid
        # The corrupt bytes are gone: re-reading the file parses cleanly.
        reparsed = configparser.ConfigParser()
        reparsed.read(str(live))  # must not raise
        # ensure_key_exists can now rebuild a section from the (empty) defaults.
        Settings.ensure_key_exists("system", "inactivity_timeout", "900")
        assert Settings.read("system", "inactivity_timeout", "900") == "900"


class TestAtomicWrite:

    def test_written_config_is_parseable_and_leaves_no_temp(self, cfg_paths):
        """A normal write produces a parseable file and leaves no temp files
        behind in the config directory.

        Regression manifestation: a leftover temp file indicates the atomic
        rename path failed; a non-parseable result means the write regressed.
        """
        live, _ = cfg_paths
        Settings.write("sound", "sound", "off")

        reparsed = configparser.ConfigParser()
        reparsed.read(str(live))
        assert reparsed["sound"]["sound"] == "off"

        leftovers = [p for p in os.listdir(os.path.dirname(str(live)))
                     if p.endswith(".tmp")]
        assert leftovers == [], f"atomic write left temp files: {leftovers}"

    def test_failed_replace_leaves_original_intact(self, cfg_paths, monkeypatch):
        """If the atomic replace fails mid-write, the original file is left
        intact (never truncated/zero-filled) and no exception propagates.

        Regression manifestation: a plain in-place open(...,'w') truncates the
        target before writing, so an interrupted write leaves a zero-length or
        null file - exactly the corruption that crash-looped the board. This
        test fails (original content lost) if the write stops being atomic.
        """
        live, _ = cfg_paths
        # Seed a known-good live config.
        Settings.write("sound", "sound", "on")
        original = live.read_text(encoding="utf-8")

        # Simulate the rename step failing (e.g. power loss at the worst moment).
        def _boom(src, dst):
            raise OSError("simulated interrupted replace")

        monkeypatch.setattr(os, "replace", _boom)
        Settings.write("sound", "sound", "off")  # must not raise

        # The live file still holds the pre-write content, fully parseable.
        assert live.read_text(encoding="utf-8") == original
        reparsed = configparser.ConfigParser()
        reparsed.read(str(live))
        assert reparsed["sound"]["sound"] == "on"
        # And the failed write left no temp file behind.
        leftovers = [p for p in os.listdir(os.path.dirname(str(live)))
                     if p.endswith(".tmp")]
        assert leftovers == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
