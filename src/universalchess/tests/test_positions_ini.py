"""
Tests for positions.ini parsing and hint move functionality.

Tests validate:
1. Basic FEN parsing without hint moves
2. FEN parsing with hint moves
3. Invalid hint move detection
4. Game over state detection (checkmate, stalemate, insufficient material)
5. Hint move legality validation
"""

import unittest
import chess


class TestParsePositionEntry(unittest.TestCase):
    """Test the parse_position_entry function.
    
    Expected failure before fix: AttributeError (function doesn't exist)
    Expected pass after fix: All assertions pass
    """
    
    def setUp(self):
        """Import the function under test."""
        # Import here to get the latest version
        from universalchess.utils.positions import parse_position_entry
        self.parse = parse_position_entry
    
    def test_fen_only(self):
        """Test parsing FEN without hint move.
        
        Expected: Returns (fen, None) tuple
        Failure reason: Function returns incorrect tuple structure
        """
        value = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        fen, hint = self.parse(value)
        
        self.assertEqual(fen, value)
        self.assertIsNone(hint)
    
    def test_fen_with_hint(self):
        """Test parsing FEN with hint move.
        
        Expected: Returns (fen, hint_move) tuple
        Failure reason: Pipe separator not handled correctly
        """
        value = "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1 | a1a8"
        fen, hint = self.parse(value)
        
        self.assertEqual(fen, "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1")
        self.assertEqual(hint, "a1a8")
    
    def test_fen_with_promotion_hint(self):
        """Test parsing FEN with promotion hint move.
        
        Expected: Returns (fen, hint_move) with promotion suffix (e.g., a7a8q)
        Failure reason: 5-character UCI moves not handled
        """
        value = "8/P7/8/8/8/8/8/4K2k w - - 0 1 | a7a8q"
        fen, hint = self.parse(value)
        
        self.assertEqual(fen, "8/P7/8/8/8/8/8/4K2k w - - 0 1")
        self.assertEqual(hint, "a7a8q")
    
    def test_fen_with_extra_spaces(self):
        """Test parsing with extra spaces around pipe.
        
        Expected: Whitespace is trimmed from both FEN and hint
        Failure reason: Whitespace not stripped correctly
        """
        value = "  6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1   |   a1a8  "
        fen, hint = self.parse(value)
        
        self.assertEqual(fen, "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1")
        self.assertEqual(hint, "a1a8")
    
    def test_invalid_short_hint(self):
        """Test that too-short hint moves are rejected.
        
        Expected: hint_move is None for invalid format
        Failure reason: Invalid hints not filtered out
        """
        value = "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1 | a1"
        fen, hint = self.parse(value)
        
        self.assertEqual(fen, "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1")
        self.assertIsNone(hint)  # Too short, should be rejected
    
    def test_invalid_long_hint(self):
        """Test that too-long hint moves are rejected.
        
        Expected: hint_move is None for invalid format
        Failure reason: Invalid hints not filtered out
        """
        value = "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1 | a1a8qq"
        fen, hint = self.parse(value)
        
        self.assertEqual(fen, "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1")
        self.assertIsNone(hint)  # Too long, should be rejected


