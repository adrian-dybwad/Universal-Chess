"""The board's Lichess lobby is drawn in the device's language.

Why these tests exist
---------------------
Every other board menu is built from the localized catalog, but the Lichess
lobby builds its rows, its help, its splash copy and its error copy as English
literals in ``lobby.py`` and ``match.py``. A Spanish or French board therefore
opened Players in its own language and then showed an English lobby, while the
web -- reading the same catalog nodes -- was translated.

The rows and their help are asserted against the localized catalog rather than
against fixed Spanish text: the point is that the board reads the one source the
web reads, so the wording can be improved in ``menu.json`` and its overlays
without touching the board. The remaining copy has no catalog node and comes
from the board string bundle.
"""

import pytest

from universalchess import i18n
from universalchess.menus.catalog import loader
from universalchess.menus.catalog.loader import get_localized_catalog
from universalchess.players.lichess.lobby import (
    build_lichess_menu_entries,
    choose_lichess_reset_action,
)
from universalchess.players.lichess.match import (
    LichessSeek,
    lichess_cancelling_message,
    lichess_started_message,
    lichess_waiting_message,
)
from universalchess.players.lichess.player import LichessGameMode

SPANISH = "es"


@pytest.fixture
def spanish(spanish_board):
    """The board in Spanish (see the ``spanish_board`` fixture in conftest)."""
    return spanish_board


def _seek() -> LichessSeek:
    """A posted seek, which the waiting splash lists under its headline."""
    return LichessSeek(
        time_minutes=10,
        increment_seconds=5,
        rated=True,
        color="white",
        account_id="org:alice",
        host_id="org",
        rating_range="1000-1600",
    )


def _labels(entries) -> list:
    return [entry.label for entry in entries]


def test_lobby_rows_read_the_localized_catalog(spanish):
    """Account, Rated, Ongoing, Challenges and Seek New Game are in Spanish.

    Why: these rows were English literals, so the lobby stayed English on a
    translated board while the web card built from the same catalog nodes was
    translated. How the regression manifests: a row's label is the English
    board label rather than the Spanish one for its node.
    """
    catalog = get_localized_catalog(SPANISH)
    english = get_localized_catalog("en")
    entries = build_lichess_menu_entries("alice", rated=True)

    keys = [entry.key for entry in entries]
    assert keys == ["Account", "Rated", "Ongoing", "Challenges", "NewGame"]

    for key, node_id in (
        ("Ongoing", "lichess.ongoing"),
        ("Challenges", "lichess.challenges"),
        ("NewGame", "lichess.new_game"),
    ):
        entry = next(e for e in entries if e.key == key)
        expected = catalog.get_node(node_id)["boardLabel"]
        assert entry.label == expected
        # The node is genuinely translated, so reading the catalog is what put
        # Spanish on the row -- not an English board label that happens to match.
        assert expected != english.get_node(node_id)["boardLabel"]

    account = next(e for e in entries if e.key == "Account")
    assert account.label == f"{catalog.get_node('lichess.account')['boardLabel']}\nalice"

    rated = next(e for e in entries if e.key == "Rated")
    assert rated.label == (
        f"{catalog.get_node('field.lichess.rated')['boardLabel']}\n{i18n.t('common.on')}"
    )


def test_lobby_row_help_is_the_catalog_help_for_that_row(spanish):
    """Rated, Ongoing and Challenges explain themselves from the catalog.

    Why: the board kept its own copy of help text the catalog already carried,
    so the two drifted and only one of them was ever translated. How the
    regression manifests: the help on a row is not the localized catalog help
    for its node -- either English, or a second wording of the same thing.
    """
    catalog = get_localized_catalog(SPANISH)
    entries = {entry.key: entry for entry in build_lichess_menu_entries("alice")}

    for key, node_id in (
        ("Rated", "field.lichess.rated"),
        ("Ongoing", "lichess.ongoing"),
        ("Challenges", "lichess.challenges"),
    ):
        assert entries[key].help == catalog.get_node(node_id)["help"]
        assert entries[key].help != get_localized_catalog("en").get_node(node_id)["help"]


