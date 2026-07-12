"""Tests for persisting user-entered custom positions.

Custom positions entered from the web Positions page are written to a separate
overlay file (``positions.custom.ini``) and merged over the packaged defaults by
``load_positions_config``. Keeping user entries in their own file means the
packaged ``positions.ini`` stays pristine and still receives updates on upgrade,
while the board and the web UI share one merged catalog.

Each test states the specific behaviour it guards and how a regression would
manifest, so an unrelated breakage that happens to surface here is
distinguishable from the regression the test was written for.
"""

import configparser
import pathlib
import tempfile
import unittest

from universalchess.utils.positions import (
    add_custom_position,
    load_positions_config,
)

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class TestAddCustomPosition(unittest.TestCase):
    """Behaviour of ``add_custom_position`` writing to the overlay file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.overlay = pathlib.Path(self._tmp.name) / "positions.custom.ini"

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_entry_under_custom_section(self):
        """A saved position lands in the [custom] section of the overlay file.

        Regression: if the write targeted the wrong section (or the packaged
        file), the overlay would not contain a [custom]/<name> key and this
        read-back would raise NoSectionError / NoOptionError.
        """
        key = add_custom_position("My Opening", START_FEN, overlay_path=str(self.overlay))

        self.assertEqual(key, "my_opening")
        config = configparser.ConfigParser(interpolation=None)
        config.read(str(self.overlay))
        self.assertEqual(config.sections(), ["custom"])
        self.assertEqual(config.get("custom", "my_opening"), START_FEN)

    def test_hint_is_appended_with_pipe(self):
        """A legal hint is stored as ``FEN | hint`` so parse_position_entry reads it.

        Regression: a missing/misplaced separator would make the whole value
        parse as a FEN with 7+ fields and be dropped on load (hint lost).
        """
        add_custom_position("With Hint", START_FEN, "e2e4", overlay_path=str(self.overlay))

        config = configparser.ConfigParser(interpolation=None)
        config.read(str(self.overlay))
        self.assertEqual(config.get("custom", "with_hint"), f"{START_FEN} | e2e4")

    def test_same_name_overwrites_rather_than_duplicates(self):
        """Re-saving under the same display name updates the single entry.

        Regression: without normalisation to a stable key, "My Opening" and
        "my opening" would create two keys; the count assertion catches the
        duplication a presence check would miss.
        """
        add_custom_position("My Opening", START_FEN, overlay_path=str(self.overlay))
        other = "8/8/8/8/8/8/8/4K2k w - - 0 1"
        add_custom_position("my opening", other, overlay_path=str(self.overlay))

        config = configparser.ConfigParser(interpolation=None)
        config.read(str(self.overlay))
        self.assertEqual(list(config["custom"].keys()), ["my_opening"])
        self.assertEqual(config.get("custom", "my_opening"), other)

    def test_rejects_fen_with_wrong_field_count(self):
        """A FEN missing fields is rejected before any file is written.

        Regression: writing a malformed FEN would be silently dropped by the
        loader later, so the user's position would vanish with no error.
        """
        with self.assertRaises(ValueError):
            add_custom_position("bad", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w", overlay_path=str(self.overlay))
        self.assertFalse(self.overlay.exists())

    def test_rejects_illegal_fen(self):
        """A six-field but illegal FEN (no kings) is rejected.

        Regression: a field-count-only check would accept unplayable positions
        that crash the engine/board when set up.
        """
        with self.assertRaises(ValueError):
            add_custom_position("no kings", "8/8/8/8/8/8/8/8 w - - 0 1", overlay_path=str(self.overlay))

    def test_rejects_empty_name(self):
        """A name that normalises to nothing is rejected.

        Regression: an empty key would be written and shadow every future add,
        or produce an unnameable entry in the UI.
        """
        with self.assertRaises(ValueError):
            add_custom_position("   ", START_FEN, overlay_path=str(self.overlay))

    def test_rejects_illegal_hint(self):
        """A hint that is not a legal move in the position is rejected.

        Regression: an illegal hint stored on disk would surface a wrong/broken
        hint on the board with no way for the user to know it was invalid.
        """
        with self.assertRaises(ValueError):
            add_custom_position("bad hint", START_FEN, "e2e5", overlay_path=str(self.overlay))

    def test_name_injection_is_sanitised(self):
        """A name containing INI control characters cannot inject structure.

        Regression: newlines / '=' / '[' in the name could add rogue keys or
        sections; normalisation to [a-z0-9_] collapses them to a single safe key
        and the file must still contain exactly one section and one key.
        """
        add_custom_position("evil]\n[inject", START_FEN, overlay_path=str(self.overlay))

        config = configparser.ConfigParser(interpolation=None)
        config.read(str(self.overlay))
        self.assertEqual(config.sections(), ["custom"])
        self.assertEqual(len(config["custom"]), 1)


class TestLoadMergesOverlay(unittest.TestCase):
    """Behaviour of ``load_positions_config`` merging overlay over the base."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.overlay = pathlib.Path(self._tmp.name) / "positions.custom.ini"

    def tearDown(self):
        self._tmp.cleanup()

    def test_custom_entry_appears_alongside_defaults(self):
        """A saved custom position is merged in without dropping default categories.

        Regression: if the loader read only the overlay (clobbering the base),
        the built-in categories would disappear; asserting both a default
        category and the new custom entry are present catches either failure.
        """
        add_custom_position("My Study", START_FEN, overlay_path=str(self.overlay))

        merged = load_positions_config(overlay_path=str(self.overlay))

        # A packaged category still present (base not clobbered).
        self.assertIn("game_over", merged)
        self.assertTrue(merged["game_over"], "default category should retain its entries")
        # The new custom entry is present with the exact FEN saved.
        self.assertIn("custom", merged)
        self.assertIn("my_study", merged["custom"])
        self.assertEqual(merged["custom"]["my_study"], (START_FEN, None))

    def test_hint_round_trips_through_load(self):
        """A saved hint is parsed back out by the merged loader.

        Regression: a serialisation/format mismatch between add and load would
        yield a None hint here even though a hint was saved.
        """
        add_custom_position("Hinted", START_FEN, "e2e4", overlay_path=str(self.overlay))

        merged = load_positions_config(overlay_path=str(self.overlay))

        self.assertEqual(merged["custom"]["hinted"], (START_FEN, "e2e4"))

    def test_absent_overlay_is_a_no_op(self):
        """With no overlay file, load returns only the packaged defaults.

        Regression: touching / requiring the overlay would raise on a fresh
        install where the user has saved nothing yet.
        """
        merged = load_positions_config(overlay_path=str(self.overlay))

        self.assertFalse(self.overlay.exists())
        self.assertIn("game_over", merged)


if __name__ == "__main__":
    unittest.main()