class TestGameOverPositions(unittest.TestCase):
    """Test detection of game-over states from positions.ini.
    
    These tests validate that the FEN positions correctly represent
    checkmate, stalemate, and insufficient material states.
    
    Expected failure before positions added: KeyError (positions not in config)
    Expected pass after fix: chess.Board correctly identifies game state
    """
    
    def test_checkmate_white_wins(self):
        """Test position where black is already checkmated.
        
        Expected: is_checkmate() returns True, turn is BLACK
        Failure reason: FEN doesn't represent actual checkmate
        """
        # Use a validated checkmate position from defaults/config/positions.ini ([game_over]).
        # Back rank mate - black is mated by white rook on a8.
        fen = "R6k/6pp/8/8/8/8/8/7K b - - 0 1"
        board = chess.Board(fen)
        
        self.assertTrue(board.is_checkmate(), 
            f"Position should be checkmate: {fen}")
        self.assertEqual(board.turn, chess.BLACK,
            "Black should be to move (and mated)")
    
    def test_checkmate_black_wins(self):
        """Test position where white is already checkmated.
        
        Expected: is_checkmate() returns True, turn is WHITE
        Failure reason: FEN doesn't represent actual checkmate
        """
        # White king on e1, black queen on e2, black king on f3
        fen = "8/8/8/8/8/5k2/4q3/4K3 w - - 0 1"
        board = chess.Board(fen)
        
        self.assertTrue(board.is_checkmate(),
            f"Position should be checkmate: {fen}")
        self.assertEqual(board.turn, chess.WHITE,
            "White should be to move (and mated)")
    
    def test_stalemate_black(self):
        """Test position where black is stalemated.
        
        Expected: is_stalemate() returns True, turn is BLACK
        Failure reason: FEN doesn't represent actual stalemate
        """
        # Use a validated stalemate position from defaults/config/positions.ini ([game_over]).
        fen = "8/8/8/8/8/1K6/2Q5/k7 b - - 0 1"
        board = chess.Board(fen)
        
        self.assertTrue(board.is_stalemate(),
            f"Position should be stalemate: {fen}")
        self.assertEqual(board.turn, chess.BLACK,
            "Black should be to move (and stalemated)")
    
    def test_stalemate_white(self):
        """Test position where white is stalemated.
        
        Expected: is_stalemate() returns True, turn is WHITE
        Failure reason: FEN doesn't represent actual stalemate
        """
        # Use a validated stalemate position from defaults/config/positions.ini ([game_over]).
        fen = "8/8/8/8/8/6k1/5q2/7K w - - 0 1"
        board = chess.Board(fen)
        
        self.assertTrue(board.is_stalemate(),
            f"Position should be stalemate: {fen}")
        self.assertEqual(board.turn, chess.WHITE,
            "White should be to move (and stalemated)")
    
    def test_insufficient_k_vs_k(self):
        """Test King vs King position.
        
        Expected: is_insufficient_material() returns True
        Failure reason: FEN doesn't have only two kings
        """
        fen = "8/8/4k3/8/8/4K3/8/8 w - - 0 1"
        board = chess.Board(fen)
        
        self.assertTrue(board.is_insufficient_material(),
            f"K vs K should be insufficient material: {fen}")
    
    def test_insufficient_kb_vs_k(self):
        """Test King+Bishop vs King position.
        
        Expected: is_insufficient_material() returns True
        Failure reason: FEN has more than KB vs K
        """
        fen = "8/8/4k3/8/8/4K3/8/4B3 w - - 0 1"
        board = chess.Board(fen)
        
        self.assertTrue(board.is_insufficient_material(),
            f"KB vs K should be insufficient material: {fen}")
    
    def test_insufficient_kn_vs_k(self):
        """Test King+Knight vs King position.
        
        Expected: is_insufficient_material() returns True
        Failure reason: FEN has more than KN vs K
        """
        fen = "8/8/4k3/8/8/4K3/8/4N3 w - - 0 1"
        board = chess.Board(fen)
        
        self.assertTrue(board.is_insufficient_material(),
            f"KN vs K should be insufficient material: {fen}")


