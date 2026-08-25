"""Find and repair the settings references to an engine's strength profiles.

A strength profile is a section in ``config/engines/<engine>.uci``, and three
settings keys in ``centaur.ini`` point at one *by section name*: each player
slot's ``elo`` and the Centaur engine proxy's ``level``. Nothing enforces that
relationship, so renaming, deleting, or re-seeding a profile leaves those keys
naming a section that no longer exists -- and the failure is silent:
``players.engine.EnginePlayer._load_uci_options`` finds no matching section and
falls back to the engine-wide ``[DEFAULT]``, so the board plays at a strength
nobody chose, with no error at any layer.

This module owns the only knowledge of *where* those references live, so a
fourth referrer is added in one place rather than discovered by a later bug.
:func:`repair_dangling` repoints references whose target is gone, and reports
what it changed so the caller can tell the user at the moment of the action
instead of leaving it to be discovered mid-game. There is no rename repair
because a profile's identity is a generated id that never changes: renaming means
editing the profile's ``Name``, which no reference depends on.

Kept out of :mod:`universalchess.services.engine_profiles`, which stays a pure
``.uci`` file module with no coupling to the settings store. The settings reads
and writes are injected (defaulting to
:mod:`universalchess.utils.settings_persistence`) so the repair is testable
without a config file on disk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from universalchess.services.centaur_engine_proxy.config import (
    CONFIG_SECTION as CENTAUR_SECTION,
    ENGINE_KEY as CENTAUR_ENGINE_KEY,
    LEVEL_KEY as CENTAUR_LEVEL_KEY,
    OPTIONS_KEY as CENTAUR_OPTIONS_KEY,
)
from universalchess.services.engine_profiles import SEEDED_DEFAULT_PROFILE
from universalchess.players.settings import PLAYER1_SECTION, PLAYER2_SECTION

__all__ = [
    "ReferenceSite",
    "ProfileReference",
    "REFERENCE_SITES",
    "find_references",
    "repair_dangling",
]

# Reads ``(section, key, default) -> str``, matching ``Settings.read``.
ReadSetting = Callable[[str, str, str], str]
# Writes ``(section, key, value) -> bool``, matching ``save_setting``.
WriteSetting = Callable[[str, str, Any], bool]
# Resolves ``(engine, profile) -> {uci_option: value}`` for sites that cache the
# profile's resolved options alongside the reference.
ResolveOptions = Callable[[str, str], Dict[str, Any]]


@dataclass(frozen=True)
class ReferenceSite:
    """One settings location that names an engine and one of its profiles.

    A reference is only to *this* engine's profile when ``engine_key`` holds the
    engine in question, so both keys are needed to interpret either: two slots on
    different engines can store the same profile name meaning different sections.

    Attributes:
        section: ``centaur.ini`` section holding both keys.
        engine_key: Key naming the engine the profile belongs to.
        profile_key: Key holding the profile (``.uci`` section) name.
        options_key: Key caching the profile's resolved UCI options as a JSON
            object, or None when the site stores no such cache. The Centaur proxy
            reads its options from this cache rather than re-resolving the level,
            so repointing the level without refreshing the cache would leave the
            proxy applying the removed profile's options.
    """

    section: str
    engine_key: str
    profile_key: str
    options_key: Optional[str] = None


# Every place a profile name is stored. Player slots keep the engine and its
# strength in the same section; the Centaur card mirrors that shape and adds the
# resolved-options cache the proxy actually launches with.
REFERENCE_SITES: Tuple[ReferenceSite, ...] = (
    ReferenceSite(section=PLAYER1_SECTION, engine_key="engine", profile_key="elo"),
    ReferenceSite(section=PLAYER2_SECTION, engine_key="engine", profile_key="elo"),
    ReferenceSite(
        section=CENTAUR_SECTION,
        engine_key=CENTAUR_ENGINE_KEY,
        profile_key=CENTAUR_LEVEL_KEY,
        options_key=CENTAUR_OPTIONS_KEY,
    ),
)


@dataclass(frozen=True)
class ProfileReference:
    """A stored reference to a profile, and what it was repointed to.

    ``profile`` is the value found in the settings and ``repointed_to`` is the
    value written in its place (None when only listing). Both are carried so the
    caller can report the change it made without re-reading the settings.
    """

    site: ReferenceSite
    engine: str
    profile: str
    repointed_to: Optional[str] = None

    @property
    def description(self) -> str:
        """``section.key`` for the response payload and log lines."""
        return f"{self.site.section}.{self.site.profile_key}"


def _default_read() -> ReadSetting:
    from universalchess.utils.settings_persistence import load_str

    return load_str


def _default_write() -> WriteSetting:
    from universalchess.utils.settings_persistence import save_setting

    return save_setting


def _same_profile(stored: str, name: str) -> bool:
    """Whether a stored reference names ``name``.

    Case-insensitive because the ``.uci`` readers resolve a section that way (see
    ``engine_profiles.resolve_section``): a slot storing ``1200 elo`` resolves to
    a legacy section ``1200 ELO`` at game start, so deleting that section must be
    recognised as affecting the slot.
    """
    return stored.strip().casefold() == name.strip().casefold()


def find_references(
    engine: str,
    profile: str,
    *,
    read_setting: Optional[ReadSetting] = None,
) -> List[ProfileReference]:
    """Return every stored reference to ``(engine, profile)``.

    Args:
        engine: Engine executable name the profile belongs to.
        profile: Profile (``.uci`` section) name being referenced.
        read_setting: ``(section, key, default) -> str`` reader; defaults to the
            ``centaur.ini`` reader.

    Returns:
        One :class:`ProfileReference` per site that names this engine and
        profile, in :data:`REFERENCE_SITES` order. Empty when nothing points at
        it.
    """
    read = read_setting or _default_read()
    if not engine or not profile:
        return []
    found: List[ProfileReference] = []
    for site in REFERENCE_SITES:
        stored_engine = read(site.section, site.engine_key, "").strip()
        if stored_engine != engine:
            continue
        stored_profile = read(site.section, site.profile_key, "")
        if _same_profile(stored_profile, profile):
            found.append(
                ProfileReference(site=site, engine=engine, profile=stored_profile)
            )
    return found


def _repoint(
    references: Iterable[ProfileReference],
    target: str,
    *,
    write_setting: Optional[WriteSetting] = None,
    resolve_options: Optional[ResolveOptions] = None,
) -> List[ProfileReference]:
    """Write ``target`` over each reference, refreshing option caches.

    A site with an ``options_key`` only has its cache rewritten when a resolver
    is supplied; without one the stale cache is left alone rather than being
    replaced with an empty map, since an empty map means "the engine's own
    defaults" and would silently change how the proxy plays.
    """
    write = write_setting or _default_write()
    changed: List[ProfileReference] = []
    for reference in references:
        site = reference.site
        write(site.section, site.profile_key, target)
        if site.options_key and resolve_options is not None:
            write(
                site.section,
                site.options_key,
                json.dumps(resolve_options(reference.engine, target)),
            )
        changed.append(
            ProfileReference(
                site=site,
                engine=reference.engine,
                profile=reference.profile,
                repointed_to=target,
            )
        )
    return changed


def repair_dangling(
    engine: str,
    existing_profiles: Sequence[str],
    *,
    fallback: str = SEEDED_DEFAULT_PROFILE,
    read_setting: Optional[ReadSetting] = None,
    write_setting: Optional[WriteSetting] = None,
    resolve_options: Optional[ResolveOptions] = None,
) -> List[ProfileReference]:
    """Repoint references to ``engine`` profiles that no longer exist.

    Uniform across delete and re-seed: rather than assuming which profile a
    mutation removed, this compares each stored reference against the profiles
    that exist *now*, so exactly the references that would otherwise fall back to
    ``[DEFAULT]`` are repaired and the rest are left untouched. A reset that
    happens to re-derive the same ladder therefore changes nothing.

    Args:
        engine: Engine executable name whose profiles were mutated.
        existing_profiles: The profile names present after the mutation.
        fallback: Profile to repoint to. ``Default`` exists for every probed
            engine (seeded by ``uci_schema.derive_sections``), so it is the only
            name guaranteed to resolve.
        read_setting: ``(section, key, default) -> str`` reader.
        write_setting: ``(section, key, value) -> bool`` writer.
        resolve_options: ``(engine, profile) -> options`` for sites caching
            resolved options.

    Returns:
        The references that were repointed, each carrying ``repointed_to``.
    """
    read = read_setting or _default_read()
    if not engine:
        return []
    existing = {name.strip().casefold() for name in existing_profiles}
    dangling: List[ProfileReference] = []
    for site in REFERENCE_SITES:
        stored_engine = read(site.section, site.engine_key, "").strip()
        if stored_engine != engine:
            continue
        stored_profile = read(site.section, site.profile_key, "").strip()
        if not stored_profile or stored_profile.casefold() in existing:
            continue
        dangling.append(
            ProfileReference(site=site, engine=engine, profile=stored_profile)
        )
    return _repoint(
        dangling,
        fallback,
        write_setting=write_setting,
        resolve_options=resolve_options,
    )
