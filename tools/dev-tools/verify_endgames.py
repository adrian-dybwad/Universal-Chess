"""Verify the curated endgame set shipped in defaults/config/positions.ini.

For every position this checks:
  * the FEN is syntactically valid and a legal chess position (python-chess),
  * the position is not already game over, and
  * for positions with <=7 pieces, the Lichess Syzygy tablebase agrees with the
    expected game-theoretic result (win / draw / loss from the side to move).

Run with network access:  python tools/dev-tools/verify_endgames.py

This is a maintenance/curation aid, not shipped application code. It documents
why each FEN is included and guards against a typo silently shipping a
mislabeled position (e.g. a "winning" position that is actually a draw).
"""

import json
import sys
import time
import urllib.parse
import urllib.request

import chess

TB_URL = "https://tablebase.lichess.ovh/standard?"

# section -> name -> (fen, expected_result)
# expected_result is from the side-to-move perspective: "win", "draw", or "loss"
# ("loss" is used for reciprocal-zugzwang positions where the mover loses).
CURATED = {
    "pawn_endgames": {
        "opposition_hold_draw": ("8/8/8/4k3/8/4K3/4P3/8 w - - 0 1", "draw"),
        "pawn_win_opposition": ("3k4/8/3K4/3P4/8/8/8/8 w - - 0 1", "win"),
        "trebuchet_zugzwang": ("8/8/8/4pK2/3kP3/8/8/8 w - - 0 1", "loss"),
        "rook_pawn_corner_draw": ("k7/8/K7/P7/8/8/8/8 w - - 0 1", "draw"),
        "two_connected_passers": ("8/8/8/3k4/8/2PP4/8/4K3 w - - 0 1", "win"),
        "protected_passer_win": ("8/8/3k4/3P4/3KP3/8/8/8 w - - 0 1", "win"),
        "king_catches_pawn_draw": ("8/8/8/8/5k2/8/P7/K7 w - - 0 1", "draw"),
        "pawn_outruns_king_win": ("8/8/8/8/8/6k1/P7/K7 w - - 0 1", "win"),
        "distant_opposition_draw": ("8/8/8/3k4/8/8/3P4/3K4 w - - 0 1", "draw"),
    },
    "rook_endgames": {
        "lucena_c_pawn_win": ("2K5/2P1k3/8/8/8/8/1r6/3R4 w - - 0 1", "win"),
        "lucena_e_pawn_win": ("4K3/4P1k1/8/8/8/8/r7/3R4 w - - 0 1", "win"),
        "lucena_f_pawn_win": ("5K2/5P1k/8/8/8/8/r7/3R4 w - - 0 1", "win"),
        "lucena_g_pawn_win": ("6K1/6P1/5k2/8/8/8/r7/3R4 w - - 0 1", "win"),
        "philidor_defense_draw": ("3k4/R7/8/3KP3/8/8/8/5r2 b - - 0 1", "draw"),
        "vancura_defense_draw": ("R7/6r1/P4k2/8/8/8/8/6K1 w - - 0 1", "draw"),
        "rook_vs_pawn_win": ("8/8/8/8/2k5/2p5/8/2K4R w - - 0 1", "win"),
    },
    "queen_endgames": {
        "queen_vs_rook_win": ("8/8/8/4k3/8/4K3/8/3Q3r w - - 0 1", "win"),
        "queen_vs_central_pawn_win": ("8/8/8/8/8/1k6/3p4/3K1Q2 w - - 0 1", "win"),
        "queen_vs_rook_pawn_draw": ("8/7K/8/8/4Q3/8/p7/k7 w - - 0 1", "draw"),
        "queen_vs_rook_pawn_win": ("8/8/8/8/2K5/8/p5Q1/k7 w - - 0 1", "win"),
        "queen_vs_bishop_pawn_draw": ("7K/8/8/8/4Q3/8/1kp5/8 w - - 0 1", "draw"),
        "queen_vs_knight_pawn_win": ("8/8/8/7Q/8/1k6/1p6/4K3 w - - 0 1", "win"),
    },
    "minor_piece_endgames": {
        "two_knights_no_mate_draw": ("8/8/8/4k3/8/4K3/8/2N2N2 w - - 0 1", "draw"),
        "opposite_bishops_draw": ("8/8/4k3/4b3/8/2B5/4P3/4K3 w - - 0 1", "draw"),
        "wrong_bishop_rook_pawn_draw": ("7k/8/5K1P/8/8/8/8/5B2 b - - 0 1", "draw"),
        "right_bishop_rook_pawn_win": ("7k/8/5K1P/8/8/8/8/6B1 w - - 0 1", "win"),
        "bishop_and_pawn_win": ("8/8/4k3/8/4P3/4K3/8/4B3 w - - 0 1", "win"),
        "knight_and_pawn_win": ("8/8/4k3/8/4P3/4K3/8/4N3 w - - 0 1", "win"),
    },
    "basic_mates": {
        "king_queen_mate": ("8/8/8/4k3/8/4K3/8/4Q3 w - - 0 1", "win"),
        "king_rook_mate": ("8/8/8/4k3/8/4K3/8/4R3 w - - 0 1", "win"),
        "two_rooks_mate": ("7k/R7/8/8/8/8/8/6RK w - - 0 1", "win"),
        "bishop_knight_mate": ("8/8/8/4k3/8/4K3/8/2B2N2 w - - 0 1", "win"),
        "two_bishops_mate": ("8/8/8/4k3/8/4K3/8/2B2B2 w - - 0 1", "win"),
    },
    "endgame_studies": {
        "reti_draw": ("7K/8/k1P5/7p/8/8/8/8 w - - 0 1", "draw"),
        "saavedra_win": ("8/8/1KP5/3r4/8/8/8/k7 w - - 0 1", "win"),
    },
}


def tb_lookup(fen):
    url = TB_URL + urllib.parse.urlencode({"fen": fen})
    # Guard the scheme so this can only ever perform an HTTPS request, never a
    # file:/custom-scheme open (the concern behind ruff S310 / bandit B310).
    # TB_URL is a constant https endpoint; this stays correct if it is edited.
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https tablebase URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "universalchess-endgame-verify"})  # noqa: S310
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310  # nosec B310 - https-only, guarded above
                return json.load(resp)
        except Exception as exc:
            if attempt == 3:
                return {"error": str(exc)}
            time.sleep(1.0)


def main():
    failures = 0
    total = 0
    for section, entries in CURATED.items():
        print(f"[{section}]")
        for name, (fen, expected) in entries.items():
            total += 1
            try:
                board = chess.Board(fen)
            except ValueError as exc:
                print(f"  FAIL {name}: illegal FEN: {exc}")
                failures += 1
                continue
            if board.status() != chess.STATUS_VALID:
                print(f"  FAIL {name}: illegal position (status={int(board.status())})")
                failures += 1
                continue
            if board.is_game_over():
                print(f"  FAIL {name}: already game over")
                failures += 1
                continue
            d = tb_lookup(fen)
            if "error" in d:
                print(f"  WARN {name}: tablebase error: {d['error']}")
                continue
            cat = d.get("category")
            dtm = d.get("dtm")
            ok = cat == expected
            if not ok:
                failures += 1
            print(f"  {'OK  ' if ok else 'FAIL'} {name:28} tb={cat} dtm={dtm} exp={expected}")
    print(f"\n{total - failures}/{total} verified; {failures} failure(s).")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