class TestMateIn1Positions(unittest.TestCase):
    """Test mate-in-1 positions have legal winning moves.
    
    Expected failure before fix: Hint move is not legal or doesn't deliver mate
    Expected pass after fix: Hint move is legal and delivers checkmate
    """
    
    def test_mate_in_1_back_rank(self):
        """Test back rank mate in 1.
        
        Expected: a1a8 delivers checkmate
        Failure reason: Move is illegal or doesn't mate
        """
        fen = "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1"
        hint = "a1a8"
        
        board = chess.Board(fen)
        move = chess.Move.from_uci(hint)
        
        self.assertIn(move, board.legal_moves,
            f"Hint {hint} should be legal in position {fen}")
        
        board.push(move)
        self.assertTrue(board.is_checkmate(),
            f"Move {hint} should deliver checkmate")
    
    def test_mate_in_1_white(self):
        """Test white delivers mate with Rh8#.
        
        Expected: h1h8 delivers checkmate
        Failure reason: Move is illegal or doesn't mate
        """
        # Use validated opera mate position from defaults/config/positions.ini ([game_over]).
        fen = "3k4/8/3K4/3B4/8/8/8/7R w - - 0 1"
        hint = "h1h8"
        
        board = chess.Board(fen)
        move = chess.Move.from_uci(hint)
        
        self.assertIn(move, board.legal_moves,
            f"Hint {hint} should be legal in position {fen}")
        
        board.push(move)
        self.assertTrue(board.is_checkmate(),
            f"Move {hint} should deliver checkmate")


class TestStalemateIn1Positions(unittest.TestCase):
    """Test stalemate-in-1 positions.
    
    Expected failure before fix: Hint move doesn't stalemate opponent
    Expected pass after fix: Hint move is legal and causes stalemate
    """
    
    def test_stalemate_in_1_white(self):
        """Test white stalemates black in 1 move.
        
        Expected: d2c2 stalemates black
        Failure reason: Move is illegal or doesn't stalemate
        """
        # Use validated stalemate-in-1 position from defaults/config/positions.ini ([game_over]).
        fen = "8/8/8/8/8/1K6/3Q4/k7 w - - 0 1"
        hint = "d2c2"
        
        board = chess.Board(fen)
        move = chess.Move.from_uci(hint)
        
        self.assertIn(move, board.legal_moves,
            f"Hint {hint} should be legal in position {fen}")
        
        board.push(move)
        self.assertTrue(board.is_stalemate(),
            f"Move {hint} should cause stalemate")


class TestLoadPositionsConfig(unittest.TestCase):
    """Test load_positions_config returns correct structure.
    
    Expected failure before fix: Returns dict of strings instead of tuples
    Expected pass after fix: Returns dict of (fen, hint) tuples
    """
    
    def test_returns_tuples(self):
        """Test that loaded positions are (fen, hint) tuples.
        
        Expected: Each position value is a tuple of (str, str|None)
        Failure reason: Function still returns plain strings
        """
        from universalchess.utils.positions import load_positions_config
        
        positions = load_positions_config()
        
        # Should have at least the test category
        self.assertIn('test', positions,
            "positions.ini should have [test] section")
        
        # Each entry should be a tuple
        for category, entries in positions.items():
            for name, value in entries.items():
                self.assertIsInstance(value, tuple,
                    f"{category}/{name} should be a tuple, got {type(value)}")
                self.assertEqual(len(value), 2,
                    f"{category}/{name} tuple should have 2 elements")
                
                fen, hint = value
                self.assertIsInstance(fen, str,
                    f"{category}/{name} FEN should be string")
                self.assertTrue(hint is None or isinstance(hint, str),
                    f"{category}/{name} hint should be None or string")
    
    def test_game_over_category_exists(self):
        """Test that game_over category was added.
        
        Expected: positions has 'game_over' key with endgame positions
        Failure reason: Category not added to positions.ini
        """
        from universalchess.utils.positions import load_positions_config
        
        positions = load_positions_config()
        
        self.assertIn('game_over', positions,
            "positions.ini should have [game_over] section")
        
        # Check for specific positions
        game_over = positions['game_over']
        expected_positions = [
            'checkmate_back_rank_white_wins',
            'checkmate_back_rank_black_wins',
            'stalemate_black',
            'stalemate_white',
            'insufficient_k_vs_k',
        ]
        
        for pos_name in expected_positions:
            self.assertIn(pos_name, game_over,
                f"game_over should contain {pos_name}")


