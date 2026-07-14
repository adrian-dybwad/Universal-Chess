# Derived-engine UCI protocol wrapper
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Implements the UCI protocol loop for a derived engine. It advertises the
# engine's options, tracks the board from ``position`` commands, applies
# ``setoption`` changes, and on ``go`` asks the injected Stockfish for a
# multi-PV evaluation of every legal move, converts each to a mover-POV
# ``Candidate`` (recording whether it is a capture), applies the engine's
# selection policy, and prints ``bestmove``.
#
# Stockfish is injected (any object exposing ``analyse(board, limit, multipv)``)
# and the RNG is injected too, so the loop is testable without a real engine
# process and randomised policies stay deterministic under test.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from __future__ import annotations

import random
from typing import Dict, List, Optional, TextIO, Tuple

import chess
import chess.engine

from .policies import Candidate, SelectionContext
from .spec import DerivedEngineSpec

# Time budget (seconds) for the whole per-move analysis when no clock/movetime
# is given. Small so the engine responds promptly; it still evaluates every
# legal move because a single multipv search shares this budget across them.
DEFAULT_MOVETIME_SECONDS = 0.3
# Floor applied to any computed budget so Stockfish always gets a usable slice.
MIN_BUDGET_SECONDS = 0.05
# Fraction of the remaining clock to spend when only wtime/btime is provided,
# and the cap on that clock-derived budget so a long clock never makes a move
# take unreasonably long (an explicit ``movetime`` is always honoured as given).
CLOCK_FRACTION = 0.05
MAX_CLOCK_BUDGET_SECONDS = 2.0

# The ``go`` sub-parameters that carry an integer value (milliseconds or a
# count). Parsed leniently; anything else is ignored.
_GO_INT_PARAMS = frozenset(
    {"movetime", "wtime", "btime", "winc", "binc", "depth", "nodes", "movestogo"}
)


def run(
    engine: chess.engine.SimpleEngine,
    spec: DerivedEngineSpec,
    in_stream: TextIO,
    out_stream: TextIO,
    rng: Optional[random.Random] = None,
) -> None:
    """Run the UCI command loop until EOF or ``quit``.

    Args:
        engine: Backing analysis engine (real Stockfish, or a fake in tests).
        spec: The derived engine: its display name, options, and select policy.
        in_stream: Line-oriented UCI command input.
        out_stream: UCI response output (flushed per line).
        rng: RNG for randomised policies; injected for deterministic tests. A
            fresh :class:`random.Random` is created when omitted.
    """
    if rng is None:
        # Non-security RNG: it only varies which near-equal move Drawfish plays.
        # A seedable PRNG is required so tests stay deterministic; a CSPRNG would
        # add nothing and cannot be seeded for reproducibility.
        rng = random.Random()  # noqa: S311  # nosec B311
    board = chess.Board()
    option_values: Dict[str, int] = spec.default_option_values()

    def send(line: str) -> None:
        out_stream.write(line + "\n")
        out_stream.flush()

    for raw in in_stream:
        line = raw.strip()
        if not line:
            continue
        tokens = line.split()
        command = tokens[0]

        if command == "uci":
            send(f"id name {spec.display_name}")
            send("id author Universal-Chess")
            for option in spec.options:
                send(option.handshake_line())
            send("uciok")
        elif command == "isready":
            send("readyok")
        elif command == "ucinewgame":
            board = chess.Board()
        elif command == "setoption":
            _apply_setoption(spec, option_values, tokens[1:])
        elif command == "position":
            board = _parse_position(tokens[1:])
        elif command == "go":
            move = _choose_move(engine, spec, board, tokens[1:], option_values, rng)
            send(f"bestmove {move.uci()}" if move is not None else "bestmove (none)")
        elif command == "quit":
            break
        # Other commands (debug, ...) are accepted and ignored.


def _apply_setoption(
    spec: DerivedEngineSpec, option_values: Dict[str, int], args: List[str]
) -> None:
    """Apply a ``setoption name <name> value <value>`` command in place.

    Unknown option names and values that fail the option's own validation are
    ignored, leaving the prior value intact -- a GUI sending a malformed option
    must never crash the engine or silently corrupt a setting. The option name
    is taken verbatim from between ``name`` and ``value`` (so multi-word names
    are supported), and the value is everything after ``value``.
    """
    name, raw_value = _parse_setoption(args)
    if name is None or raw_value is None:
        return
    for option in spec.options:
        if option.name == name:
            coerced = option.coerce(raw_value)
            if coerced is not None:
                option_values[name] = coerced
            return


