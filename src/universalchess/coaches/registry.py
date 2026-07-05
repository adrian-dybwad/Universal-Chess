# Coaches Registry
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Discovery and selection for coaches.

Discovers coaches from two sources and merges them into one registry keyed by
coach id:

- Built-in coaches shipped in :mod:`universalchess.coaches.builtin` (every module
  is scanned, so a new built-in is just a new module).
- User coaches: any ``*.py`` in the user coaches folder (``USER_COACHES_DIR``,
  under the config directory). A user module with the same id as a built-in
  overrides it.

Security note: user discovery imports and executes user-provided Python with the
application's privileges -- the same trust level as installing an engine binary.
Only the device owner can place files in the folder. A user module that fails to
import is skipped with a logged warning so one bad file never breaks coaching.

Selection: :func:`resolve_coach` maps the ``coach_id`` setting to a coach. The
special value ``"auto"`` (and any unknown id) picks the coach whose Elo is closest
to the opponent's, so the coaching style matches the opposition strength.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil
import re
from typing import Dict, List, Mapping, Optional, Tuple

from universalchess.coaches.base import Coach, CoachingSituation, MoveContext

# Setting value selecting automatic, Elo-matched coach choice.
AUTO = "auto"

# Setting value turning coaching off entirely. Distinct from AUTO/an unknown id
# (which resolve to an Elo-matched coach): OFF resolves to *no* coach so the
# coaching feature is the single master switch on the Coach selector -- the agent
# only chooses which AI service powers an enabled coach, never whether it runs.
OFF = "off"

# Target Elo used for Auto selection when the opponent's Elo is unknown or
# non-numeric (e.g. "Default"). A mid-range value so the coach is neither the
# most basic nor the most advanced when strength cannot be determined.
DEFAULT_TARGET_ELO = 1200

_cache: Optional[Dict[str, Coach]] = None


def _log():
    """Lazy logger import so this module is safe to import anywhere."""
    from universalchess.board.logging import log

    return log


def user_coaches_dir() -> str:
    """Return the directory users drop custom coach modules into.

    Lives next to ``centaur.ini`` (the config directory) so it sits with other
    user configuration and is writable by the device owner.
    """
    from universalchess.board.settings import Settings

    return os.path.join(os.path.dirname(Settings.configfile), "coaches")


def _coach_classes_in(module) -> List[type]:
    """Return the Coach subclasses defined/exposed by a module (excluding base)."""
    classes = []
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Coach) and value is not Coach:
            classes.append(value)
    return classes


def _instantiate(cls: type) -> Optional[Coach]:
    """Instantiate a coach class, returning None (logged) on failure or blank id.

    A blank id cannot be selected or persisted, so such a class is skipped rather
    than silently shadowing another coach under an empty key.
    """
    try:
        coach = cls()
    except Exception as exc:  # a user coach ctor must not break discovery
        _log().warning(f"[Coaches] Failed to instantiate {cls.__name__}: {exc}")
        return None
    if not getattr(coach, "id", ""):
        _log().warning(f"[Coaches] Skipping {cls.__name__}: missing id")
        return None
    return coach


def _discover_builtin(registry: Dict[str, Coach]) -> None:
    """Load every built-in coach module and register its coaches."""
    from universalchess.coaches import builtin

    for module_info in pkgutil.iter_modules(builtin.__path__):
        name = module_info.name
        if name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{builtin.__name__}.{name}")
        except Exception as exc:  # a broken built-in must not break the rest
            _log().warning(f"[Coaches] Failed to import built-in '{name}': {exc}")
            continue
        for cls in _coach_classes_in(module):
            coach = _instantiate(cls)
            if coach is not None:
                registry[coach.id] = coach


