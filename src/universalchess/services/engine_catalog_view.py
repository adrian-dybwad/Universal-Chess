"""The engine list both surfaces render, decided once.

The board and the web each used to build their own list from the shared catalog.
Everything above the catalog -- order, grouping, whether this device can install
an engine, what is wrong with one -- was written twice, and drifted: the board
sorted alphabetically with no strength groups, never consulted architecture
support, and showed neither custom engines nor repair state.

This module makes those decisions once and returns them as plain data. It follows
the shape of the menu system, where one catalog is read in-process by the board
and served to the web as a prepared projection: the server decides, the clients
draw. The difference is medium, not principle -- a menu is static enough to be a
file, whereas this list is computed per call from install state on disk.

Every dependency is injected. Nothing here imports Flask, the e-paper stack, or
the real ``/opt`` paths, so both callers and the tests supply their own.

Deliberately excluded: fields only one surface renders -- documentation links,
the ref picker, profile readiness, net top-up counts. Each costs disk reads or
network per engine, and making the board pay for them to redraw an e-paper menu
would trade one real problem for another. The endpoint adds them on top of these
rows. The boundary is what both render, not everything either renders.
"""

from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Protocol, Sequence

from universalchess.managers.engine_manager import (
    arch_unsupported_reason,
    catalog_engines_by_strength,
)

# Group shown for an operator-added engine. It carries no rating, and the
# surfaces list custom engines in their own section rather than in a strength
# band, but the field is set so every row has the same shape.
CUSTOM_ENGINE_TIER = "specialty"


class InstallStateReader(Protocol):
    """The part of the engine manager this view needs.

    Narrowed to four questions so a caller -- or a test -- can answer them
    without constructing the install machinery.
    """

    def is_installed(self, engine_name: str) -> bool: ...

    def needs_repair(self, engine_name: str) -> bool: ...


@dataclass(frozen=True)
class EngineRow:
    """One entry in the engine list, as both surfaces render it.

    Frozen because it is a decision already made: a renderer that could adjust a
    row would be deciding presentation again, which is what this module exists to
    prevent.
    """

    name: str
    display_name: str
    summary: str
    description: str
    # Strength band the row is grouped under, derived from the rating.
    tier: str
    # Published rating, or None for the unrated and for custom engines.
    elo: Optional[int]
    installed: bool
    # Installed but unusable -- a net-backed engine whose weights are missing.
    # Distinct from not installed: the answer is Repair, not Install.
    needs_repair: bool
    # Whether THIS device can install it, and why not when it cannot.
    supported: bool
    unsupported_reason: Optional[str]
    is_system_package: bool
    can_uninstall: bool
    estimated_install_minutes: int
    # A stopped or restart-killed install whose build tree survives, or None.
    # Carried per engine because several can be paused at once.
    resume_point: Optional[Any]
    # The last install failure as the caller chose to expose it, or None.
    last_failure: Optional[Mapping[str, Any]]
    is_custom: bool


def build_engine_rows(
    *,
    engine_manager: InstallStateReader,
    arch: str,
    has_neon: bool,
    resume_store,
    custom_store,
    failure_payload: Callable[[str], Optional[Mapping[str, Any]]],
    custom_binary_installed: Callable[[Any], bool],
) -> List[EngineRow]:
    """Build the engine list: catalog engines strongest first, then custom ones.

    Args:
        engine_manager: Answers install and repair state per engine.
        arch: This device's architecture token, from ``get_current_arch()``.
            The coarse 'arm64'/'armhf' vocabulary, not ``platform.machine()``
            spelling -- the catalog's ``supported_archs`` sets use the former,
            so passing 'aarch64' would find every engine unsupported.
        has_neon: Whether the CPU has NEON. Read separately because one
            architecture token spans CPUs that have it and CPUs that do not.
        resume_store: Source of paused installs; ``list_all()`` is called once
            rather than a lookup per engine, since this renders the whole
            catalog at a time.
        custom_store: Registry of operator-added engines.
        failure_payload: Returns an engine's last failure in whatever form the
            caller is willing to expose. Injected rather than read here because
            the web redacts it -- the endpoint is reachable unauthenticated and
            the raw text carries absolute paths -- while the board shows a local
            user their own device's error.
        custom_binary_installed: Whether a custom engine's binary is present.
            The caller owns this because resolving the path safely is its
            concern: the id comes from a registry file and must be resolved
            through the containment guard, not joined onto a directory here.

    Returns:
        Catalog engines ordered strongest first, then the custom engines in
        registry order.
    """
    resume_points = resume_store.list_all()

    rows = [
        _catalog_row(
            name=name,
            engine=engine,
            engine_manager=engine_manager,
            arch=arch,
            has_neon=has_neon,
            resume_point=resume_points.get(name),
            last_failure=failure_payload(name),
        )
        for name, engine in catalog_engines_by_strength()
    ]
    rows.extend(
        _custom_rows(custom_store.list(), custom_binary_installed, failure_payload)
    )
    return rows


def _catalog_row(
    *, name, engine, engine_manager, arch, has_neon, resume_point, last_failure
) -> EngineRow:
    """Build the row for one catalog engine."""
    # "Installed" here means present enough to show as installed rather than
    # offer a fresh Install: a system package is always present, any other engine
    # counts once its binary exists. An engine whose nets are missing is still
    # installed -- it surfaces Repair, not Install -- but is not usable for play.
    installed = engine.is_system_package or engine_manager.is_installed(name)
    unsupported_reason = arch_unsupported_reason(engine, arch, has_neon=has_neon)

    return EngineRow(
        name=name,
        display_name=engine.display_name,
        summary=engine.summary,
        description=engine.description,
        tier=engine.tier,
        elo=engine.elo,
        installed=installed,
        needs_repair=engine_manager.needs_repair(name),
        supported=unsupported_reason is None,
        unsupported_reason=unsupported_reason,
        is_system_package=engine.is_system_package,
        can_uninstall=engine.can_uninstall,
        estimated_install_minutes=engine.estimated_install_minutes,
        resume_point=resume_point,
        last_failure=last_failure,
        is_custom=False,
    )


def _custom_rows(
    customs: Sequence[Any], binary_installed, failure_payload
) -> List[EngineRow]:
    """Build the rows for operator-added engines, in registry order.

    They declare no architecture, so nothing excludes them: the support gate
    reads ``supported_archs`` off a catalog definition a custom engine does not
    have, and defaulting it to unsupported would hide the operator's own binary
    behind a reason that does not apply to it. They expose no repair path and are
    always uninstallable.

    They do carry a failure: adding a custom engine by URL can fail the same way
    a catalog install can, and the operator needs to see why.
    """
    return [
        EngineRow(
            name=custom.id,
            display_name=custom.display_name,
            summary="Custom engine",
            description=(
                "Uploaded engine binary."
                if custom.source == "upload"
                else f"Installed from {custom.url}"
            ),
            tier=CUSTOM_ENGINE_TIER,
            elo=None,
            installed=binary_installed(custom),
            needs_repair=False,
            supported=True,
            unsupported_reason=None,
            is_system_package=False,
            can_uninstall=True,
            estimated_install_minutes=0,
            resume_point=None,
            last_failure=failure_payload(custom.id),
            is_custom=True,
        )
        for custom in customs
    ]
