"""Pure Lichess matchmaking helpers: host, pairing, seek, splash copy.

Seek parameters are derived from Players + Game settings so PLAY, lobby New
Game, and a board-reset to the start position cannot drift. The bound
credential's host (``org:alice`` / ``dev:bob``) selects the API server;
lichess.dev is never chosen by a game-level toggle.
"""

from __future__ import annotations

from dataclasses import dataclass
from universalchess.state.time_control import DelayMode, build_time_control
from .hosts import (
    ACCOUNT_TYPE_LICHESS,
    DEFAULT_HOST_ID,
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


class LichessSeekError(Exception):
    """Settings cannot produce a Lichess seek.

    ``code`` is ``pairing`` (not Human vs Lichess) or ``clock`` (not a simple
    Fischer/sudden-death control). ``message`` is e-paper-sized copy.
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
    host_id: str = DEFAULT_HOST_ID

    @property
    def account_type(self) -> str:
        """Store type for this plugin (always lichess)."""
        return ACCOUNT_TYPE_LICHESS

    @property
    def use_dev(self) -> bool:
        """True when the bound credential is on lichess.dev."""
        return self.host_id == HOST_DEV


def lichess_base_url(host_id: str = DEFAULT_HOST_ID) -> str:
    """berserk ``base_url`` for a Lichess host id (``org`` / ``dev``)."""
    return get_host(host_id).base_url


def create_berserk_client(token: str, host_id: str = DEFAULT_HOST_ID):
    """berserk Client pointed at the credential's host.

    ``base_url`` is required: the library defaults to lichess.org, which would
    send a lichess.dev token (or an org token in reverse) to the wrong server.
    """
    import berserk

    session = berserk.TokenSession(token)
    return berserk.Client(session=session, base_url=lichess_base_url(host_id))


def lichess_waiting_message(mode, seek=None) -> str:
    """Copy shown while seeking or joining, before the stream accepts.

    ``seek`` is the posted (or join) parameters. NEW lists clock, rated,
    color, host:user, and rating range so the wait screen matches the seek.
    Ongoing/challenge join fills a dummy 10+5 on ``LichessSeek``; that clock
    is not the remote game's and is omitted. Host:user is still shown.
    """
    from .player import LichessGameMode
    from .hosts import credential_label, get_host, parse_credential_id

    if mode == LichessGameMode.ONGOING:
        headline = "Connecting..."
        include_clock = False
    elif mode == LichessGameMode.CHALLENGE:
        headline = "Loading\nChallenge..."
        include_clock = False
    else:
        headline = "Waiting for game"
        include_clock = True

    if seek is None:
        return headline

    lines = [headline]
    if include_clock:
        rated = "rated" if seek.rated else "casual"
        lines.append(f"{seek.time_minutes}+{seek.increment_seconds} {rated}")
        lines.append(str(seek.color).capitalize())
    host_id, username = parse_credential_id(seek.account_id)
    if not username:
        host_id = seek.host_id
    if username:
        lines.append(credential_label(host_id, username))
    else:
        lines.append(get_host(host_id).label)
    if include_clock and seek.rating_range:
        lines.append(seek.rating_range)
    return "\n".join(lines)


def lichess_cancelling_message() -> str:
    """Copy while BACK tears down a seek that has not been accepted yet."""
    return "Exiting..."


def lichess_started_message(human_is_white: bool) -> str:
    """Copy after accept: the game exists and which side the human sits."""
    side = "White" if human_is_white else "Black"
    return f"Game started\nYou play {side}"


def _human_and_lichess(player1, player2):
    """Return (human, lichess) or raise pairing."""
    types = (player1.type, player2.type)
    if types == ("human", "lichess"):
        return player1, player2
    if types == ("lichess", "human"):
        return player2, player1
    raise LichessSeekError("pairing", "Need Human vs\nLichess")


def _seek_clock(game) -> tuple[int, int]:
    """Map Game time control to Lichess (minutes, increment).

    Lichess ``board.seek`` is a single Fischer/sudden-death pair. Delay,
    Bronstein, stages, time-odds, untimed, and non-whole-minute bases cannot be
    sent without lying about the local clock.
    """
    control = build_time_control(game)
    if (
        not control.is_timed
        or not control.is_symmetric
        or control.delay_mode is not DelayMode.NONE
        or control.delay_seconds
        or len(control.white_stages) != 1
    ):
        raise LichessSeekError("clock", "Lichess needs a\nsimple clock")
    stage = control.white_stages[0]
    if stage.base_seconds <= 0 or stage.base_seconds % 60 != 0:
        raise LichessSeekError("clock", "Lichess needs a\nsimple clock")
    return stage.base_seconds // 60, int(stage.increment_seconds)


def lichess_seek_from_settings(
    settings, rating_range: str = "", *, require_clock: bool = True
) -> LichessSeek:
    """Build a seek from AllSettings-like player/game objects plus account range.

    ``rating_range`` is the bound credential's range (empty = unrestricted).
    Engine ``elo`` is not consulted. Host comes from the Lichess slot's
    ``account`` id (``dev:bob`` → lichess.dev), not from Game settings.

    ``require_clock`` is True for a NEW seek (Lichess ``board.seek`` needs a
    simple Fischer pair). Ongoing/challenge join already has a remote clock, so
    a local delay/staged/untimed control must not block connecting.
    """
    _human, lichess = _human_and_lichess(settings.player1, settings.player2)
    if require_clock:
        minutes, increment = _seek_clock(settings.game)
    else:
        minutes, increment = 10, 5
    # Player 1's color is the color that slot plays. The human's color is the
    # seek color (the Lichess account is the local user).
    if settings.player1.type == "human":
        color = settings.player1.color
    else:
        color = "black" if settings.player1.color == "white" else "white"
    account_id = getattr(lichess, "account", "") or ""
    host_id, _username = parse_credential_id(account_id)
    return LichessSeek(
        time_minutes=minutes,
        increment_seconds=increment,
        color=color,
        rated=bool(getattr(settings.game, "lichess_rated", False)),
        rating_range=rating_range or "",
        account_id=account_id,
        host_id=host_id,
    )
