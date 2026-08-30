"""Pure Lichess matchmaking helpers: host, pairing, seek, splash copy.

Seek parameters are derived from Players settings and the lobby's Rated,
Clock, and Color so PLAY, lobby New Game, and a board-reset to the start
position cannot drift. The Game clock is for local games and is not posted.
Player 1 Color is the physical setup at the e-paper end (and whether the
panel is rotated); it is not the seek colour. The bound
credential's host (``org:alice`` / ``dev:bob``) selects the API server;
lichess.dev is never chosen by a game-level toggle.
"""

from __future__ import annotations

from dataclasses import dataclass
from universalchess.i18n import t
from .hosts import (
    ACCOUNT_TYPE_LICHESS,
    HOST_BY_ID,
    HOST_DEV,
    get_host,
    parse_credential_id,
)

# Re-exported so existing imports keep working.
LICHESS_ORG_BASE_URL = get_host("org").base_url
LICHESS_DEV_BASE_URL = get_host("dev").base_url

# Cap the "Game started / You play …" splash so a player who does not move still
# sees the board. First move dismisses it earlier.
START_PLAYING_SPLASH_SECONDS = 5.0


# Board API Rapid/Classical clocks plus correspondence. Keys match the Game
# presets that qualify; ``none`` is correspondence (the API has no unlimited
# seek). Blitz/Bullet are omitted: Lichess rejects them with Invalid time control.
DEFAULT_LICHESS_CLOCK = "rapid_10_0"
DEFAULT_LICHESS_COLOR = "random"
LICHESS_COLORS = ("random", "white", "black")
LICHESS_CORRESPONDENCE_DAYS = 2
LICHESS_CLOCKS = {
    "none": None,
    "rapid_10_0": (10, 0),
    "rapid_10_5": (10, 5),
    "rapid_15_10": (15, 10),
    "classical_30_0": (30, 0),
    "classical_30_20": (30, 20),
    "classical_60_30": (60, 30),
    "classical_90_30": (90, 30),
}


