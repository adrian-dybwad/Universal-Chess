#!/usr/bin/env python3
"""Tests for the root Main menu rendered through the menu engine.

Background / why these tests exist
----------------------------------
The Main menu is now rendered from the shared ``main`` catalog container via the
engine (see ``main._build_main_menu_entries``); the bespoke
create_main_menu_entries builder was removed. The root loop still dispatches by
entry key, so these tests pin the runtime variations the builder used to apply:
the top row's PLAY/RESUME relabel, hiding Lichess when no account is saved, and
hiding Original Centaur when the Centaur software is absent. A fake context
supplies the same store/compute main.py registers, so the catalog wiring is
exercised without board hardware.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows


def _main_ctx(*, game_in_progress=False, centaur_available=True, lichess_available=True):
    """Context mirroring main._build_main_menu_context for the root menu."""
    values = {
        "centaur_available": centaur_available,
        "lichess_available": lichess_available,
    }

    def main_get(key):
        if key in values:
            return values[key]
        raise KeyError(key)

    ctx = BoardMenuContext()
    ctx.register_store(
        "main",
        main_get,
        lambda k, v: (_ for _ in ()).throw(NotImplementedError(k)),
    )
    ctx.register_value(
        "play_label",
        lambda node: node["label_in_progress"] if game_in_progress else node["label"],
    )
    return ctx


def _rows(**kwargs):
    return build_rows("main", _main_ctx(**kwargs), platform="board", catalog=load_catalog())


class TestMainMenuPlayResumeLabel:

    def test_no_game_shows_play(self):
        """With no game in progress the top row reads PLAY and keys "Universal".

        Regression manifestation: showing RESUME with nothing to resume would
        mislead the user; a changed key would break the loop's routing.
        """
        top = _rows(game_in_progress=False)[0]
        assert top.key == "Universal"
        assert top.label == "PLAY"

    def test_game_in_progress_shows_resume(self):
        """With a game in progress the top row reads RESUME but keeps its key.

        Regression manifestation: if the key changed, selecting the row would no
        longer enter the game; if the label did not change, the user could not
        tell a game was suspended.
        """
        top = _rows(game_in_progress=True)[0]
        assert top.key == "Universal"
        assert top.label == "RESUME"


class TestMainMenuCentaurVisibility:

    def test_centaur_shown_when_available(self):
        """Original Centaur appears when the Centaur software is present.

        Regression manifestation: a broken visibleWhen would hide a working
        fallback path the user relies on to return to the original software.
        """
        keys = [r.key for r in _rows(centaur_available=True)]
        assert keys == ["Universal", "Lichess", "Centaur", "Positions", "Settings"]

    def test_centaur_hidden_when_unavailable(self):
        """Original Centaur is hidden when the Centaur software is absent.

        Regression manifestation: showing Centaur with nothing installed leaves a
        dead row that fails or does nothing when selected.
        """
        keys = [r.key for r in _rows(centaur_available=False)]
        assert keys == ["Universal", "Lichess", "Positions", "Settings"]


class TestMainMenuLichessVisibility:

    def test_lichess_shown_when_an_account_exists(self):
        """Lichess appears on the root menu when a credential is saved.

        How the regression manifests: a broken visibleWhen hides the lobby on a
        board that can actually play online.
        """
        keys = [r.key for r in _rows(lichess_available=True, centaur_available=False)]
        assert keys == ["Universal", "Lichess", "Positions", "Settings"]

    def test_lichess_hidden_when_no_account_is_saved(self):
        """Lichess is omitted until a token exists, so the root menu stays Play.

        How the regression manifests: a dead Lichess row sits on boards that
        have never added an account. First-token setup stays on the web card.
        """
        keys = [r.key for r in _rows(lichess_available=False, centaur_available=False)]
        assert keys == ["Universal", "Positions", "Settings"]
