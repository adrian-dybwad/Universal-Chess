# In-process policy engine player (Worstfish / Drawfish)
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# The novelty engines Worstfish and Drawfish have no evaluation of their own;
# they drive Stockfish and then pick a move by a policy (the worst move, or a
# near-equal non-winning move). This player runs that policy IN-PROCESS on top
# of the app's shared, pooled Stockfish from ``EngineRegistry`` instead of
# launching the derived-engine UCI subprocess, which would spawn a SECOND
# Stockfish. On a low-RAM board (dgt-64: 415 MiB) a second Stockfish thrashes
# swap and the first move cost ~40-130s; sharing the already-warm pooled engine
# removes both the extra process and its cold start.
#
# The launcher shim is still installed and used for option probing (the Settings
# schema reads Randomness/AvoidCaptures from its UCI handshake) and for the
# catalog install/uninstall flow; only move computation is moved in-process.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from __future__ import annotations

import pathlib
import random
from typing import List, Optional

import chess
import chess.engine

from universalchess.board.logging import log
from universalchess.services.derived_engines.policies import Candidate, SelectionContext
from universalchess.services.derived_engines.spec import DerivedEngineSpec, SPECS
from universalchess.services.derived_engines.stockfish import resolve_stockfish_path
from universalchess.services.engine_registry import EngineHandle
from .engine import EnginePlayer, EnginePlayerConfig


class PolicyEnginePlayer(EnginePlayer):
    """An engine player whose move is chosen by a policy over shared Stockfish.

    Reuses :class:`EnginePlayer`'s entire lifecycle (async load, think thread,
    pending-move / physical-board handling) and overrides only the three seams
    that must differ:

    * :meth:`_resolve_engine_path` -> the shared Stockfish binary, so the
      registry pools ONE Stockfish across this player, analysis, and the coach
      (no second engine process).
    * :meth:`_configure_handle` -> no-op: Randomness/AvoidCaptures are not
      Stockfish options and are applied in Python; the shared engine must not be
      mutated with options it does not understand (or that would disrupt other
      consumers).
    * :meth:`_compute_move` -> a single multi-PV analyse of every legal move,
      converted to :class:`Candidate` objects, with the derived engine's
      selection policy applied.

    The ``engine_name`` stays the derived id (``"worstfish"``/``"drawfish"``) so
    the display name and the saved ``.uci`` option section still resolve to the
    right engine; only the executable that is launched changes to Stockfish.
    """

    def __init__(
        self,
        config: Optional[EnginePlayerConfig] = None,
        spec: Optional[DerivedEngineSpec] = None,
    ) -> None:
        """Create the player for a derived-engine spec.

        Args:
            config: Engine config; ``engine_name`` selects the ``.uci`` options
                and display name.
            spec: The derived engine's spec (options + selection policy). When
                omitted it is looked up from :data:`SPECS` by
                ``config.engine_name``; an unknown name is a programming error
                (the caller must only route known policy engines here).
        """
        super().__init__(config)
        resolved = spec or SPECS.get(self._engine_config.engine_name)
        if resolved is None:
            raise ValueError(
                f"No derived-engine spec for {self._engine_config.engine_name!r}"
            )
        self._spec: DerivedEngineSpec = resolved
        # Non-security RNG: it only varies which near-equal/worst move is played.
        # Must be a seedable PRNG so tests are deterministic; a CSPRNG would add
        # nothing and cannot be seeded for reproducibility.
        self._rng = random.Random()  # noqa: S311  # nosec B311
        # Pondering has no meaning for a policy engine (it does not use
        # play()/go ponder), and would otherwise make start() acquire a
        # dedicated Stockfish, defeating the point of sharing the pooled one.
        if self._engine_config.ponder:
            log.info(
                "[PolicyEnginePlayer] Disabling ponder for policy engine "
                f"{self._engine_config.engine_name}"
            )
            self._engine_config.ponder = False

    def _resolve_engine_path(self) -> Optional[pathlib.Path]:
        """Resolve to the shared Stockfish binary (not the derived-engine shim).

        Acquiring Stockfish's path means the registry hands back the same pooled,
        already-initialised Stockfish used elsewhere, so no second process is
        spawned. Returns None when Stockfish cannot be found, matching the base
        contract (start() then reports the engine as unavailable).
        """
        path = resolve_stockfish_path()
        if path:
            return pathlib.Path(path)
        log.error("[PolicyEnginePlayer] Stockfish not found; cannot back policy engine")
        return None

    def _configure_handle(self, handle: EngineHandle) -> None:
        """Do not push options to the shared Stockfish.

        Randomness/AvoidCaptures are policy inputs, not Stockfish options, and
        are read from ``_uci_options`` at move time. Configuring the shared
        engine here would either be dropped (unadvertised options) or disturb
        other consumers of the same pooled Stockfish.
        """
        log.debug(
            "[PolicyEnginePlayer] Skipping engine configure; policy options "
            "applied in-process"
        )

    def _compute_move(
        self, handle: EngineHandle, board: chess.Board
    ) -> Optional[chess.Move]:
        """Choose a move via multi-PV analyse + the engine's selection policy.

        Returns None with no legal moves, and plays a forced single move without
        analysing. Otherwise every legal move is scored in one multi-PV search on
        the shared Stockfish, each is tagged capture-or-not from the current
        board, and the policy picks among them using the user's option values. If
        the engine returns no usable lines, the first legal move is played rather
        than failing to move (mirrors the subprocess wrapper's fallback).
        """
        legal = list(board.legal_moves)
        if not legal:
            return None
        if len(legal) == 1:
            return legal[0]

        infos = handle.analyse(
            board,
            chess.engine.Limit(time=self._engine_config.time_limit_seconds),
            multipv=len(legal),
        )

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
        ctx = SelectionContext(
            options=self._spec.resolve_options(self._uci_options), rng=self._rng
        )
        return self._spec.select(candidates, ctx)
