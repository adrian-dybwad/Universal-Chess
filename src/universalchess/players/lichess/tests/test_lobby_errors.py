"""Lichess 403 Missing-scope errors are permission errors, not generic dumps.

Why these tests exist
---------------------
Listing challenges with ``board:play`` but without ``challenge:read`` is HTTP
403 ``Missing scope: challenge:read``, not 401. The panel previously showed a
truncated ``Challenges failed: HTTP 403...`` dump, which does not name the
scope to add. Catalog help also omitted ``challenge:read``, so a newly minted
token could still 403 on Challenges.
"""

from types import SimpleNamespace

from universalchess.players.lichess.lobby import (
    show_lichess_challenges,
    show_lichess_error,
    show_lichess_ongoing_games,
)


def _log():
    return SimpleNamespace(error=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None)


def _menu_with_panel(monkeypatch, shown):
    """A menu manager with a panel, capturing splash copy instead of a menu row.

    Production errors go through ``show_dismissible_splash``. Patching it here
    is the test double for that boundary: the message that would be on the
    splash is what the user reads.
    """
    monkeypatch.setattr(
        "universalchess.epaper.splash_screen.show_dismissible_splash",
        lambda manager, message, **kwargs: shown.append(message) or True,
    )
    return SimpleNamespace(_board=SimpleNamespace(display_manager=object()))


def test_challenges_403_missing_scope_names_challenge_read(monkeypatch):
    """A 403 Missing scope: challenge:read tells the user to add that scope.

    How a regression manifests: the panel shows ``Challenges failed: HTTP 403``
    (or a generic challenge-permissions line) instead of ``challenge:read``.
    """

    class FakeChallenges:
        def get_mine(self):
            raise Exception(
                "HTTP 403: Forbidden: {'error': 'Missing scope: challenge:read'}"
            )

    shown = []
    result = show_lichess_challenges(
        SimpleNamespace(challenges=FakeChallenges()),
        _menu_with_panel(monkeypatch, shown),
        _log(),
    )

    assert result is None
    assert shown == ["Token needs\nchallenge:read"]


def test_challenges_401_still_uses_permission_copy(monkeypatch):
    """A 401 without a Missing-scope body keeps the challenge-permissions line.

    How a regression manifests: 401 falls through to the truncated HTTP dump.
    """

    class FakeChallenges:
        def get_mine(self):
            raise Exception("HTTP 401: Unauthorized")

    shown = []
    result = show_lichess_challenges(
        SimpleNamespace(challenges=FakeChallenges()),
        _menu_with_panel(monkeypatch, shown),
        _log(),
    )

    assert result is None
    assert shown == ["Token does not have\nchallenge permissions"]


def test_ongoing_403_missing_scope_names_board_play(monkeypatch):
    """Ongoing games with a missing board:play scope name that scope.

    How a regression manifests: 403 dumps ``Games failed: HTTP 403`` instead of
    naming ``board:play``.
    """

    class FakeGames:
        def get_ongoing(self, count=10):
            raise Exception(
                "HTTP 403: Forbidden: {'error': 'Missing scope: board:play'}"
            )

    shown = []
    result = show_lichess_ongoing_games(
        SimpleNamespace(games=FakeGames()),
        _menu_with_panel(monkeypatch, shown),
        _log(),
    )

    assert result is None
    assert shown == ["Token needs\nboard:play"]


def test_lichess_error_uses_dismissible_splash_not_a_menu_row(monkeypatch):
    """show_lichess_error must overlay a splash, not a non-selectable menu row.

    How a regression manifests: show_menu is called with a selectable=False BACK
    entry, so the user cannot dismiss the error and the copy is a truncated row.
    """
    shown = []
    menus = []
    menu_manager = _menu_with_panel(monkeypatch, shown)
    menu_manager.show_menu = lambda entries, **kwargs: menus.append(entries)

    show_lichess_error(menu_manager, "Auth Error", "Token needs\nchallenge:read")

    assert shown == ["Token needs\nchallenge:read"]
    assert menus == []