class TestEndgameSections(unittest.TestCase):
    """Guard the curated theoretical endgame sections in positions.ini.

    The endgames were expanded from a single flat [endgames] list into themed
    sections whose FENs are tablebase-verified (see
    tools/dev-tools/verify_endgames.py). These tests guard against two
    regressions the restructure could introduce:
      1. a themed section being dropped or renamed (the Positions menu would
         silently lose a whole category), and
      2. an illegal or already-finished FEN slipping in (it would be unplayable
         or immediately end the game when selected from the menu).
    """

    ENDGAME_SECTIONS = {
        "pawn_endgames": 9,
        "rook_endgames": 7,
        "queen_endgames": 6,
        "minor_piece_endgames": 6,
        "basic_mates": 5,
        "endgame_studies": 2,
    }

    def setUp(self):
        from universalchess.utils.positions import load_positions_config
        self.positions = load_positions_config()

    def test_all_themed_sections_present_with_expected_counts(self):
        """Every themed endgame section exists with its full position count.

        Failure manifests as a missing key (section dropped/renamed) or a count
        mismatch (a position was lost or an unverified one was added without
        updating this guard).
        """
        for section, expected_count in self.ENDGAME_SECTIONS.items():
            self.assertIn(section, self.positions,
                f"positions.ini should have [{section}] section")
            self.assertEqual(len(self.positions[section]), expected_count,
                f"[{section}] should have {expected_count} positions")

    def test_named_theoretical_positions_present(self):
        """The signature theoretical positions must be present by name.

        These are the anchors of the set (Lucena/Philidor/Vancura, the
        trebuchet, Reti and Saavedra studies). If a rename drops one, the menu
        loses a canonical teaching position even if counts still match.
        """
        expected = {
            "pawn_endgames": ["trebuchet_zugzwang", "opposition_hold_draw"],
            "rook_endgames": ["lucena_e_pawn_win", "philidor_defense_draw", "vancura_defense_draw"],
            "queen_endgames": ["queen_vs_rook_win", "queen_vs_rook_pawn_draw"],
            "basic_mates": ["bishop_knight_mate", "two_bishops_mate"],
            "endgame_studies": ["reti_draw", "saavedra_win"],
        }
        for section, names in expected.items():
            for name in names:
                self.assertIn(name, self.positions[section],
                    f"[{section}] should contain {name}")

    def test_all_endgame_positions_legal_and_in_progress(self):
        """Every endgame FEN is a legal, playable, not-yet-decided position.

        Unlike [game_over], these are meant to be played out. A checkmate/
        stalemate FEN here would end the game the instant it is loaded, and an
        illegal FEN (e.g. the enemy king left in check) cannot be set up at all.
        """
        for section in self.ENDGAME_SECTIONS:
            for name, (fen, _hint) in self.positions[section].items():
                board = chess.Board(fen)
                self.assertEqual(board.status(), chess.STATUS_VALID,
                    f"{section}/{name} FEN is not a legal position: {fen}")
                self.assertFalse(board.is_game_over(),
                    f"{section}/{name} FEN is already game over: {fen}")


class TestPositionsMenuCategoryIcons(unittest.TestCase):
    """The themed endgame sections must map to the endgame category icon.

    Without an explicit mapping the menu falls back to the generic "positions"
    icon, so the new endgame categories would look unrelated to endgames.
    """

    def test_endgame_subcategories_use_endgame_icon(self):
        from universalchess.menus.positions_menu import CATEGORY_ICONS
        for section in ("pawn_endgames", "rook_endgames", "queen_endgames",
                        "minor_piece_endgames", "basic_mates", "endgame_studies"):
            self.assertEqual(CATEGORY_ICONS.get(section), "positions_endgames",
                f"{section} should use the endgame icon")


if __name__ == '__main__':
    unittest.main()
