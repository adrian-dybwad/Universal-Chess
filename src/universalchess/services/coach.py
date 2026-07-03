"""AI chess-coach service.

Turns a played move (position, move, evaluation swing) into a short natural
language coaching remark using a configurable AI provider. The remark is meant
for the board's 128x128 area, so it is constrained to one or two short
sentences.

Design
------
- Pure request-building and response-parsing helpers are separated from the one
  networked entry point (:func:`generate_coach_statement`) so provider payloads
  and parsing are unit-tested without any network access.
- The HTTP call is injectable (``http_post``) for the same reason; it defaults
  to ``requests.post``.
- Providers:
  - ``openai``: OpenAI Chat Completions at ``https://api.openai.com/v1``.
  - ``custom``: any OpenAI-compatible Chat Completions endpoint, at the
    configured ``base_url``.
  - ``anthropic``: Anthropic Messages API.
- Failures (network error, non-2xx, empty/*malformed* body) raise
  :class:`CoachError`; the caller renders a short fallback and does NOT persist,
  so a transient failure is retried on the next review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

DEFAULT_TIMEOUT_SECONDS = 20

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
# Fast, low-cost, current model. The previous default (claude-3-5-haiku-latest)
# was retired by Anthropic and now returns 404, which surfaced as "Coach
# unavailable"; keep this pointing at a live model id.
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5"

# Providers that produce a coach statement. "none" (or anything else) means the
# feature is not configured.
CONFIGURED_PROVIDERS = ("openai", "anthropic", "custom")

# Curated fallback model ids per provider, used only when the live model list
# cannot be fetched (offline, key not yet valid, endpoint down). The live list
# from the provider's models endpoint is preferred; these keep the dropdown
# usable rather than empty. Ordered best-first for display.
OPENAI_FALLBACK_MODELS = (
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4.1",
    "gpt-5-mini",
    "gpt-5",
)
ANTHROPIC_FALLBACK_MODELS = (
    "claude-haiku-4-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
)

# Substrings that mark an OpenAI/custom model id as non-chat (audio, images,
# embeddings, etc.). Used to keep the model dropdown focused on models usable for
# a text coaching completion. Not applied to Anthropic, whose listed models are
# all chat models.
_NON_CHAT_MODEL_KEYWORDS = (
    "embedding",
    "embed",
    "whisper",
    "tts",
    "audio",
    "realtime",
    "transcribe",
    "search",
    "moderation",
    "image",
    "dall-e",
    "dalle",
)

_SYSTEM_PROMPT = (
    "You are a concise, encouraging chess coach. Given a position and the move "
    "that was just played, explain the move's idea or mistake in at most two "
    "short sentences. Do not restate the move in notation. Be specific and "
    "practical."
)

_MAX_TOKENS = 120


class CoachError(Exception):
    """Raised when a coach statement could not be produced."""


@dataclass
class CoachConfig:
    """Coach provider configuration, sourced from game settings.

    Attributes:
        provider: One of ``openai``/``anthropic``/``custom`` (or ``none``).
        api_key: Provider API key.
        model: Model id; falls back to the provider default when empty.
        base_url: Base URL for the ``custom`` (OpenAI-compatible) provider.
    """

    provider: str = "none"
    api_key: str = ""
    model: str = ""
    base_url: str = ""

    def is_configured(self) -> bool:
        """True when a real provider and an API key are set.

        ``custom`` additionally requires a base URL, since it has no default
        endpoint to fall back to.
        """
        if self.provider not in CONFIGURED_PROVIDERS:
            return False
        if not self.api_key:
            return False
        if self.provider == "custom" and not self.base_url:
            return False
        return True

    def resolved_model(self) -> str:
        """The model id to use, applying the provider default when unset."""
        if self.model:
            return self.model
        if self.provider == "anthropic":
            return ANTHROPIC_DEFAULT_MODEL
        return OPENAI_DEFAULT_MODEL


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
    """

    fen_before: str
    move_text: str
    side_to_move: str
    eval_before_cp: Optional[int] = None
    eval_after_cp: Optional[int] = None
    move_number: Optional[int] = None
    facts: Tuple[str, ...] = ()
    is_potential_move: bool = False


