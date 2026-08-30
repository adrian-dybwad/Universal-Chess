"""The Lichess account belongs to the lobby, not to a player slot.

Why these tests exist
---------------------
The account was stored on whichever Players slot was set to Lichess, so with no
Lichess slot there was nowhere to put it: the board's lobby picker wrote to
neither slot and silently discarded the pick, the web disabled its selector, and
everything then authenticated as the first credential -- including the seek,
which went out on an account the user had not chosen. The binding now lives in
the game store as ``lichess_account``, which exists whatever the slots say.

An upgraded board must keep the account it was playing as, so a config that has
no ``lichess_account`` adopts the one its Lichess slot carried.

How a regression manifests
--------------------------
The stored account is ignored (games are seeked by the first credential), an
upgrade silently switches accounts, or the legacy slot key resurrects an account
the user has since replaced with Default.
"""

import pytest

import universalchess.players.settings as settings_mod
from universalchess.players.settings import AllSettings, GameSettings, PlayerSettings

LOBBY_ACCOUNT = "org:bob"
SLOT_ACCOUNT = "org:alice"


@pytest.fixture
def stored(monkeypatch):
    """Fake centaur.ini: section -> {key: value}, with key presence honoured.

    ``load_section`` must fall back to the caller's defaults for absent keys
    exactly as the real one does, because the migration turns on the difference
    between a key that is absent and one that is present but empty.
    """
    sections: dict[str, dict] = {}

    def fake_load_section(section, defaults):
        data = dict(defaults)
        data.update(
            {k: v for k, v in sections.get(section, {}).items() if k in defaults}
        )
        return data

    def fake_has_setting(section, key):
        return key in sections.get(section, {})

    def fake_save_setting(section, key, value):
        sections.setdefault(section, {})[key] = value
        return True

    monkeypatch.setattr(settings_mod, "load_section", fake_load_section)
    monkeypatch.setattr(settings_mod, "has_setting", fake_has_setting)
    monkeypatch.setattr(settings_mod, "save_setting", fake_save_setting)
    return sections


def _load(stored) -> AllSettings:
    return AllSettings.load("player1", "player2", "game")


def test_game_settings_round_trip_the_lobby_account():
    """The id must survive to_dict/load or the lobby cannot store a pick.

    How a regression manifests: to_dict drops the key (the web sends nothing to
    render or save) or load ignores the stored value, so every restart falls
    back to the default credential.
    """
    game = GameSettings(section="game", lichess_account=LOBBY_ACCOUNT)

    assert game.to_dict()["lichess_account"] == LOBBY_ACCOUNT
    assert GameSettings(section="game").to_dict()["lichess_account"] == ""


def test_a_config_upgraded_from_a_slot_binding_keeps_that_account(stored):
    """An upgrade must play as the account the board was already playing as.

    Why this test exists: the account moved out of the player slot. Without
    adoption, every board that had bound one would silently start seeking as
    whichever credential happens to sort first.

    How a regression manifests: lichess_account is empty after loading a config
    whose Lichess slot named an account.
    """
    stored["player2"] = {"type": "lichess", "account": SLOT_ACCOUNT}

    assert _load(stored).game.lichess_account == SLOT_ACCOUNT


def test_the_slot_the_lobby_authenticated_as_wins_the_adoption(stored):
    """Player 1's slot is adopted when both slots named an account.

    Why this test exists: the lobby resolved its account from player 1 first, so
    that is the account the board was actually playing as and the one an upgrade
    must preserve.

    How a regression manifests: player 2's account is adopted, so an upgraded
    board changes identity.
    """
    stored["player1"] = {"type": "lichess", "account": SLOT_ACCOUNT}
    stored["player2"] = {"type": "lichess", "account": "org:carol"}

    assert _load(stored).game.lichess_account == SLOT_ACCOUNT


@pytest.mark.parametrize("chosen", [LOBBY_ACCOUNT, ""])
def test_a_lobby_account_already_chosen_is_never_overwritten(stored, chosen):
    """A stored choice wins over the legacy slot key, including Default.

    Why this test exists: the legacy key is not deleted, so it is read on every
    load. Treating an empty stored value as "not chosen" would resurrect the old
    slot account every restart, undoing a user who had picked Default.

    How a regression manifests: lichess_account comes back as the slot's account
    after the user selected Default (the empty case), or a later pick is
    reverted to the pre-upgrade account (the non-empty case).
    """
    stored["player2"] = {"type": "lichess", "account": SLOT_ACCOUNT}
    stored["game"] = {"lichess_account": chosen}

    assert _load(stored).game.lichess_account == chosen


def test_an_account_left_on_a_slot_that_is_not_lichess_is_not_adopted(stored):
    """A stale binding on an engine slot must not become the lobby account.

    Why this test exists: the account field persisted whatever was last bound,
    even after the slot was switched to another type. Adopting that would make
    an upgrade start playing as an account the user had already moved away from.

    How a regression manifests: lichess_account picks up the leftover id from a
    slot whose type is engine or human.
    """
    stored["player1"] = {"type": "engine", "account": SLOT_ACCOUNT}
    stored["player2"] = {"type": "human", "account": "org:carol"}

    assert _load(stored).game.lichess_account == ""


def test_a_lichess_slot_is_rewritten_to_human_and_persisted(stored):
    """Leftover type=lichess must become human on load, and the write must stick.

    Why this test exists: Lichess is no longer a Type picker choice. A leftover
    slot blocks Positions and makes PLAY post a seek. The lobby substitutes the
    pairing at start without writing slots, so the saved type must not stay
    lichess. The rewrite is persisted so the next boot does not need the
    Lichess slot to re-adopt the lobby account (that account is persisted first).

    How a regression manifests: player2.type is still lichess after load, or
    the INI still says lichess so the next load sees it again.
    """
    stored["player2"] = {"type": "lichess", "account": SLOT_ACCOUNT}

    loaded = _load(stored)
    assert loaded.player2.type == "human"
    assert loaded.player1.type == "human"
    assert stored["player2"]["type"] == "human"
    assert loaded.game.lichess_account == SLOT_ACCOUNT
    assert stored["game"]["lichess_account"] == SLOT_ACCOUNT


def test_set_type_lichess_is_stored_as_human(stored):
    """A Type write of lichess (old web client, leftover POST) must not stick.

    Why this test exists: the picker no longer offers Lichess, but a stale
    client can still POST it. Persisting that would block Positions again.

    How a regression manifests: set('type', 'lichess') leaves type=lichess.
    """
    player = PlayerSettings(section="player1", type="human")
    player.set("type", "lichess")
    assert player.type == "human"
    assert stored["player1"]["type"] == "human"
