# Players Module

This module provides the player abstraction for chess games. Every game has
two players: one for White and one for Black. `PlayerType` is how a move is
sourced (`HUMAN`, `ENGINE`, `REMOTE`), not a catalog of providers.

- **HumanPlayer**: Moves come from the physical board
- **EnginePlayer**: Moves come from a UCI chess engine
- **HandBrainPlayer**: Catalog type that reports HUMAN or ENGINE by mode
- **LichessPlayer**: REMOTE provider plugin; moves come from a Lichess host

## Key Concepts

### Unified Move Flow

All players follow the same flow:

1. `request_move()` is called when it's the player's turn
2. Player receives piece events via `on_piece_event()`
3. Player forms a move from lift/place events
4. Player submits the move via `move_callback`
5. GameManager validates and executes (or enters correction mode)

The difference is in how each player processes piece events:

- **HumanPlayer**: Forms a move from any lift/place sequence and submits it.
  The move may be legal or illegal - GameManager validates.

- **EnginePlayer**: Has a computed pending move. Only submits if piece events
  match the pending move. If they don't match, the board needs correction.

- **LichessPlayer**: Has a pending move from the server. Only submits if piece
  events match. If they don't match, the board needs correction.

### Pending Move Callback

For Engine and Lichess players, when they have a move ready (computed or from
server), they call `pending_move_callback`. This is used for LED display to
show the user what move to execute on the physical board.

### Resignation

Any player can be resigned via the board interface if `can_resign()` returns True.
When a king is held off the board for 3 seconds, the resign menu appears for
that color (if that player can resign).

### Player Lifecycle

1. Create player(s)
2. Create `PlayerManager` with white and black players
3. Set callbacks: `move_callback`, `pending_move_callback`, `status_callback`
4. Call `player_manager.start()` to initialize all players
5. During game:
   - `request_move(board)` when it's a player's turn
   - `on_piece_event(type, square, board)` for each piece lift/place
6. Call `player_manager.stop()` when done

## Usage Examples

### Two-Player Mode (Human vs Human)

```python
from universalchess.players import HumanPlayer, PlayerManager

white = HumanPlayer()
black = HumanPlayer()
manager = PlayerManager(white, black, move_callback=on_move)
manager.start()

# On each turn
manager.request_move(board)

# Route piece events
manager.on_piece_event("lift", square, board)
manager.on_piece_event("place", square, board)
# Player submits move via callback
```

### Human vs Engine

```python
from universalchess.players import HumanPlayer, EnginePlayer, EnginePlayerConfig
import chess

# Human plays white
white = HumanPlayer()

# Engine plays black
config = EnginePlayerConfig(
    name="Stockfish",
    color=chess.BLACK,
    engine_name="stockfish",
    elo_section="1500",
    time_limit_seconds=5.0
)
black = EnginePlayer(config)

manager = PlayerManager(
    white, black,
    move_callback=on_move,
    pending_move_callback=on_pending_move  # For LED display
)
manager.start()

# When it's engine's turn, it computes a move
# pending_move_callback is called with the move for LED display
# Piece events confirm execution, then move is submitted via move_callback
```

### Lichess Online Game

```python
from universalchess.players import HumanPlayer
from universalchess.players.lichess import LichessPlayer, LichessPlayerConfig, LichessGameMode

# Create Lichess player for the remote opponent
lichess_config = LichessPlayerConfig(
    name="Lichess",
    mode=LichessGameMode.NEW,
    time_minutes=10,
    increment_seconds=5
)
lichess = LichessPlayer(lichess_config)
human = HumanPlayer()

manager = PlayerManager(
    human, lichess,
    move_callback=on_move,
    pending_move_callback=on_pending_move
)
manager.start()

# Lichess move arrives via stream, pending_move_callback shows LEDs
# Piece events confirm execution, move is submitted via move_callback
```

## Module Structure

- `base.py` - `Player` base class, `PlayerConfig`, `PlayerState`, `PlayerType` (`HUMAN` / `ENGINE` / `REMOTE`)
- `human.py` - `HumanPlayer` for physical board moves
- `engine.py` - `EnginePlayer` for UCI engine moves
- `hand_brain.py` - `HandBrainPlayer` (catalog type; HUMAN or ENGINE by mode)
- `lichess/` - Lichess provider plugin (`player`, `hosts`, `accounts`, `match`, `lobby`, `session`)
- `lichess/tests/` - plugin tests; host-socket tests stay in `src/universalchess/tests/`
- `manager.py` - `PlayerManager` to coordinate both players

## Design Notes

This module replaces the old `opponents/` module with a unified player
abstraction. Key design principles:

1. **All players are equal**: Every player receives piece events and submits
   moves via callback. No special cases.

2. **Move validation is centralized**: GameManager validates all moves,
   regardless of source. Players just submit moves.

3. **Pending moves for guidance**: Engine/Lichess players have pending moves
   that must be matched. The `pending_move_callback` enables LED display.

4. **Board-level concerns stay in GameManager**: Correction mode, resign
   detection, castling tracking are board features, not player features.