def _format_eval(cp: Optional[int]) -> str:
    """Human-readable eval from centipawns (white's perspective), or 'unknown'."""
    if cp is None:
        return "unknown"
    return f"{cp / 100.0:+.2f}"


def build_user_prompt(request: CoachRequest) -> str:
    """Build the user prompt describing the move and its evaluation swing.

    For a played move the prompt asks the coach to explain the executed move. For
    a potential move (``is_potential_move`` -- a hint/tip) it states the move has
    not been played yet and asks why the move would be a good choice, so the coach
    never critiques it as if it were already on the board.
    """
    move_label = (
        "Candidate move being considered (a hint, NOT yet played)"
        if request.is_potential_move
        else "Move played"
    )
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
    else:
        lines.append(
            "Coach the side that just moved in at most two short sentences. "
            + tactical_guard
        )
    return "\n".join(lines)


def _openai_base_url(config: CoachConfig) -> str:
    """Resolve the OpenAI-compatible base URL for openai/custom providers."""
    if config.provider == "custom":
        return config.base_url.rstrip("/")
    return OPENAI_DEFAULT_BASE_URL


def build_openai_payload(config: CoachConfig, request: CoachRequest) -> Tuple[str, dict, dict]:
    """Build ``(url, headers, json_body)`` for an OpenAI-compatible request."""
    url = f"{_openai_base_url(config)}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": config.resolved_model(),
        "max_tokens": _MAX_TOKENS,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(request)},
        ],
    }
    return url, headers, body


def build_anthropic_payload(config: CoachConfig, request: CoachRequest) -> Tuple[str, dict, dict]:
    """Build ``(url, headers, json_body)`` for an Anthropic Messages request."""
    headers = {
        "x-api-key": config.api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    body = {
        "model": config.resolved_model(),
        "max_tokens": _MAX_TOKENS,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": build_user_prompt(request)},
        ],
    }
    return ANTHROPIC_API_URL, headers, body