def test_rated_row_says_off_in_the_device_language(spanish):
    """The Rated row's state word follows the language too.

    Why: the state was the literal On/Off, so a Spanish row read half in each
    language once its label was translated. How the regression manifests: the
    label ends in "Off" instead of the bundle's word for it.
    """
    rated = next(e for e in build_lichess_menu_entries("alice", rated=False) if e.key == "Rated")
    assert rated.label.endswith(i18n.t("common.off"))
    assert i18n.t("common.off") != "Off"


class _ScriptedMenuManager:
    """Captures the entries a prompt shows, then answers with a fixed key."""

    def __init__(self, key):
        from universalchess.managers.menu import MenuSelection

        self.result = MenuSelection.from_key(key)
        self.shown = []

    def show_menu(self, entries, initial_index=0, on_index_change=None):
        self.shown.append(entries)
        return self.result


def test_board_reset_prompt_is_in_the_device_language(spanish):
    """The prompt after a board reset asks its question in Spanish.

    Why: it is built row by row in ``lobby.py``, so it was English on every
    board. How the regression manifests: a row still reads the English literal
    while the menu around it is Spanish.
    """
    manager = _ScriptedMenuManager("Cancel")
    choose_lichess_reset_action(manager)
    labels = _labels(manager.shown[0])

    assert labels[0] == i18n.t("lichess.reset.prompt")
    assert labels[1] == get_localized_catalog(SPANISH).get_node("players.lichess")["boardLabel"]
    assert labels[2] == get_localized_catalog(SPANISH).get_node("lichess.new_game")["boardLabel"]
    assert labels[3] == i18n.t("common.cancel")
    assert "Cancel" not in labels


def test_abort_prompt_is_in_the_device_language(spanish):
    """When the opponent aborts, the header must say so in Spanish.

    Why: abort reused the board-reset prompt, which only asked to seek.
    How the regression manifests: the header is still Seek a new game, or
    English Game aborted on a Spanish board.
    """
    manager = _ScriptedMenuManager("Cancel")
    choose_lichess_reset_action(manager, reason="ABORTED")
    labels = _labels(manager.shown[0])

    assert labels[0] == i18n.t("lichess.unfinished.aborted")
    assert i18n.t("lichess.reset.prompt") not in labels
    assert "Seek" not in labels[0]
    assert "Aborted" not in labels[0]


def test_resign_prompt_is_in_the_device_language(spanish):
    """When the opponent resigns, the header must say so in Spanish.

    Why: resign never opened this menu, so there was no translated header.
    How the regression manifests: the header is still Seek a new game, or
    English Opponent resigned on a Spanish board.
    """
    manager = _ScriptedMenuManager("Cancel")
    choose_lichess_reset_action(manager, reason="RESIGN")
    labels = _labels(manager.shown[0])

    assert labels[0] == i18n.t("lichess.unfinished.resign")
    assert i18n.t("lichess.reset.prompt") not in labels
    assert "Seek" not in labels[0]
    assert "Resign" not in labels[0]


def test_waiting_splash_lists_the_seek_in_the_device_language(spanish):
    """The seek the board is waiting on is described in Spanish.

    Why: the headline, the rated/casual word and the colour were English
    literals, so the screen a player stares at during a seek was the one screen
    that never translated. How the regression manifests: any of those three
    fall back to the English literal.
    """
    message = lichess_waiting_message(LichessGameMode.NEW, seek=_seek())
    lines = message.split("\n")

    assert lines[0] == i18n.t("lichess.waiting.seeking")
    assert lines[1] == f"10+5 {i18n.t('lichess.seek.rated')}"
    assert lines[2] == i18n.t("chess.color.white")
    assert "Waiting" not in message
    assert "rated" not in message


