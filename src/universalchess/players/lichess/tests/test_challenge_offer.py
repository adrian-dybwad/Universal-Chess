"""A lobby seek is the board's terms; a challenge is the opponent's.

Why these tests exist
---------------------
Clicking the board account in the Lichess lobby sends a challenge whose clock,
rated flag, color, and variant are chosen by the challenger -- not by the seek
the board posted. Auto-accepting that started a game the Human had not agreed
to. The board must show those terms and wait for Accept or Decline.

How a regression manifests
--------------------------
lichess_challenge_offer drops clock/color/variant; the menu label hides the
terms; Accept is not a selectable key.
"""

from universalchess.players.lichess.match import (
    LichessChallengeOffer,
    lichess_challenge_offer,
    lichess_challenge_terms_label,
)
from universalchess.players.lichess.session import challenge_menu_entries


def _challenge(**overrides):
    payload = {
        "id": "ch-1",
        "challenger": {"id": "alice", "name": "Alice", "rating": 1842},
        "destUser": {"id": "boardaccount", "name": "BoardAccount"},
        "rated": True,
        "color": "white",
        "variant": {"key": "standard", "name": "Standard"},
        "timeControl": {"type": "clock", "limit": 300, "increment": 3, "show": "5+3"},
    }
    payload.update(overrides)
    return payload


def test_offer_reads_challenger_terms():
    """The offer is the challenger's clock, rated, color, and variant.

    Why: Lichess ``color`` is the color the challenger plays, so the board's
    color is the opposite. Failure: our_color is white when they asked for white.
    """
    offer = lichess_challenge_offer(_challenge())
    assert offer is not None
    assert offer.challenge_id == "ch-1"
    assert offer.challenger_name == "Alice"
    assert offer.challenger_rating == "1842"
    assert offer.clock_label == "5+3"
    assert offer.rated is True
    assert offer.our_color == "black"
    assert offer.variant_key == "standard"


def test_offer_random_color_and_casual_unlimited():
    """Unlimited casual random must not be formatted as a Fischer clock.

    How the regression manifests: clock_label is 0+0 or empty, or our_color
    is forced to white/black.
    """
    offer = lichess_challenge_offer(
        _challenge(
            rated=False,
            color="random",
            timeControl={"type": "unlimited"},
        )
    )
    assert offer is not None
    assert offer.rated is False
    assert offer.our_color == "random"
    assert offer.clock_label == "Unlimited"


def test_offer_correspondence_days_and_non_standard_variant():
    """Correspondence Chess960 must be visible so it can be declined.

    How the regression manifests: variant_key is standard or clock_label is
    empty, so the menu looks like a matching blitz seek.
    """
    offer = lichess_challenge_offer(
        _challenge(
            color="black",
            variant={"key": "chess960", "name": "Chess960"},
            timeControl={"type": "correspondence", "daysPerTurn": 2},
        )
    )
    assert offer is not None
    assert offer.our_color == "white"
    assert offer.variant_key == "chess960"
    assert offer.variant_name == "Chess960"
    assert offer.clock_label == "2 days"


def test_offer_none_without_id():
    """A challenge with no id cannot be accepted or declined.

    How the regression manifests: an empty-id offer is built and Accept POSTs
    ``/api/challenge/``.
    """
    assert lichess_challenge_offer({}) is None
    assert lichess_challenge_offer(_challenge(id="")) is None


def test_terms_label_shows_name_clock_rated_and_our_color():
    """The e-paper row must name who challenged and on what terms.

    How the regression manifests: the label is only 'Accept Challenge', so the
    Human cannot tell 5+3 rated Black from 3+0 casual White.
    """
    label = lichess_challenge_terms_label(
        LichessChallengeOffer(
            challenge_id="ch-1",
            challenger_name="Alice",
            challenger_rating="1842",
            clock_label="5+3",
            rated=True,
            our_color="black",
            variant_key="standard",
            variant_name="Standard",
        )
    )
    assert "Alice" in label
    assert "1842" in label
    assert "5+3" in label
    assert "rated" in label
    assert "Black" in label
    assert "Chess960" not in label


def test_terms_label_includes_non_standard_variant():
    """A variant other than standard must appear so it is not accepted by accident.

    How the regression manifests: Chess960 is omitted and the row looks like
    standard chess.
    """
    label = lichess_challenge_terms_label(
        LichessChallengeOffer(
            challenge_id="ch-1",
            challenger_name="Bob",
            challenger_rating="",
            clock_label="2 days",
            rated=False,
            our_color="random",
            variant_key="chess960",
            variant_name="Chess960",
        )
    )
    assert "Bob" in label
    assert "2 days" in label
    assert "casual" in label
    assert "Chess960" in label


def test_challenge_menu_has_accept_and_decline():
    """The dialog is Accept / Decline; the terms row is not selectable.

    How the regression manifests: only Accept exists, or the terms row is
    selectable and PLAY accepts without a real Accept key.
    """
    offer = lichess_challenge_offer(_challenge())
    entries = challenge_menu_entries(offer)
    keys = [e.key for e in entries]
    assert keys == ["terms", "accept", "decline"]
    by_key = {e.key: e for e in entries}
    assert by_key["terms"].selectable is False
    assert "Alice" in by_key["terms"].label
    assert by_key["accept"].selectable is True
    assert by_key["decline"].selectable is True