def parse_openai_response(data: dict) -> str:
    """Extract the assistant message text from an OpenAI-compatible response."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CoachError("Unexpected OpenAI response shape") from exc
    text = (content or "").strip()
    if not text:
        raise CoachError("Empty OpenAI response")
    return text


def parse_anthropic_response(data: dict) -> str:
    """Extract the text from an Anthropic Messages response."""
    try:
        blocks = data["content"]
        text = "".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        )
    except (KeyError, TypeError) as exc:
        raise CoachError("Unexpected Anthropic response shape") from exc
    text = text.strip()
    if not text:
        raise CoachError("Empty Anthropic response")
    return text


def fallback_models(provider: str) -> list:
    """Return the curated fallback model ids for a provider (may be empty).

    Used when the live model list cannot be fetched. ``custom`` has no curated
    list (its models are endpoint-specific and unknown), so it returns empty.
    """
    if provider == "anthropic":
        return list(ANTHROPIC_FALLBACK_MODELS)
    if provider == "openai":
        return list(OPENAI_FALLBACK_MODELS)
    return []


def build_models_request(config: CoachConfig) -> Tuple[str, dict]:
    """Build ``(url, headers)`` for the provider's list-models endpoint.

    Anthropic uses ``GET /v1/models`` with ``x-api-key``; openai/custom use the
    OpenAI-compatible ``GET {base}/models`` with a Bearer key.
    """
    if config.provider == "anthropic":
        headers = {
            "x-api-key": config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        return ANTHROPIC_MODELS_URL, headers
    headers = {"Authorization": f"Bearer {config.api_key}"}
    return f"{_openai_base_url(config)}/models", headers


def parse_models_response(data: dict) -> list:
    """Extract model ids from a models-list response.

    Both OpenAI-compatible and Anthropic list endpoints return ``{"data": [{"id":
    ...}, ...]}``. Raises :class:`CoachError` on a shape that carries no ids.
    """
    try:
        items = data["data"]
        ids = [item["id"] for item in items if isinstance(item, dict) and item.get("id")]
    except (KeyError, TypeError) as exc:
        raise CoachError("Unexpected models response shape") from exc
    if not ids:
        raise CoachError("No models returned")
    return ids


def filter_chat_models(provider: str, model_ids: list) -> list:
    """Return chat-usable model ids, sorted for stable display.

    Anthropic's listed models are all chat models, so they pass through (sorted).
    For openai/custom, ids whose name marks them as non-chat (audio, embeddings,
    image, etc.) are dropped -- but if that would empty the list (e.g. a custom
    endpoint using unusual names), the unfiltered list is returned so the user is
    never left with no choices.
    """
    if provider == "anthropic":
        return sorted(model_ids)
    filtered = [
        model_id
        for model_id in model_ids
        if not any(keyword in model_id.lower() for keyword in _NON_CHAT_MODEL_KEYWORDS)
    ]
    return sorted(filtered or model_ids)


def list_models(
    config: CoachConfig,
    *,
    http_get: Optional[Callable] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list:
    """Fetch the available model ids for the configured provider.

    Queries the provider's list-models endpoint with the configured key so the
    model dropdown always reflects models the account can actually use. Returns
    the chat-usable ids (sorted). Raises :class:`CoachError` if not configured or
    on any network/HTTP/parse failure -- the caller falls back to the curated
    list so a transient failure does not empty the dropdown.
    """
    if not config.is_configured():
        raise CoachError("Coach service is not configured")

    url, headers = build_models_request(config)

    if http_get is None:
        import requests

        http_get = requests.get

    try:
        response = http_get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # network layer failure
        raise CoachError("Model list request failed") from exc

    status = getattr(response, "status_code", None)
    if status is None or not (200 <= status < 300):
        raise CoachError(f"Model list returned status {status}")

    try:
        data = response.json()
    except Exception as exc:
        raise CoachError("Model list response was not valid JSON") from exc

    return filter_chat_models(config.provider, parse_models_response(data))


def generate_coach_statement(
    config: CoachConfig,
    request: CoachRequest,
    *,
    http_post: Optional[Callable] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Produce a coach statement for a move via the configured provider.

    Args:
        config: Provider configuration (must be configured).
        request: The move to coach.
        http_post: Callable used to POST (defaults to ``requests.post``);
            injectable for tests.
        timeout: Request timeout in seconds.

    Returns:
        The coach statement text.

    Raises:
        CoachError: If not configured, on any network/HTTP failure, or on an
            empty/malformed response.
    """
    if not config.is_configured():
        raise CoachError("Coach service is not configured")

    if config.provider == "anthropic":
        url, headers, body = build_anthropic_payload(config, request)
        parse = parse_anthropic_response
    else:
        # openai + custom share the OpenAI-compatible chat completions shape.
        url, headers, body = build_openai_payload(config, request)
        parse = parse_openai_response

    if http_post is None:
        import requests

        http_post = requests.post

    try:
        response = http_post(url, headers=headers, json=body, timeout=timeout)
    except Exception as exc:  # network layer failure
        raise CoachError("Coach request failed") from exc

    status = getattr(response, "status_code", None)
    if status is None or not (200 <= status < 300):
        raise CoachError(f"Coach request returned status {status}")

    try:
        data = response.json()
    except Exception as exc:
        raise CoachError("Coach response was not valid JSON") from exc

    return parse(data)


__all__ = [
    "CoachConfig",
    "CoachRequest",
    "CoachError",
    "generate_coach_statement",
    "build_user_prompt",
    "build_openai_payload",
    "build_anthropic_payload",
    "parse_openai_response",
    "parse_anthropic_response",
    "list_models",
    "build_models_request",
    "parse_models_response",
    "filter_chat_models",
    "fallback_models",
]