def _discover_user(registry: Dict[str, Coach], user_dir: str) -> None:
    """Load user coach modules from ``user_dir``; user ids override built-ins."""
    if not user_dir or not os.path.isdir(user_dir):
        return
    for entry in sorted(os.listdir(user_dir)):
        if not entry.endswith(".py") or entry.startswith("_"):
            continue
        path = os.path.join(user_dir, entry)
        stem = entry[:-3]
        try:
            spec = importlib.util.spec_from_file_location(
                f"universalchess.coaches._user_{stem}", path
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # executes user code by design
        except Exception as exc:  # one bad file must not break coaching
            _log().warning(f"[Coaches] Failed to load user coach '{entry}': {exc}")
            continue
        for cls in _coach_classes_in(module):
            coach = _instantiate(cls)
            if coach is not None:
                registry[coach.id] = coach


def discover_coaches(
    *, user_dir: Optional[str] = None, include_user: bool = True
) -> Dict[str, Coach]:
    """Build the coach registry (built-in + user), keyed by id.

    Args:
        user_dir: Override the user coaches directory (for tests). Defaults to
            :func:`user_coaches_dir`.
        include_user: When False, only built-in coaches are loaded.

    Returns:
        A fresh dict mapping coach id to a Coach instance. User coaches override
        built-ins sharing an id.
    """
    registry: Dict[str, Coach] = {}
    _discover_builtin(registry)
    if include_user:
        _discover_user(registry, user_dir if user_dir is not None else user_coaches_dir())
    return registry


def get_registry() -> Dict[str, Coach]:
    """Return the cached registry, discovering it on first use."""
    global _cache
    if _cache is None:
        _cache = discover_coaches()
    return _cache


def refresh() -> None:
    """Drop the cached registry so the next access re-discovers (tests/new files)."""
    global _cache
    _cache = None


def list_coaches() -> List[Dict[str, object]]:
    """Return coach display info, sorted by Elo then name, for the selector."""
    coaches = get_registry().values()
    ordered = sorted(coaches, key=lambda c: (c.elo, c.name))
    return [c.get_info() for c in ordered]


def get_coach(coach_id: str) -> Optional[Coach]:
    """Return the coach with ``coach_id``, or None if unknown."""
    return get_registry().get(coach_id)


# First run of digits in a section name. The engine strength selection is stored
# as a section name, not a bare number (there are no shipped .uci files); the
# seeded ELO ladder names them "<n> ELO" and a Maia net level is named from its
# filename (e.g. "maia-1500..."), so the strength is the embedded number.
_ELO_IN_TEXT = re.compile(r"\d+")


def _parse_elo(value: object) -> Optional[int]:
    """Derive a numeric Elo from a strength selection, or None when unknown.

    Accepts a bare number and also the section-name form the app actually stores
    (e.g. ``"1200 ELO"`` from the seeded ladder, or a Maia net level whose name
    carries the strength). Names without a number (e.g. ``"Default"`` or a custom
    personality like ``"Attacker"``) return None, so Auto coach selection falls
    back to :data:`DEFAULT_TARGET_ELO` rather than guessing.
    """
    if value is None:
        return None
    text = str(value).strip()
    try:
        # A bare number (or numeric string) is the Elo directly.
        return int(float(text))
    except (TypeError, ValueError):
        # Not a plain number: fall back to the first digits embedded in the name
        # (e.g. "1200 ELO", "maia-1100.pb.gz"), else unknown.
        match = _ELO_IN_TEXT.search(text)
        return int(match.group()) if match else None


def resolve_coach(coach_id_setting: str, opponent_elo: object) -> Optional[Coach]:
    """Resolve the active coach from the setting and the opponent's Elo.

    ``"off"`` turns coaching off and resolves to None regardless of the roster, so
    the Coach selector is the master on/off switch. An explicit, known id selects
    that coach. ``"auto"`` (or an unknown id) picks the coach whose Elo is closest
    to the opponent's, tie-breaking toward the lower Elo then name for deterministic
    selection. When the opponent Elo is unknown or non-numeric,
    :data:`DEFAULT_TARGET_ELO` is used. Returns None when coaching is off or no
    coaches are registered.
    """
    if coach_id_setting == OFF:
        return None

    registry = get_registry()
    if not registry:
        return None

    if coach_id_setting and coach_id_setting != AUTO and coach_id_setting in registry:
        return registry[coach_id_setting]

    target = _parse_elo(opponent_elo)
    if target is None:
        target = DEFAULT_TARGET_ELO
    return min(
        registry.values(),
        key=lambda c: (abs(c.elo - target), c.elo, c.name),
    )


def resolve_coach_info(coach_id_setting: str, opponent_elo: object) -> Optional[Dict[str, object]]:
    """Return the resolved coach's display info (for showing the Auto choice)."""
    coach = resolve_coach(coach_id_setting, opponent_elo)
    return coach.get_info() if coach is not None else None


def resolve_human_color(
    player1: Mapping[str, object], player2: Mapping[str, object]
) -> Optional[str]:
    """Return the human player's color, or None when there is no single human.

    Used to decide whether a played move is the player's own (Player persona) or
    the opponent's (Opponent persona). Returns None for engine-vs-engine or two
    humans, where there is no single "player" perspective.
    """
    p1_human = str(player1.get("type", "")) == "human"
    p2_human = str(player2.get("type", "")) == "human"
    if p1_human and not p2_human:
        return str(player1.get("color", "")) or None
    if p2_human and not p1_human:
        return str(player2.get("color", "")) or None
    return None


def resolve_opponent_elo(
    player1: Mapping[str, object], player2: Mapping[str, object]
) -> Optional[object]:
    """Return the opponent (non-human) player's strength selection for Auto coach.

    With one human, the opponent is the other player. With two engines, player two
    is used. With two humans there is no engine opponent, so None is returned and
    Auto falls back to the default target Elo.

    The returned value is the stored strength *selection* (a section name such as
    ``"1200 ELO"``, not a bare number). :func:`_parse_elo` -- the single place
    that turns a selection into a number -- derives the numeric Elo from it, so
    the seeded ELO ladder and Maia net levels both resolve to a target strength.
    """
    p1_human = str(player1.get("type", "")) == "human"
    p2_human = str(player2.get("type", "")) == "human"
    if p1_human and not p2_human:
        return player2.get("elo")
    if p2_human and not p1_human:
        return player1.get("elo")
    if not p1_human and not p2_human:
        return player2.get("elo")
    return None


def select_move_context(
    is_potential_move: bool, side_to_move: str, human_color: Optional[str]
) -> MoveContext:
    """Choose the persona context for a move.

    A hint (potential move) is always the player's; a played move is the player's
    when the side that moved is the human's color, otherwise the opponent's. With
    no known human color, played moves are treated as the opponent's.
    """
    if is_potential_move:
        return MoveContext.PLAYER_MOVE
    if human_color and side_to_move == human_color:
        return MoveContext.PLAYER_MOVE
    return MoveContext.OPPONENT_MOVE


def resolve_persona(
    coach_id_setting: str,
    opponent_elo: object,
    *,
    human_color: Optional[str],
    is_potential_move: bool,
    side_to_move: str,
    fen_before: Optional[str] = None,
    move_text: Optional[str] = None,
    facts: Tuple[str, ...] = (),
    eval_before_cp: Optional[int] = None,
    eval_after_cp: Optional[int] = None,
    move_number: Optional[int] = None,
) -> Optional[str]:
    """Resolve the persona text for a move, or None when no coach is available.

    Combines coach selection (:func:`resolve_coach`) with the move-context choice
    (:func:`select_move_context`) and asks the coach for its persona. The optional
    position fields are passed through on the situation for coaches that vary their
    text by position; the built-in coaches ignore them.
    """
    coach = resolve_coach(coach_id_setting, opponent_elo)
    if coach is None:
        return None
    situation = CoachingSituation(
        move_context=select_move_context(is_potential_move, side_to_move, human_color),
        is_potential_move=is_potential_move,
        side_to_move=side_to_move,
        human_color=human_color,
        fen_before=fen_before,
        move_text=move_text,
        facts=facts,
        eval_before_cp=eval_before_cp,
        eval_after_cp=eval_after_cp,
        move_number=move_number,
    )
    return coach.persona(situation)


__all__ = [
    "AUTO",
    "OFF",
    "DEFAULT_TARGET_ELO",
    "user_coaches_dir",
    "discover_coaches",
    "get_registry",
    "refresh",
    "list_coaches",
    "get_coach",
    "resolve_coach",
    "resolve_coach_info",
    "resolve_human_color",
    "resolve_opponent_elo",
    "select_move_context",
    "resolve_persona",
]