def _parse_setoption(args: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Split ``name <name...> value <value...>`` into (name, value) strings.

    Returns ``(None, None)`` when either keyword is absent, so the caller can
    ignore a malformed command.
    """
    if "name" not in args or "value" not in args:
        return None, None
    name_index = args.index("name")
    value_index = args.index("value")
    if name_index > value_index:
        return None, None
    name = " ".join(args[name_index + 1:value_index])
    value = " ".join(args[value_index + 1:])
    if not name or not value:
        return None, None
    return name, value


def _parse_position(args: List[str]) -> chess.Board:
    """Build a board from the arguments of a ``position`` command.

    Handles ``startpos`` and ``fen <6 fields>``, each optionally followed by
    ``moves <uci> ...``. An empty/unrecognised argument list yields the initial
    position, matching UCI's default.
    """
    if not args:
        return chess.Board()

    if args[0] == "startpos":
        board = chess.Board()
        index = 1
    elif args[0] == "fen":
        # A FEN is exactly six space-separated fields.
        board = chess.Board(" ".join(args[1:7]))
        index = 7
    else:
        return chess.Board()

    if index < len(args) and args[index] == "moves":
        for uci in args[index + 1:]:
            board.push_uci(uci)
    return board


def _choose_move(
    engine: chess.engine.SimpleEngine,
    spec: DerivedEngineSpec,
    board: chess.Board,
    go_args: List[str],
    option_values: Dict[str, int],
    rng: random.Random,
) -> Optional[chess.Move]:
    """Select the move to play for the current position.

    Returns None only when there are no legal moves (the game is already over,
    so no ``bestmove`` is meaningful). A single legal move is played directly
    without spending the analysis budget. Otherwise every legal move is scored
    by the engine (one multi-PV search), each is tagged as capture-or-not from
    the current board, and the policy chooses among them; if the engine returns
    no usable lines, the first legal move is played as a safe fallback rather
    than failing to move.
    """
    legal = list(board.legal_moves)
    if not legal:
        return None
    if len(legal) == 1:
        return legal[0]

    budget = _parse_time_budget(go_args, board.turn)
    infos = engine.analyse(board, chess.engine.Limit(time=budget), multipv=len(legal))

    candidates: List[Candidate] = []
    for info in infos:
        principal_variation = info.get("pv")
        score = info.get("score")
        if not principal_variation or score is None:
            continue
        move = principal_variation[0]
        candidates.append(
            Candidate(
                move=move,
                score=score.pov(board.turn),
                is_capture=board.is_capture(move),
            )
        )

    if not candidates:
        return legal[0]
    ctx = SelectionContext(options=option_values, rng=rng)
    return spec.select(candidates, ctx)


def _parse_time_budget(go_args: List[str], turn: chess.Color) -> float:
    """Derive the analysis time budget (seconds) from ``go`` parameters.

    An explicit ``movetime`` is honoured as given (only floored). Otherwise a
    small fraction of the mover's remaining clock is used (floored and capped).
    With neither, a fixed default keeps the engine responsive.
    """
    values = _parse_go_ints(go_args)

    if "movetime" in values:
        return max(values["movetime"] / 1000.0, MIN_BUDGET_SECONDS)

    remaining = values.get("wtime" if turn == chess.WHITE else "btime")
    if remaining is not None:
        clock_budget = remaining / 1000.0 * CLOCK_FRACTION
        return min(max(clock_budget, MIN_BUDGET_SECONDS), MAX_CLOCK_BUDGET_SECONDS)

    return DEFAULT_MOVETIME_SECONDS


def _parse_go_ints(go_args: List[str]) -> dict:
    """Extract the integer-valued ``go`` sub-parameters, ignoring malformed ones.

    A non-integer operand is skipped (checked before conversion, so no parse of
    the whole command is aborted) and the remaining well-formed parameters still
    apply.
    """
    values: dict = {}
    for i in range(len(go_args) - 1):
        key = go_args[i]
        operand = go_args[i + 1]
        # UCI integer operands are (optionally signed) digit strings; validating
        # before int() avoids swallowing a broad exception for a value that a
        # malformed command could contain.
        if key in _GO_INT_PARAMS and operand.lstrip("-").isdigit():
            values[key] = int(operand)
    return values
