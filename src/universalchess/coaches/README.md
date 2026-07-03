# Coaches Framework

A **coach** is a named persona that supplies the tone, focus, and instructions the
AI adopts when explaining a move. Coaches shape *how* the AI coach talks; the AI
provider/key configured separately (OpenAI, Anthropic, custom) is only the backend
that generates the text.

Coaches are Python plugins:

- **Built-in coaches** ship in `coaches/builtin/` (one module per coach).
- **User coaches** are dropped into the user coaches folder (next to
  `centaur.ini`, at `<config>/coaches/`) and picked up automatically.

## Architecture

Every coach subclasses `Coach` and provides display metadata plus two personas
(one for the player's own moves/hints, one for the opponent's moves):

```python
from universalchess.coaches.base import Coach

class Coach:
    id: str = ""                      # stable, unique, lowercase slug
    name: str = ""                    # shown in the selector
    elo: int = 0                      # target strength (drives Auto selection)
    character_type: str = ""          # short label, e.g. "Socratic Coach"
    description: str = ""             # one-line style description
    player_move_persona: str = ""     # used for the human's own move / a hint
    opponent_move_persona: str = ""   # used for a move played by the opponent

    def persona(self, situation: CoachingSituation) -> str: ...
    def get_info(self) -> dict: ...
```

The default `persona()` selects between the two persona strings by the move
context. The composed prompt is always the persona **plus fixed guardrails**
(brevity for the board's small display and the no-invented-tactics rule); a coach
shapes tone but can never relax those guardrails.

### The coaching situation

`persona()` receives a `CoachingSituation` describing the move being coached:

```python
@dataclass
class CoachingSituation:
    move_context: MoveContext          # PLAYER_MOVE or OPPONENT_MOVE
    is_potential_move: bool            # True for a hint the player is considering
    side_to_move: str                  # "white" / "black"
    human_color: Optional[str]         # human's color, or None (engine vs engine)
    fen_before: Optional[str]          # position before the move (best-effort)
    move_text: Optional[str]           # move in the user's notation
    facts: Tuple[str, ...]             # verified move facts (captures/checks/pins)
    eval_before_cp / eval_after_cp     # eval swing, white's perspective (centipawns)
    move_number: Optional[int]
```

Simple coaches ignore everything except `move_context` (handled by the default
`persona()`); a position-aware coach overrides `persona()` and reads the rest.

## Built-in coaches

Seeded from a beginner-to-expert taxonomy (weakest first):

| id       | name   | Elo  | character type   |
| -------- | ------ | ---- | ---------------- |
| `dave`   | Dave   | 800  | Guarded Mentor   |
| `myron`  | Myron  | 1250 | Socratic Coach   |
| `sofia`  | Sofia  | 1750 | Silent Partner   |
| `viktor` | Viktor | 2200 | Engine Oracle    |

## Selecting a coach

The `coach_id` game setting chooses the coach:

- A specific id (e.g. `"myron"`) always uses that coach.
- `"auto"` (the default) picks the coach whose Elo is closest to the opponent's,
  so the coaching style matches the opposition. When the opponent's Elo is unknown
  or non-numeric, a mid-range default target is used.

## Creating a custom coach

Create a module in `<config>/coaches/` (for example `grumpy.py`):

```python
from universalchess.coaches.base import Coach

class Grumpy(Coach):
    id = "grumpy"
    name = "Grumpy"
    elo = 1500
    character_type = "Blunt Veteran"
    description = "Terse, no-nonsense feedback."

    player_move_persona = (
        "You are Grumpy, a blunt veteran coach. Point out the single most "
        "important issue with the player's idea. No pleasantries."
    )
    opponent_move_persona = (
        "You are Grumpy. State plainly what the opponent just threatened and what "
        "the player must deal with. No filler."
    )
```

It appears in the coach selector automatically. A user coach whose `id` matches a
built-in **overrides** that built-in, so you can customize a shipped coach without
editing the package.

For position-aware behavior, override `persona()`:

```python
from universalchess.coaches.base import Coach, CoachingSituation, MoveContext

class Adaptive(Coach):
    id = "adaptive"
    name = "Adaptive"
    elo = 1600
    character_type = "Situational"
    description = "Sharpens up when the eval swings."

    def persona(self, situation: CoachingSituation) -> str:
        swing_is_large = (
            situation.eval_before_cp is not None
            and situation.eval_after_cp is not None
            and abs(situation.eval_after_cp - situation.eval_before_cp) >= 150
        )
        if situation.move_context is MoveContext.PLAYER_MOVE:
            if swing_is_large:
                return "You are Adaptive. That move changed the evaluation sharply; explain the key consequence."
            return "You are Adaptive. Briefly confirm the plan behind the move."
        return "You are Adaptive. Explain what the opponent's move threatens."
```

## Notes

- **Security**: user coach discovery imports and executes user-provided Python
  with the application's privileges -- the same trust level as installing an
  engine binary. Only the device owner can place files in the folder. A user
  module that fails to import is skipped with a logged warning, so one bad file
  never breaks coaching.
- Coaches contain no I/O and no network calls; the persona string is the coach's
  only work product. The service (`services/coach.py`) composes it with the
  guardrails and calls the configured provider.