def test_the_other_lichess_splashes_are_in_the_device_language(spanish):
    """Connecting, waiting for an opponent, exiting and started all translate.

    Why: each is its own literal on a different path (join, outgoing challenge,
    BACK, accept), so one being translated says nothing about the others. How
    the regression manifests: the named path still shows English.
    """
    assert lichess_waiting_message(LichessGameMode.ONGOING) == i18n.t("lichess.waiting.connecting")
    assert lichess_waiting_message(
        LichessGameMode.CHALLENGE, awaiting_opponent=True
    ) == i18n.t("lichess.waiting.opponent")
    assert lichess_waiting_message(
        LichessGameMode.CHALLENGE, awaiting_opponent=False
    ) == i18n.t("lichess.waiting.challenge")
    assert lichess_cancelling_message() == i18n.t("lichess.waiting.exiting")
    assert lichess_started_message(True) == i18n.t(
        "lichess.started", color=i18n.t("chess.color.white")
    )
    assert "Exiting" not in lichess_cancelling_message()


def test_in_game_offers_are_in_the_device_language(spanish):
    """Accept/Decline for a challenge, a takeback and a draw all translate.

    Why: these interrupt a game in progress, so an English row appears over an
    otherwise Spanish board at the moment the player has to decide something.
    How the regression manifests: a row still reads Accept or Decline.
    """
    from universalchess.players.lichess.match import LichessChallengeOffer
    from universalchess.players.lichess.session import (
        LichessPlaySession,
        challenge_menu_entries,
    )

    offer = LichessChallengeOffer(
        challenge_id="abc",
        challenger_name="bob",
        challenger_rating=1500,
        our_color="black",
        rated=False,
        clock_label="10+5",
        variant_key="standard",
        variant_name="Standard",
    )
    rows = challenge_menu_entries(offer)
    assert rows[1].label == i18n.t("lichess.offer.accept_challenge")
    assert rows[2].label == i18n.t("lichess.offer.decline")
    # The terms line under them names the colour and the rated state as words.
    assert i18n.t("chess.color.black") in rows[0].label
    assert i18n.t("lichess.seek.casual") in rows[0].label
    assert "Accept" not in "".join(row.label for row in rows)

    session = LichessPlaySession.__new__(LichessPlaySession)
    session._beep = None
    session._started_splash_held = True
    shown = []

    class Menu:
        def show_menu(self, entries, **kwargs):
            shown.append([entry.label for entry in entries])

            class Result:
                key = "decline"

            return Result()

    session._menu_manager = Menu()
    session._on_takeback_offer(lambda: None, lambda: None)
    session._on_draw_offer(lambda: None, lambda: None)

    assert shown[0] == [
        i18n.t("lichess.offer.accept_takeback"),
        i18n.t("lichess.offer.decline"),
    ]
    assert shown[1] == [
        i18n.t("lichess.offer.accept_draw"),
        i18n.t("lichess.offer.decline"),
    ]


def test_english_still_reads_as_it_did_before(monkeypatch):
    """An English board keeps the wording the lobby shipped with.

    Why: routing these strings through the catalog and the bundle must not
    reword the English screens -- the bundle is authored from them. How the
    regression manifests: an English label or splash line comes back changed
    (or as a bare translation key, which is what a missing bundle entry
    returns).
    """
    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: "en"
    )
    i18n._active_locale = None
    i18n._bundles.clear()
    loader._active_locale = None
    i18n.refresh_active_language()
    loader.refresh_active_language()

    entries = build_lichess_menu_entries("alice", rated=True)
    assert _labels(entries) == [
        "Account\nalice",
        "Rated\nOn",
        "Ongoing\nGames",
        "Challenges",
        "Seek New\nGame",
    ]
    assert build_lichess_menu_entries(None)[0].label == "Account\nUnknown"
    assert lichess_waiting_message(LichessGameMode.NEW, seek=_seek()).startswith(
        "Waiting for game\n10+5 rated\nWhite"
    )
    assert lichess_cancelling_message() == "Exiting..."
    assert lichess_started_message(False) == "Game started\nYou play Black"
