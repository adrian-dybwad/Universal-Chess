#!/usr/bin/env python3
"""Tests for the pure PLAY-button decision helper.

Why these tests exist:
  The PLAY button is a single universal control: it starts a new game, resumes a
  suspended one, or suspends the running game back to the menu. The decision is
  pure (depends only on whether the game screen is showing and whether a
  suspended game exists), so it is pinned here independently of the hardware
  routing in main.py. A regression in this mapping would make PLAY do the wrong
  thing in one of the three contexts (e.g. start a new game on top of a
  suspended one, losing it).
"""

from universalchess.menus.play_action import PlayAction, decide_play_action


class TestDecidePlayAction:

    def test_game_showing_suspends(self):
        """While the game screen is showing, PLAY suspends to the menu,
        regardless of any other state.

        Regression manifestation: returning START_NEW/RESUME here would replace
        or restart the running game instead of pausing it back to the menu.
        """
        assert decide_play_action(
            app_state_is_game=True, has_suspended_game=False) is PlayAction.SUSPEND
        # has_suspended_game is irrelevant while in the game; SUSPEND still wins.
        assert decide_play_action(
            app_state_is_game=True, has_suspended_game=True) is PlayAction.SUSPEND

    def test_menu_with_suspended_game_resumes(self):
        """In the menu with a game in progress, PLAY resumes it.

        Regression manifestation: returning START_NEW would discard the
        in-progress game and begin a fresh one - the worst data-loss outcome.
        """
        assert decide_play_action(
            app_state_is_game=False, has_suspended_game=True) is PlayAction.RESUME

    def test_menu_without_game_starts_new(self):
        """In the menu with no game in progress, PLAY starts a new game.

        Regression manifestation: returning RESUME with no game to resume would
        do nothing (or crash) and PLAY would feel dead from a fresh menu.
        """
        assert decide_play_action(
            app_state_is_game=False, has_suspended_game=False) is PlayAction.START_NEW
