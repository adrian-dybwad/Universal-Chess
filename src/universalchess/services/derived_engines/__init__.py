# Derived engines
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# "Derived" engines are not standalone chess engines: each is a thin UCI wrapper
# process that drives the already-installed Stockfish and applies a pure
# move-selection policy to Stockfish's per-move evaluations. This package ships
# two of them:
#
#   * Worstfish -- plays the move Stockfish rates worst for the side to move.
#   * Drawfish  -- inspired by the chess.com "Zach" beginner bot: refuses to win,
#                  never willingly checkmates, avoids captures, and shuffles
#                  toward equality.
#
# The move-selection logic lives in ``policies`` (pure, no I/O), each engine's
# options + policy in ``spec``, the UCI protocol loop in ``uci_wrapper``
# (Stockfish and RNG injected so it is testable without a real engine), and the
# on-disk launcher shim in ``shim``. ``__main__`` wires them together so the
# engine runs as ``python -m universalchess.services.derived_engines <engine-id>``.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.
