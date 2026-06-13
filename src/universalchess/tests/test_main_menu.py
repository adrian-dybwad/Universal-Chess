#!/usr/bin/env python3
"""Tests for the main-menu entry builder.

Why these tests exist:
  The top menu entry doubles as both "start a new game" and "resume the
  suspended game". Its label must reflect which one PLAY will do, so the user
  knows whether a game is still in progress. These tests pin the RESUME/PLAY
  relabel and that the entry key is stable (the main loop routes on the key, not
  the label).
"""

from universalchess.menus.main_menu import create_main_menu_entries


def _top_entry(entries):
    return entries[0]


class TestMainMenuPlayResumeLabel:

    def test_no_game_shows_play(self):
        """With no game in progress the top entry reads PLAY.

        Regression manifestation: showing RESUME with nothing to resume would
        mislead the user into thinking a game is still going.
        """
        entry = _top_entry(create_main_menu_entries(game_in_progress=False))
        assert entry.label == "PLAY"
        assert entry.key == "Universal"

    def test_game_in_progress_shows_resume(self):
        """With a game in progress the top entry reads RESUME but keeps the
        same key so the main loop's routing is unchanged.

        Regression manifestation: if the key changed, selecting the row would
        no longer enter the game; if the label did not change, the user could
        not tell a game was suspended.
        """
        entry = _top_entry(create_main_menu_entries(game_in_progress=True))
        assert entry.label == "RESUME"
        assert entry.key == "Universal"

    def test_default_is_play(self):
        """Omitting the flag defaults to PLAY, preserving the original
        signature's behavior for existing callers.
        """
        entry = _top_entry(create_main_menu_entries())
        assert entry.label == "PLAY"
