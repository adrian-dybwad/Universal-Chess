"""Generate a coach statement, grounded so it never shows an impossible move.

The AI coach can hallucinate a concrete-sounding but illegal line (the reported
bug: "cxd4 in response to d3" when d4 is empty). The service layer that talks to
the provider has no chess dependency and cannot check this, so this module -- which
does -- wraps generation with a validate/regenerate/repair loop:

1. Generate a statement via the coach service.
2. If it names a move that is illegal in the position (:mod:`coach_move_check`),
   regenerate once with an explicit note telling the model which move to avoid.
3. If the final attempt still names an illegal move, drop the offending
   sentence(s); if nothing usable remains, return a short, move-free fallback.

The result never contains an impossible move, so the panel shows correct advice or
a safe generic remark instead of nonsense. Provider/network failures raise
:class:`CoachError` unchanged so the caller's existing failure handling applies.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Callable, Optional

from universalchess.services.coach import CoachConfig, CoachRequest

from .coach_move_check import find_grounding_problems

# Attempts total: the first plus one grounded regeneration. More attempts add
# latency and cost for diminishing returns; a single corrective retry with the
# explicit "this move was illegal" note fixes the vast majority of cases.
_MAX_ATTEMPTS = 2

# Truthful, move-free remark shown only when every attempt named an illegal move
# and removing the offending sentences left nothing usable. It makes no specific
# tactical claim, so it can never be wrong for the position.
_FALLBACK_STATEMENT = (
    "Look for your safest, most active continuation and check your opponent's best reply."
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _retry_note(problems: list[str]) -> str:
    """Instruction appended on regeneration naming the fabricated reference(s).

    Covers both classes caught by :func:`find_grounding_problems`: illegal moves and
    claims that a piece is on a square where it is not.
    """
    items = ", ".join(problems)
    return (
        "A previous attempt referenced something that does not match the actual "
        f"position: {items}. Do not mention these. Only name moves that are legal "
        "here and pieces that are actually on the board as listed in the piece "
        "placement; if unsure, describe the idea without naming a specific move or "
        "square."
    )


def _strip_offending_sentences(statement: str, problems: list[str]) -> str:
    """Remove sentences containing any fabricated move/claim, join the rest."""
    sentences = _SENTENCE_SPLIT_RE.split(statement.strip())
    kept = [s for s in sentences if not any(tok in s for tok in problems)]
    return " ".join(part.strip() for part in kept if part.strip()).strip()


def generate_validated_statement(
    config: CoachConfig,
    request: CoachRequest,
    *,
    generate: Optional[Callable[[CoachConfig, CoachRequest], str]] = None,
    http_post: Optional[Callable] = None,
    max_attempts: int = _MAX_ATTEMPTS,
) -> str:
    """Return a coach statement guaranteed to name no illegal move.

    Args:
        config: Agent configuration.
        request: The move to coach. ``fen_before``/``move_uci``/``chess960`` are
            used to validate the moves the model names.
        generate: The underlying generator ``(config, request) -> str``. Defaults
            to ``services.coach.generate_coach_statement`` (with ``http_post``
            threaded through); injectable for tests.
        http_post: Passed to the default generator only.
        max_attempts: Total generation attempts (first + regenerations).

    Raises:
        CoachError: Propagated unchanged from the underlying generator on a
            provider/network failure, so existing failure handling still applies.
    """
    if generate is None:
        from universalchess.services.coach import generate_coach_statement

        def generate(cfg: CoachConfig, req: CoachRequest) -> str:  # noqa: E306
            # Only forward http_post when supplied so the service uses its own
            # default transport; callers/tests that stub generate_coach_statement
            # with a plain (config, request) signature then still work.
            if http_post is None:
                return generate_coach_statement(cfg, req)
            return generate_coach_statement(cfg, req, http_post=http_post)

    fen = request.fen_before
    uci = request.move_uci
    chess960 = request.chess960

    statement = ""
    problems: list[str] = []
    for attempt in range(max_attempts):
        current = request if attempt == 0 else replace(request, retry_note=_retry_note(problems))
        statement = generate(config, current)
        problems = find_grounding_problems(statement, fen, uci, chess960=chess960)
        if not problems:
            return statement

    # Every attempt still referenced a fabricated move/piece: repair by dropping the
    # offending sentence(s). If a coherent, clean remainder is left, use it;
    # otherwise fall back to the safe statement rather than show something false.
    repaired = _strip_offending_sentences(statement, problems)
    if repaired and not find_grounding_problems(repaired, fen, uci, chess960=chess960):
        return repaired
    return _FALLBACK_STATEMENT


__all__ = ["generate_validated_statement"]