class LichessSeekError(Exception):
    """Settings cannot produce a Lichess seek.

    ``code`` is ``pairing`` (not Human vs Lichess) or ``clock`` (not a Board API
    Rapid, Classical, or correspondence clock). ``message`` is e-paper-sized copy.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LichessSeek:
    """Parameters for ``board.seek`` plus which credential/host to authenticate as."""

    time_minutes: int
    increment_seconds: int
    color: str
    rated: bool
    rating_range: str
    account_id: str
    host_id: str = ""
    days: int = 0

    @property
    def account_type(self) -> str:
        """Store type for this plugin (always lichess)."""
        return ACCOUNT_TYPE_LICHESS

    @property
    def use_dev(self) -> bool:
        """True when the bound credential is on lichess.dev."""
        return self.host_id == HOST_DEV


@dataclass(frozen=True)
class LichessChallengeOffer:
    """Incoming challenge terms, for Accept/Decline on the board.

    A seek is the board's clock, rated flag, and color. This is the
    challenger's. Empty ``challenge_id`` is not used; :func:`lichess_challenge_offer`
    returns None instead.
    """

    challenge_id: str
    challenger_name: str
    challenger_rating: str
    clock_label: str
    rated: bool
    our_color: str
    variant_key: str
    variant_name: str


def lichess_challenge_offer(challenge: dict):
    """Parse a Board API ``challenge`` object. None if it cannot be accepted."""
    if not challenge:
        return None
    challenge_id = str(challenge.get("id") or "")
    if not challenge_id:
        return None
    challenger = challenge.get("challenger") or {}
    name = str(challenger.get("name") or challenger.get("id") or "Unknown")
    rating = challenger.get("rating")
    rating_str = "" if rating in (None, "") else str(rating)
    variant = challenge.get("variant") or {}
    variant_key = str(variant.get("key") or "standard").lower()
    variant_name = str(variant.get("name") or variant_key)
    color = str(challenge.get("color") or "random").lower()
    if color == "white":
        our_color = "black"
    elif color == "black":
        our_color = "white"
    else:
        our_color = "random"
    return LichessChallengeOffer(
        challenge_id=challenge_id,
        challenger_name=name,
        challenger_rating=rating_str,
        clock_label=_challenge_clock_label(challenge.get("timeControl") or {}),
        rated=bool(challenge.get("rated")),
        our_color=our_color,
        variant_key=variant_key,
        variant_name=variant_name,
    )


def _challenge_clock_label(time_control: dict) -> str:
    """E-paper clock copy from a Lichess ``timeControl`` object."""
    shown = time_control.get("show")
    if shown:
        return str(shown)
    ttype = str(time_control.get("type") or "")
    if ttype == "unlimited":
        return "Unlimited"
    if ttype == "correspondence":
        days = time_control.get("daysPerTurn")
        if days is None:
            return "Correspondence"
        try:
            n = int(days)
        except (TypeError, ValueError):
            return "Correspondence"
        if n == 1:
            return "1 day"
        return f"{n} days"
    limit = time_control.get("limit")
    if limit is None:
        return ""
    try:
        seconds = int(limit)
        increment = int(time_control.get("increment") or 0)
    except (TypeError, ValueError):
        return ""
    minutes, rem = divmod(seconds, 60)
    if rem:
        return f"{seconds}s+{increment}"
    return f"{minutes}+{increment}"


def lichess_challenge_terms_label(offer: LichessChallengeOffer) -> str:
    """Two-line e-paper summary of who challenged and on what terms."""
    rated = t("lichess.seek.rated") if offer.rated else t("lichess.seek.casual")
    color = color_label(offer.our_color)
    line1 = offer.challenger_name or t("common.unknown")
    if offer.challenger_rating:
        line1 = f"{line1} {offer.challenger_rating}"
    bits = [offer.clock_label, rated, color]
    if offer.variant_key and offer.variant_key != "standard":
        bits.append(offer.variant_name or offer.variant_key)
    line2 = " ".join(part for part in bits if part)
    return f"{line1}\n{line2}"


def lichess_base_url(host_id: str) -> str:
    """berserk ``base_url`` for a Lichess host id (``org`` / ``dev``)."""
    return get_host(host_id).base_url


def create_lichess_connection(token: str, host_id: str):
    """berserk client for the credential's host, paired with its session.

    ``host_id`` is required: the library defaults to lichess.org, which would
    send a lichess.dev token (or an org token in reverse) to the wrong server.

    The session can abort its own streams, and is returned alongside the client
    because berserk keeps it out of reach on a private requestor. Without both,
    nothing can close the ``board.seek`` connection Lichess keeps a lobby seek
    alive for (see :mod:`~universalchess.players.lichess.http_session`).
    """
    import berserk

    from .http_session import LichessConnection, abortable_token_session_class

    session = abortable_token_session_class()(token)
    client = berserk.Client(session=session, base_url=lichess_base_url(host_id))
    return LichessConnection(client=client, session=session)


def lichess_waiting_message(mode, seek=None, *, awaiting_opponent: bool = False) -> str:
    """Copy shown while seeking or joining, before the stream accepts.

    ``seek`` is the posted (or join) parameters. NEW lists clock, rated,
    color, host:user, and rating range so the wait screen matches the seek.
    Ongoing/challenge join fills a dummy 10+5 on ``LichessSeek``; that clock
    is not the remote game's and is omitted. Host:user is still shown.

    ``awaiting_opponent`` is a challenge the board sent: there is no game to
    load until the other player accepts, and that wait is open-ended, so it
    must not read as a join already under way.
    """
    from .player import LichessGameMode
    from .hosts import credential_label, get_host, parse_credential_id

    if mode == LichessGameMode.ONGOING or mode == LichessGameMode.ATTACH:
        headline = t("lichess.waiting.connecting")
        include_clock = False
    elif mode == LichessGameMode.CHALLENGE:
        headline = (
            t("lichess.waiting.opponent")
            if awaiting_opponent
            else t("lichess.waiting.challenge")
        )
        include_clock = False
    else:
        headline = t("lichess.waiting.seeking")
        include_clock = True

    if seek is None:
        return headline

    lines = [headline]
    if include_clock:
        rated = t("lichess.seek.rated") if seek.rated else t("lichess.seek.casual")
        if seek.days:
            clock = t("lichess.seek.correspondence")
        else:
            clock = f"{seek.time_minutes}+{seek.increment_seconds}"
        lines.append(f"{clock} {rated}")
        lines.append(color_label(seek.color))
    host_id, username = parse_credential_id(seek.account_id)
    if not username:
        host_id = seek.host_id
    if username and host_id in HOST_BY_ID:
        lines.append(credential_label(host_id, username))
    elif host_id in HOST_BY_ID:
        lines.append(get_host(host_id).label)
    if include_clock and seek.rating_range:
        lines.append(seek.rating_range)
    return "\n".join(lines)


def color_label(color) -> str:
    """The side's name in the device's language, for a seek or a game screen.

    Anything that is not white or black is the random seek, which asks Lichess
    to choose. An unknown value falls through to that rather than printing a
    raw setting value at the player.
    """
    normalized = str(color).strip().lower()
    if normalized == "white":
        return t("chess.color.white")
    if normalized == "black":
        return t("chess.color.black")
    return t("lichess.color.random")


def lichess_cancelling_message() -> str:
    """Copy while BACK tears down a seek that has not been accepted yet."""
    return t("lichess.waiting.exiting")


def lichess_started_message(human_is_white: bool) -> str:
    """Copy after accept: the game exists and which side the human sits."""
    return t(
        "lichess.started",
        color=t("chess.color.white") if human_is_white else t("chess.color.black"),
    )


def _human_and_lichess(player1, player2):
    """Return (human, lichess) or raise pairing."""
    types = (player1.type, player2.type)
    if types == ("human", "lichess"):
        return player1, player2
    if types == ("lichess", "human"):
        return player2, player1
    raise LichessSeekError("pairing", t("lichess.error.pairing"))


def lichess_account_id(settings) -> str:
    """The saved Lichess credential the board plays and browses as.

    Chosen in the lobby's Account row and stored in the game settings rather
    than on a player slot: a lobby Seek New Game runs with a pairing derived for
    that game, which no saved slot describes, so a per-slot binding had nowhere
    to live and the pick was discarded. Empty means the default credential.
    """
    return getattr(settings.game, "lichess_account", "") or ""


def has_lichess_slot(player1, player2) -> bool:
    """Whether either Players slot is configured to play on Lichess.

    Says nothing about which account plays: that is the lobby's, and one board
    plays as one account whichever slot the remote player occupies.
    """
    return any(getattr(player, "type", "") == "lichess" for player in (player1, player2))


def _lobby_seek_color(game) -> str:
    """Map the lobby Color setting to the Board API ``color`` field.

    The seeking account is the human after remap, so White on the row posts
    white. Player 1 Color is the physical setup at the e-paper end and is not
    posted. Empty (a config that predates the setting) is random. Unknown keys
    fall through to random rather than posting a value Lichess will reject.
    """
    key = str(getattr(game, "lichess_color", "") or "").strip().lower()
    if key in LICHESS_COLORS:
        return key
    return DEFAULT_LICHESS_COLOR


def player1_is_white(player1_color: str) -> bool:
    """Whether player 1's physical side of the board is White.

    The Players color control names which colour is set up at the e-paper
    end. Local and Lichess games both honour it: a Lichess pairing used to
    force White here and only rotate the panel later, which left the pieces
    on the wrong side of the board.
    """
    return (player1_color or "white").strip().lower() != "black"


def epaper_is_flipped(player1_color: str) -> bool:
    """Whether the e-paper draws the board from Black's side.

    Player 1 Color is which colour sits at the e-paper end. Black there turns
    the panel around so the seated player can read it. Lichess assigning a
    colour remaps who plays which pieces; it does not restack them or turn
    the display.
    """
    return not player1_is_white(player1_color)


def board_seek_form(seek: LichessSeek) -> dict:
    """Form body for ``POST /api/board/seek``.

    Real-time sends ``time`` and ``increment``. Correspondence sends ``days`` and
    omits the clock pair: ``0+0`` is Blitz and Lichess rejects it as invalid.
    """
    data = {
        "rated": "true" if seek.rated else "false",
        "variant": "standard",
        "color": seek.color or "random",
        "ratingRange": seek.rating_range or "",
    }
    if seek.days:
        data["days"] = int(seek.days)
        return data
    data["time"] = int(seek.time_minutes)
    data["increment"] = int(seek.increment_seconds)
    return data


def _lobby_seek_clock(game) -> tuple[int, int, int]:
    """Map the lobby clock setting to ``(minutes, increment, days)``.

    The Game clock is for local games and is not consulted. Empty (a config
    that predates the setting) is Rapid 10+0, which the Board API accepts.
    ``days`` is non-zero only for correspondence (``none``).
    """
    key = str(getattr(game, "lichess_clock", "") or "").strip()
    if not key:
        key = DEFAULT_LICHESS_CLOCK
    if key not in LICHESS_CLOCKS:
        raise LichessSeekError("clock", t("lichess.error.clock"))
    clock = LICHESS_CLOCKS[key]
    if clock is None:
        return 0, 0, LICHESS_CORRESPONDENCE_DAYS
    minutes, increment = clock
    return minutes, increment, 0


def lichess_seek_from_settings(
    settings,
    rating_range: str = "",
    *,
    require_clock: bool = True,
    lobby_seek: bool = False,
) -> LichessSeek:
    """Build a seek from AllSettings-like player/game objects plus account range.

    ``rating_range`` is the chosen credential's range (empty = unrestricted).
    Engine ``elo`` is not consulted. The account is the lobby's
    (``game.lichess_account``) and the host follows from its id (``dev:bob`` →
    lichess.dev), not from a player slot or a Game toggle.

    ``require_clock`` is True for a NEW seek. Ongoing/challenge join already
    has a remote clock, so a missing lobby clock must not block connecting.

    ``lobby_seek`` marks a start the Lichess lobby asked for outright. Those
    buttons seek whatever the Players slots say, so the Human vs Lichess pairing
    the other paths require is not demanded of them; the caller runs the game
    with the pairing ``effective_lichess_players`` derives. Color is the lobby
    Color row, so a lobby seek over a pairing that names no Lichess slot still
    posts White, Black, or Random.
    """
    if not lobby_seek:
        # Validation only: raises unless the slots are exactly Human vs Lichess.
        # The account no longer comes from the slot it returns.
        _human_and_lichess(settings.player1, settings.player2)
    if require_clock:
        minutes, increment, days = _lobby_seek_clock(settings.game)
    else:
        minutes, increment, days = 10, 5, 0
    color = _lobby_seek_color(settings.game)
    account_id = lichess_account_id(settings)
    from .accounts import default_lichess_credential, get_lichess_credential, host_id_of

    if account_id:
        account = get_lichess_credential(account_id)
    else:
        account = default_lichess_credential()
    if account is not None:
        host_id = host_id_of(account)
    else:
        host_id, _username = parse_credential_id(account_id)
    return LichessSeek(
        time_minutes=minutes,
        increment_seconds=increment,
        color=color,
        rated=bool(getattr(settings.game, "lichess_rated", False)),
        rating_range=rating_range or "",
        account_id=account_id,
        host_id=host_id,
        days=days,
    )
