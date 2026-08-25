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
from typing import Callable, Dict, List, Mapping, Optional, Tuple

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


def _parse_elo(value: object) -> Optional[int]:
    """Return ``value`` as an Elo when it is a number, else None.

    Deliberately does NOT dig a number out of a non-numeric string. It used to,
    because a strength selection is stored as a profile identity rather than a
    bare number and the seeded ladder spelled the Elo into that identity
    ("1200 ELO"). Identities are now generated, so digits inside one are not an
    Elo: pointed at ``Profile-1`` the old behaviour returned 1 and Auto sized the
    coach against a 1-rated opponent, silently and with no error. The Elo now
    comes from the profile's own values (see
    :func:`resolve_profile_elo_from_engine`); anything unresolvable is reported as
    unknown so callers fall back to :data:`DEFAULT_TARGET_ELO` instead of acting
    on a number that means nothing.
    """
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def resolve_profile_elo_from_engine(engine: str, selection: str) -> Optional[int]:
    """Look up the Elo an engine's strength profile plays, or None when unknown.

    The default resolver injected into :func:`resolve_opponent_elo`. Kept as a
    thin lazy indirection so this module stays importable without pulling in the
    engine services, and so tests can substitute a resolver instead of seeding a
    ``.uci`` file.
    """
    from universalchess.services.uci_schema import strength_elo_for_engine

    return strength_elo_for_engine(engine, selection)


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


def resolve_opponent_slot(
    player1: Mapping[str, object], player2: Mapping[str, object]
) -> Optional[Mapping[str, object]]:
    """Return the opponent (non-human) player's settings, or None.

    With one human, the opponent is the other player. With two engines, player two
    is used. With two humans there is no engine opponent.
    """
    p1_human = str(player1.get("type", "")) == "human"
    p2_human = str(player2.get("type", "")) == "human"
    if p1_human and not p2_human:
        return player2
    if p2_human and not p1_human:
        return player1
    if not p1_human and not p2_human:
        return player2
    return None


def resolve_opponent_elo(
    player1: Mapping[str, object],
    player2: Mapping[str, object],
    *,
    profile_elo: Optional[Callable[[str, str], Optional[int]]] = None,
) -> Optional[int]:
    """Return the opponent's numeric Elo for Auto coach matching, or None.

    A slot stores its strength as a profile identity, not a rating, so the rating
    has to be read out of that profile's own values -- which is why both the
    engine and the selection are needed, and why the lookup is a dependency
    (``profile_elo(engine, selection)``, defaulting to
    :func:`resolve_profile_elo_from_engine`) rather than an import.

    Returns None when there is no engine opponent, when the slot names no engine
    or strength, or when the profile plays uncapped -- all cases where no rating
    is known. Auto then falls back to :data:`DEFAULT_TARGET_ELO`; a number is
    never invented from the identity.

    Args:
        player1: Player one's settings mapping (``type``, ``engine``, ``elo``).
        player2: Player two's settings mapping.
        profile_elo: ``(engine, selection) -> Optional[int]`` lookup override.

    Returns:
        The opponent's Elo, or None when it cannot be determined.
    """
    slot = resolve_opponent_slot(player1, player2)
    if slot is None:
        return None
    selection = slot.get("elo")
    # A slot that stores a bare number (legacy configs, tests) needs no lookup.
    numeric = _parse_elo(selection)
    if numeric is not None:
        return numeric
    engine = str(slot.get("engine") or "").strip()
    if not engine or not str(selection or "").strip():
        return None
    lookup = profile_elo or resolve_profile_elo_from_engine
    return lookup(engine, str(selection).strip())


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
    "resolve_opponent_slot",
    "resolve_opponent_elo",
    "resolve_profile_elo_from_engine",
    "select_move_context",
    "resolve_persona",
]
