"""AI chess-coach service.

Turns a played move (position, move, evaluation swing) into a short natural
language coaching remark using a configurable AI agent. The remark is meant for
the board's 128x128 area, so it is constrained to one or two short sentences.

Design
------
- This module owns the *coaching-specific* content: it composes the system prompt
  (coach persona + fixed guardrails) and the user prompt (move, eval, verified
  facts). It does NOT know any provider details.
- Transport is delegated to an :class:`~universalchess.agents.base.Agent` resolved
  by id from :mod:`universalchess.agents.registry`. The agent builds the
  ``(url, headers, body)`` and parses the response, so every AI service (OpenAI,
  Anthropic, custom, or a user-provided module) is pluggable and unit-tested
  without network. ``CoachConfig.provider`` is the agent id.
- The HTTP call is injectable (``http_post``/``http_get``) so payloads and parsing
  are tested without any network access; it defaults to ``requests``.
- Failures (network error, non-2xx, empty/malformed body, unconfigured, or an
  :class:`~universalchess.agents.base.AgentError` from the agent) raise
  :class:`CoachError`; the caller renders a short fallback and does NOT persist, so
  a transient failure is retried on the next review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from universalchess.agents import registry as agents
from universalchess.agents.base import AgentConfig, AgentError

DEFAULT_TIMEOUT_SECONDS = 20

# Always-on guardrails composed into every system prompt regardless of the coach
# persona. These enforce the board's hard constraints (brevity for the 128x128
# area) and the product's honesty rule (no invented tactics); a coach persona
# shapes tone/focus but can never relax these.
_BASE_GUARDRAILS = (
    "Explain the move's idea or mistake in at most two short sentences. "
    "Do not restate the move in notation. Be specific and practical."
)

# Persona used when no coach persona is supplied (coach framework absent or a
# coach could not be resolved). Preserves the prior default coaching voice.
_DEFAULT_PERSONA = "You are a concise, encouraging chess coach."

# Language the coach writes in when none is specified. English is the model's
# native default, so a request for English adds no instruction (the prompt stays
# lean); any other language appends an explicit "write in this language" line.
DEFAULT_LANGUAGE = "English"

_MAX_TOKENS = 120


class CoachError(Exception):
    """Raised when a coach statement could not be produced.

    Carries the provider HTTP ``status`` and error ``code`` when the failure came
    from a non-2xx response (both None for a network/parse/config failure), so a
    caller can render a specific, actionable message -- quota exhausted vs. a
    rejected key vs. a transient rate limit -- instead of a generic "try later".
    """

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


# Human, surface-neutral messages per failure category, for the web and any caller
# that wants a full sentence. The board panel keeps its own compact, line-broken
# wording; only the *classification* (error_category) is shared, so board and web
# always agree on what went wrong even if the phrasing differs.
_CATEGORY_MESSAGES = {
    "quota": "The AI account is out of credit or quota. Add billing/credits for this agent.",
    "auth": "The AI key was rejected. Check the agent's API key in Settings > Agents.",
    "rate_limited": "The AI service is rate-limiting requests. Try again shortly.",
    "unavailable": "The AI service is unavailable. Try again later.",
}


def error_category(exc: "CoachError") -> str:
    """Classify a coach failure so callers can message it appropriately.

    Returns one of ``"quota"`` (account out of credit/quota -- a permanent problem
    retrying never fixes), ``"auth"`` (key rejected), ``"rate_limited"`` (transient),
    or ``"unavailable"`` (network/parse/unknown status). Reads the provider status
    and error code carried on the error; an out-of-credit OpenAI account is 429
    ``insufficient_quota``, distinct from a genuine 429 rate limit.
    """
    status = exc.status
    code = (exc.code or "").lower()
    if code == "insufficient_quota" or status == 402:
        return "quota"
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limited"
    return "unavailable"


def error_message(exc: "CoachError") -> str:
    """Return a full-sentence, user-facing reason for a coach failure."""
    return _CATEGORY_MESSAGES[error_category(exc)]


@dataclass
class CoachConfig:
    """Coach agent configuration, sourced from game settings.

    Attributes:
        provider: The agent id selecting which AI service to use (e.g. ``openai``),
            or ``none``/an unknown id when no agent is selected.
        api_key: The agent's API key.
        model: Model id; falls back to the agent default when empty.
        base_url: Base URL for agents that require one (custom OpenAI-compatible).
        enabled: Master coaching switch. False when the Coach selector is set to
            "Disabled" (coach id ``off``), which turns coaching off no matter how
            well the agent is configured. Defaults True so a config built purely to
            inspect an agent (e.g. listing its models on the Agents tab) is not
            gated by whether coaching happens to be on.
    """

    provider: str = "none"
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    enabled: bool = True

    def _agent_config(self) -> AgentConfig:
        """The connection config passed to the resolved agent."""
        return AgentConfig(api_key=self.api_key, model=self.model, base_url=self.base_url)

    def is_configured(self) -> bool:
        """True when coaching is enabled, an agent is selected, and it is set up.

        Gates every network call: coaching turned off (Coach = "Disabled"), a
        disabled/unknown provider, or a selected agent missing its key (or base URL
        when required), all read as not configured so the caller shows the setup
        hint instead of attempting a request that would 401.
        """
        if not self.enabled:
            return False
        agent = agents.get_agent(self.provider)
        if agent is None:
            return False
        return agent.is_configured(self._agent_config())

    def resolved_model(self) -> str:
        """The model id to use, applying the agent default when unset.

        Returns an empty string when no agent is selected, since there is no
        default to apply.
        """
        agent = agents.get_agent(self.provider)
        if agent is None:
            return ""
        return agent.resolved_model(self._agent_config())


@dataclass
class CoachRequest:
    """Everything the coach needs to describe a single played move.

    Attributes:
        fen_before: FEN of the position before the move was played.
        move_text: The move already formatted in the user's chosen notation
            (SAN, LAN, UCI, or figurine). The coach is instructed to refer to the
            move using exactly this string, so its remark matches the notation the
            user sees everywhere else. Not necessarily SAN -- hence the neutral name.
        side_to_move: ``"white"`` or ``"black"`` -- the side that moved.
        eval_before_cp: Eval in centipawns (white's perspective) before the move.
        eval_after_cp: Eval in centipawns (white's perspective) after the move.
        move_number: Full-move number, for context in the prompt.
        facts: Verified, engine-independent facts about the move (captures, checks,
            real targets, absolute pins) derived from the position. The model is
            instructed to base tactical claims only on these, so it stops inventing
            tactics (e.g. a non-existent pin) that the position does not support.
        is_potential_move: True when the move is a suggestion the player is
            considering (a hint/tip) rather than a move that was actually played.
            The prompt is framed accordingly so the coach explains why the move
            would be good instead of critiquing it as an executed move.
        is_opponent_move: True when the played move was made by the opponent rather
            than the human player. The prompt is framed so the coach explains what
            the opponent is doing/threatening and addresses the player about it,
            instead of critiquing it as though the player had played it (which
            produced remarks like "By playing d6, you solidify..." for an opponent's
            move). Ignored for a potential move (a hint is always the player's).
        persona: Optional coach persona (tone/focus/instructions) for the system
            prompt, supplied by the selected coach. When unset, the default
            coaching voice is used. The fixed guardrails are always appended.
        language: Natural language the coach must write its remark in (e.g.
            ``"Spanish"``). Defaults to English, which adds no instruction (the
            model's native default); any other value appends an explicit
            "write in this language" line to the system prompt.
        candidate_lines: Engine-verified candidate moves for the position before
            the move (best first), each pre-formatted like ``"e4 (+0.30)"`` in the
            user's notation. Sourced from a MultiPV analysis (coach_multipv), they
            let the coach reference the engine's preferred/alternative moves. Empty
            when MultiPV is disabled or the analysis was unavailable, in which case
            no alternatives block is added to the prompt. Being engine output,
            these are authoritative like ``facts`` for referring to better moves.
        chess960: True when the move belongs to a Chess960 (Fischer Random) game.
            Carried on the request so downstream enrichment (e.g. MultiPV candidate
            analysis) rebuilds the board chess960-aware; 960 castling is a
            king-onto-rook move that is illegal on a standard board. This field adds
            no chess dependency to the service layer -- it is a plain flag.
    """

    fen_before: str
    move_text: str
    side_to_move: str
    eval_before_cp: Optional[int] = None
    eval_after_cp: Optional[int] = None
    move_number: Optional[int] = None
    facts: Tuple[str, ...] = ()
    is_potential_move: bool = False
    is_opponent_move: bool = False
    persona: Optional[str] = None
    language: str = DEFAULT_LANGUAGE
    candidate_lines: Tuple[str, ...] = ()
    chess960: bool = False


def _language_instruction(language: Optional[str]) -> str:
    """Return a "respond in this language" line, or "" for English/unset.

    English is the model default so it needs no instruction; any other language
    gets an explicit directive strong enough to override the English-heavy prompt
    (all other lines are in English) so the remark comes back in the chosen
    language.
    """
    name = (language or "").strip()
    if not name or name.casefold() == DEFAULT_LANGUAGE.casefold():
        return ""
    return (
        f"Write your entire response in {name}. "
        f"Every word must be in {name}, regardless of the language of this prompt."
    )


def build_system_prompt(request: "CoachRequest") -> str:
    """Compose the system prompt: persona, guardrails, then a language directive.

    The persona (who the coach is and how they coach) comes from the selected
    coach via ``request.persona``; when unset the default coaching voice is used.
    The fixed guardrails are always appended so brevity and the no-invented-tactics
    rule hold for every coach. When a non-English language is requested, an
    explicit directive is appended last so it is the final, strongest instruction.
    """
    persona = (request.persona or "").strip() or _DEFAULT_PERSONA
    prompt = f"{persona}\n\n{_BASE_GUARDRAILS}"
    language_line = _language_instruction(request.language)
    if language_line:
        prompt = f"{prompt}\n\n{language_line}"
    return prompt


def _format_eval(cp: Optional[int]) -> str:
    """Human-readable eval from centipawns (white's perspective), or 'unknown'."""
    if cp is None:
        return "unknown"
    return f"{cp / 100.0:+.2f}"


def build_user_prompt(request: CoachRequest) -> str:
    """Build the user prompt describing the move and its evaluation swing.

    The final instruction is framed by whose move it is, because the coach speaks
    to the human player:

    - Potential move (``is_potential_move`` -- a hint/tip): states the move is not
      yet played and asks why it would be a good choice, so the coach never
      critiques it as if it were already on the board.
    - Opponent's move (``is_opponent_move``): tells the coach the opponent, not the
      player, made the move and to explain what the opponent is doing/threatening.
      Without this the coach addressed the player as the mover ("By playing d6, you
      solidify...") for a move the opponent played.
    - Player's own played move (default): asks the coach to explain the player's
      move.
    """
    if request.is_potential_move:
        move_label = "Candidate move being considered (a hint, NOT yet played)"
    elif request.is_opponent_move:
        move_label = "Move played by the opponent"
    else:
        move_label = "Move played"
    lines = [
        f"Position (FEN): {request.fen_before}",
        f"Side to move: {request.side_to_move}",
        f"{move_label}: {request.move_text}",
        f"Engine eval before (white's perspective, pawns): {_format_eval(request.eval_before_cp)}",
        f"Engine eval after (white's perspective, pawns): {_format_eval(request.eval_after_cp)}",
    ]
    if request.move_number is not None:
        lines.insert(0, f"Move number: {request.move_number}")
    if request.facts:
        lines.append("Verified facts about the move (authoritative, from the board):")
        lines.extend(f"- {fact}" for fact in request.facts)
    if request.candidate_lines:
        lines.append(
            "Engine's top candidate moves in this position (best first, "
            "authoritative -- use these to reference better or alternative moves):"
        )
        lines.extend(f"- {line}" for line in request.candidate_lines)
    tactical_guard = (
        "Base every tactical claim (check, capture, pin, fork, threat) only on the "
        "verified facts above and the given position; do not assert a pin, fork, "
        "check, or capture that is not supported. "
        f'When you refer to the move, write it exactly as "{request.move_text}".'
    )
    if request.is_potential_move:
        lines.append(
            "This move is a suggestion the player is considering and has NOT been "
            "played yet. In at most two short sentences, explain why it is a good "
            "move to play -- its plan or the tactic it would achieve. " + tactical_guard
        )
    elif request.is_opponent_move:
        lines.append(
            "The opponent just played this move, not the player. Speaking to the "
            "player, explain in at most two short sentences what the opponent is "
            "doing or threatening. Do not phrase it as if the player made this move; "
            'never say "you played" about the opponent\'s move. ' + tactical_guard
        )
    else:
        lines.append(
            "The player just played this move. Coach the player on their own move in "
            "at most two short sentences. " + tactical_guard
        )
    return "\n".join(lines)


def fallback_models(provider: str) -> List[str]:
    """Return the curated fallback model ids for an agent (may be empty).

    Used when the live model list cannot be fetched. An unknown/disabled provider,
    or an agent with no curated list (e.g. custom), returns an empty list.
    """
    agent = agents.get_agent(provider)
    if agent is None:
        return []
    return list(agent.fallback_models)


def _error_info(response) -> Tuple[Optional[str], str]:
    """Best-effort (code, detail) from a non-2xx response, for diagnostics.

    A bare status code (e.g. 429) hides *why* the provider refused: an unfunded
    OpenAI account returns 429 ``insufficient_quota`` while a real rate limit
    returns 429 ``rate_limit_exceeded`` -- callers must tell those apart to show an
    actionable message. Returns the provider's structured error ``code``/``type``
    (or None) and a human ``detail`` string (``code: message`` when present, else a
    truncated raw body). Never raises; ``detail`` is "" when nothing usable is
    present. An error body carries no credentials, so surfacing it is safe.
    """
    try:
        data = response.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            raw_code = err.get("code") or err.get("type")
            code = str(raw_code) if raw_code else None
            parts = [str(err[k]) for k in ("code", "message") if err.get(k)]
            if parts:
                return code, ": ".join(parts)
            return code, ""
        if isinstance(err, str) and err:
            return None, err
    text = (getattr(response, "text", "") or "").strip().replace("\n", " ")
    return None, text[:200]


def list_models(
    config: CoachConfig,
    *,
    http_get: Optional[Callable] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> List[str]:
    """Fetch the available model ids for the configured agent.

    Queries the agent's list-models endpoint with the configured key so the model
    dropdown always reflects models the account can actually use. Returns the
    chat-usable ids (sorted). Raises :class:`CoachError` if not configured, if the
    agent cannot list models, or on any network/HTTP/parse failure -- the caller
    falls back to the curated list so a transient failure does not empty the
    dropdown.
    """
    if not config.is_configured():
        raise CoachError("Coach service is not configured")

    agent = agents.get_agent(config.provider)
    if agent is None or not agent.supports_model_listing():
        raise CoachError("Agent does not support model listing")

    try:
        url, headers = agent.build_models_request(config._agent_config())
    except AgentError as exc:
        raise CoachError(str(exc)) from exc

    if http_get is None:
        import requests

        http_get = requests.get

    try:
        response = http_get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # network layer failure
        raise CoachError("Model list request failed") from exc

    status = getattr(response, "status_code", None)
    if status is None or not (200 <= status < 300):
        code, detail = _error_info(response)
        raise CoachError(
            f"Model list returned status {status}" + (f" ({detail})" if detail else ""),
            status=status,
            code=code,
        )

    try:
        data = response.json()
    except Exception as exc:
        raise CoachError("Model list response was not valid JSON") from exc

    try:
        return agent.filter_models(agent.parse_models_response(data))
    except AgentError as exc:
        raise CoachError(str(exc)) from exc


def generate_coach_statement(
    config: CoachConfig,
    request: CoachRequest,
    *,
    http_post: Optional[Callable] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Produce a coach statement for a move via the configured agent.

    Composes the coaching prompts here and delegates the request/parse to the
    resolved agent, so this function is provider-agnostic.

    Args:
        config: Agent configuration (must be configured).
        request: The move to coach.
        http_post: Callable used to POST (defaults to ``requests.post``);
            injectable for tests.
        timeout: Request timeout in seconds.

    Returns:
        The coach statement text.

    Raises:
        CoachError: If not configured, on any network/HTTP failure, on an
            empty/malformed response, or when the agent reports an error.
    """
    if not config.is_configured():
        raise CoachError("Coach service is not configured")

    agent = agents.get_agent(config.provider)
    if agent is None:
        raise CoachError("Coach service is not configured")

    system_prompt = build_system_prompt(request)
    user_prompt = build_user_prompt(request)
    try:
        url, headers, body = agent.build_chat_request(
            config._agent_config(), system_prompt, user_prompt, _MAX_TOKENS
        )
    except AgentError as exc:
        raise CoachError(str(exc)) from exc

    if http_post is None:
        import requests

        http_post = requests.post

    try:
        response = http_post(url, headers=headers, json=body, timeout=timeout)
    except Exception as exc:  # network layer failure
        raise CoachError("Coach request failed") from exc

    status = getattr(response, "status_code", None)
    if status is None or not (200 <= status < 300):
        code, detail = _error_info(response)
        raise CoachError(
            f"Coach request returned status {status}" + (f" ({detail})" if detail else ""),
            status=status,
            code=code,
        )

    try:
        data = response.json()
    except Exception as exc:
        raise CoachError("Coach response was not valid JSON") from exc

    try:
        return agent.parse_chat_response(data)
    except AgentError as exc:
        raise CoachError(str(exc)) from exc


__all__ = [
    "CoachConfig",
    "CoachRequest",
    "CoachError",
    "DEFAULT_LANGUAGE",
    "error_category",
    "error_message",
    "generate_coach_statement",
    "build_user_prompt",
    "build_system_prompt",
    "list_models",
    "fallback_models",
]
