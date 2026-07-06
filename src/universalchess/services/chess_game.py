"""
Chess game service.

Manages game lifecycle, coordinates with ChessGameState, and broadcasts
game state to web clients via Unix socket.

PGN is maintained incrementally in memory to avoid rebuilding the entire
game tree on every move. Takebacks are handled by navigating back in the
game tree.
"""

from typing import Optional
import chess
import chess.pgn

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.state import get_chess_game as get_game_state
from universalchess.state import get_players_state
from universalchess.paths import write_fen_log
from universalchess.services.game_broadcast import broadcast_game_state


class ChessGameService:
    """Service managing chess game lifecycle and FEN log.
    
    Maintains a chess.pgn.Game in memory for efficient PGN generation.
    The game tree is updated incrementally on moves and takebacks rather
    than rebuilding on every position change.
    """
    
    def __init__(self):
        """Initialize the chess game service."""
        self._state = get_game_state()
        
        # PGN game tree - updated incrementally
        self._pgn_game: chess.pgn.Game = chess.pgn.Game()
        self._pgn_node: chess.pgn.GameNode = self._pgn_game  # Current position in tree
        self._last_move_count: int = 0  # Track move count to detect takebacks
        
        # Register for position changes to write FEN log
        self._state.on_position_change(self._on_position_change)
    
    # -------------------------------------------------------------------------
    # Properties (delegate to state for reads)
    # -------------------------------------------------------------------------
    
    @property
    def fen(self) -> str:
        """Current position in FEN notation."""
        return self._state.fen
    
    @property
    def turn(self):
        """Which player's turn."""
        return self._state.turn
    
    @property
    def is_game_over(self) -> bool:
        """Whether the game has ended."""
        return self._state.is_game_over
    
    @property
    def result(self) -> Optional[str]:
        """Game result, or None if ongoing."""
        return self._state.result
    
    # -------------------------------------------------------------------------
    # Game lifecycle
    # -------------------------------------------------------------------------
    
    def new_game(self, fen: Optional[str] = None) -> None:
        """Start a new game.
        
        Creates a fresh PGN game tree. If a custom FEN is provided, it's set
        as the starting position in the PGN headers.
        
        Args:
            fen: Starting position FEN, or None for standard starting position.
        """
        # Reset PGN game tree
        self._pgn_game = chess.pgn.Game()
        self._pgn_node = self._pgn_game
        self._last_move_count = 0
        
        if fen:
            self._state.set_position(fen)
            # Set FEN in PGN headers for non-standard starting positions
            self._pgn_game.headers["FEN"] = fen
            self._pgn_game.headers["SetUp"] = "1"
        else:
            self._state.reset()
        
        log.info(f"[ChessGameService] New game started: {self._state.fen}")
    
    def end_game(self, result: str, termination: str) -> None:
        """End the current game.
        
        Args:
            result: Game result ('1-0', '0-1', '1/2-1/2')
            termination: How game ended ('resignation', 'time_forfeit', etc.)
        """
        self._state.set_result(result, termination)
        log.info(f"[ChessGameService] Game ended: {result} ({termination})")
    
    # -------------------------------------------------------------------------
    # Move operations (delegate to state)
    # -------------------------------------------------------------------------
    
    def push_move(self, move) -> None:
        """Push a move onto the board.
        
        Args:
            move: chess.Move to execute.
        """
        self._state.push_move(move)
    
    def push_uci(self, uci: str):
        """Push a move by UCI string.
        
        Args:
            uci: Move in UCI format.
            
        Returns:
            The parsed chess.Move.
        """
        return self._state.push_uci(uci)
    
    def pop_move(self):
        """Pop the last move (takeback).
        
        Returns:
            The popped move, or None.
        """
        return self._state.pop_move()
    
    def set_position(self, fen: str) -> None:
        """Set the board to a specific position.
        
        Args:
            fen: FEN string of the position.
        """
        self._state.set_position(fen)
    
    # -------------------------------------------------------------------------
    # PGN generation
    # -------------------------------------------------------------------------
    
    def get_pgn(self) -> str:
        """Generate PGN string for the current game.
        
        The PGN is generated from the in-memory game tree which is updated
        incrementally on each move/takeback. This is O(n) where n is the
        number of moves, but the game tree itself is already built.
        
        Returns:
            PGN formatted string of the current game.
        """
        try:
            # Update headers before export (they may have changed)
            players = get_players_state()
            self._pgn_game.headers["White"] = players.white_name
            self._pgn_game.headers["Black"] = players.black_name
            
            # Set result if game is over
            if self._state.is_game_over:
                result = self._state.result
                if result:
                    self._pgn_game.headers["Result"] = result
            else:
                self._pgn_game.headers["Result"] = "*"
            
            # Export to string
            exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
            return self._pgn_game.accept(exporter)
        except Exception as e:
            log.debug(f"[ChessGameService] Error generating PGN: {e}")
            return ""
    
    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------
    
    def _sync_pgn_tree(self) -> None:
        """Synchronize PGN game tree with current board state.
        
        Detects whether a move was added or taken back by comparing move counts,
        then updates the PGN tree accordingly:
        - Move added: Add variation to current node
        - Takeback: Navigate to parent node
        - Reset to 0 moves: Create fresh PGN tree (new game)
        """
        move_stack = self._state.move_stack
        current_move_count = len(move_stack)
        
        # New game detection: went from having moves to having none
        if current_move_count == 0 and self._last_move_count > 0:
            log.debug("[ChessGameService] Move count went to 0, starting fresh PGN")
            self._pgn_game = chess.pgn.Game()
            self._pgn_node = self._pgn_game
            self._last_move_count = 0
            return
        
        if current_move_count > self._last_move_count:
            # Move(s) added - add to PGN tree
            # Handle case where multiple moves were added (shouldn't happen normally)
            for i in range(self._last_move_count, current_move_count):
                move = move_stack[i]
                self._pgn_node = self._pgn_node.add_variation(move)
        
        elif current_move_count < self._last_move_count:
            # Takeback - navigate back in tree AND remove the taken-back moves
            moves_to_pop = self._last_move_count - current_move_count
            for _ in range(moves_to_pop):
                parent = self._pgn_node.parent
                if parent is not None:
                    # Remove the current node from its parent's variations
                    # This ensures the PGN export reflects the takeback
                    parent.remove_variation(self._pgn_node)
                    self._pgn_node = parent
                else:
                    # Already at root, can't go further back
                    break
        
        # If move count is same but position changed, this is likely a set_position
        # which should be handled by new_game(). Log a warning.
        elif current_move_count == self._last_move_count and current_move_count > 0:
            # Position changed without move count change - could be set_position
            # without calling new_game(). Rebuild tree from scratch as fallback.
            log.debug("[ChessGameService] Position changed without move count change, rebuilding PGN tree")
            self._rebuild_pgn_tree()
        
        self._last_move_count = current_move_count
    
    def _rebuild_pgn_tree(self) -> None:
        """Rebuild PGN tree from scratch based on current move stack.
        
        Fallback for cases where incremental update isn't possible
        (e.g., set_position called mid-game without new_game).
        """
        self._pgn_game = chess.pgn.Game()
        self._pgn_node = self._pgn_game
        
        for move in self._state.move_stack:
            self._pgn_node = self._pgn_node.add_variation(move)
        
        self._last_move_count = len(self._state.move_stack)
    
    def _on_position_change(self) -> None:
        """Called when position changes. Updates PGN tree and broadcasts to web."""
        # Sync PGN tree with board state
        self._sync_pgn_tree()
        
        fen = self._state.fen
        
        # Write FEN log for backwards compatibility (Chromecast, etc)
        try:
            write_fen_log(fen)
        except Exception as e:
            log.debug(f"[ChessGameService] Error writing FEN log: {e}")
        
        # Broadcast to web clients
        self.broadcast_state()
    
    def broadcast_state(self) -> None:
        """Broadcast current game state to web clients.
        
        Called after position changes and also after game end events
        (resignation, draw, flag) that don't change the position but
        do change the game_over/result status.
        """
        try:
            from universalchess.services.game_broadcast import (
                get_pending_move,
                set_pending_move,
            )
            players = get_players_state()
            move_stack = self._state.move_stack
            last_move = move_stack[-1].uci() if move_stack else None

            # Reconcile the pending move against the actual position. The pending
            # move is a side channel: GameManager sets it when an engine/Lichess
            # move is announced (blue "play this" arrow) and clears it via
            # MoveState.reset() once the move is played. If that clear is ever
            # missed, a stale pending (the move already on the board) lingers -
            # the web then draws a lone blue arrow and suppresses the green
            # best-move arrow. A genuine pending move is always legal in the
            # current position; a played or otherwise invalidated one is not
            # (its from-square is empty). Drop anything not currently legal so
            # this single broadcast point stays authoritative for all clients.
            pending = get_pending_move()
            if pending is not None:
                try:
                    if chess.Move.from_uci(pending) not in self._state.legal_moves:
                        set_pending_move(None)
                        pending = None
                except ValueError:
                    set_pending_move(None)
                    pending = None

            # Always include the authoritative per-ply positions so the web
            # navigates and lists history by these server-computed FENs/SANs
            # instead of replaying the PGN in the browser. This is the single
            # source of truth for both variants: chess.js is no longer used on
            # the web, and it mis-computes Chess960 castling in any case.
            chess960 = bool(self._state.chess960)
            positions = self._state.history_positions()

            broadcast_game_state(
                fen=self._state.fen,
                pgn=self.get_pgn(),
                turn="w" if self._state.turn == chess.WHITE else "b",
                move_number=(len(move_stack) // 2) + 1,
                last_move=last_move,
                game_over=self._state.is_game_over,
                result=self._state.result,
                termination=self._state.termination,
                white=players.white_name,
                black=players.black_name,
                pending_move=pending,
                chess960=chess960,
                start_fen=self._state.start_fen,
                positions=positions,
            )
        except Exception as e:
            log.debug(f"[ChessGameService] Error broadcasting game state: {e}")


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[ChessGameService] = None


def get_chess_game_service() -> ChessGameService:
    """Get the singleton ChessGameService instance.
    
    Returns:
        The global ChessGameService instance.
    """
    global _instance
    if _instance is None:
        _instance = ChessGameService()
    return _instance
