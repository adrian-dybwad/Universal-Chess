# Assistants Module

Assistants help the user play by providing suggestions, hints, or guidance without making moves themselves.

## Architecture

All assistants inherit from the `Assistant` abstract base class:

```python
class Assistant(ABC):
    def start(self) -> bool: ...
    def stop(self) -> None: ...
    def get_suggestion(self, board: chess.Board, for_color: chess.Color) -> Optional[Suggestion]: ...
    def on_player_move(self, move: chess.Move, board: chess.Board) -> None: ...
    def on_new_game(self) -> None: ...
```

## Suggestion Types

Assistants provide suggestions via the `Suggestion` dataclass:

```python
class SuggestionType(Enum):
    PIECE_TYPE   # Which piece type to move (Hand+Brain)
    MOVE         # Specific move suggestion (hints)
    SQUARES      # Squares to highlight
    EVALUATION   # Position evaluation
    TEXT         # Text advice
```

## Built-in Assistants

### HandBrainAssistant
In Hand+Brain mode, the engine suggests which piece type to move.

```python
from universalchess.assistants import create_hand_brain_assistant

assistant = create_hand_brain_assistant(engine_name="stockfish")
assistant.set_suggestion_callback(on_suggestion)
assistant.start()

# When it's player's turn, pass the player's color:
player_color = chess.WHITE
assistant.get_suggestion(board, for_color=player_color)  # Delivers suggestion via callback
```

### HintAssistant
Provides move hints on demand (HELP button).

```python
from universalchess.assistants import create_hint_assistant

assistant = create_hint_assistant()
assistant.set_analysis_callback(get_best_move_from_analysis)
assistant.start()

# When player presses HELP, pass the player's color:
player_color = chess.WHITE
suggestion = assistant.get_suggestion(board, for_color=player_color)
if suggestion:
    show_hint_leds(suggestion.squares)
```

For puzzles with known solutions:

```python
assistant.set_predefined_hint(from_sq=12, to_sq=28)  # e2e4
```

## Creating Custom Assistants

```python
from universalchess.assistants import Assistant, AssistantConfig, Suggestion

class CoachAssistant(Assistant):
    def start(self) -> bool:
        self._active = True
        return True
    
    def stop(self) -> None:
        self._active = False
    
    def get_suggestion(self, board: chess.Board, for_color: chess.Color) -> Optional[Suggestion]:
        # Analyze position and provide advice for the specified color
        if self._is_blunder_position(board, for_color):
            return Suggestion.advice("Consider protecting your queen!")
        return None
    
    def on_player_move(self, move: chess.Move, board: chess.Board) -> None:
        # Evaluate the move quality
        if self._was_bad_move(move, board):
            self._report_suggestion(Suggestion.advice("That was a mistake!"))
    
    def on_new_game(self) -> None:
        pass
```

## Auto-Suggest vs On-Demand

- **Auto-suggest** (`auto_suggest=True`): Suggestions provided automatically when it's the player's turn. Used for Hand+Brain mode.
- **On-demand** (`auto_suggest=False`): Suggestions only when explicitly requested. Used for hints.

## Combining with players

Assistants can be used alongside any player:

```python
from universalchess.assistants import create_hand_brain_assistant
from universalchess.players.lichess import LichessPlayer, LichessPlayerConfig, LichessGameMode

# Play against Lichess with Hand+Brain assistance
remote = LichessPlayer(LichessPlayerConfig(mode=LichessGameMode.NEW))
assistant = create_hand_brain_assistant(player_color=chess.WHITE)

remote.start()
assistant.start()
```

This allows mixing and matching players and assistants for various play modes.
