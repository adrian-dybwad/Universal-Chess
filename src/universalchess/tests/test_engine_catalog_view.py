"""The one engine list both surfaces render.

Background / why these tests exist
----------------------------------
The board and the web each built their own engine list. They shared the catalog
underneath and nothing above it, so every rule about how the list is *presented*
had to be written twice -- and three of them only ever got written once.

The board's list sorted installed-first then alphabetically, so it had no notion
of strength groups at all: the catalog's strongest engine sat in the middle of
the list while leading it on the web. It never consulted architecture support, so
it offered a normal Install row for an engine that cannot run on this CPU; the
install is refused up front, but only after the user presses it. Operator-added
custom engines did not appear on the board at all. And a broken net-backed engine
looked identical to a healthy one until you opened it.

None of that was a decision. It is what two independent builders drift into, and
the board's had no test at all.

So the decisions -- which engines appear, in what order, in which group, whether
this device can install them, and what is wrong with them -- are made once, here,
and both surfaces render the result. This mirrors the menu system, where one
catalog is read in-process by the board and served to the web as a prepared
projection: the server decides, the clients draw.

What is deliberately NOT shared: fields only one surface renders (the ref picker,
profile readiness, documentation links). Computing those costs disk reads per
engine, and making the board pay for them to redraw an e-paper menu would trade
one real problem for another. The boundary is "what both render", not "everything
either renders".
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional

import pytest

from universalchess.managers.engine_manager import ENGINES
from universalchess.services.engine_catalog_view import build_engine_rows

# The two tokens get_current_arch() produces. Deliberately not 'aarch64' or
# 'armv7l': those are the platform.machine() spellings, which the catalog's
# supported_archs sets do not use, so a test written against them would find
# every engine unsupported everywhere and prove nothing.
ARCH_64 = "arm64"

# 32-bit ARM, which four catalog engines decline. Used to prove the support gate
# reaches the row rather than being computed and dropped.
ARCH_32 = "armhf"

# An engine every arch supports, used where the test is not about support.
SUPPORTED_ENGINE = "stockfish"


@dataclass(frozen=True)
class _ResumePoint:
    """Stands in for a paused install's record; only the fields a row carries."""

    engine: str
    ref: str
    percent: int


class _FakeResumeStore:
    """Paused installs, keyed by engine, as the real store reports them."""

    def __init__(self, points: Optional[Dict[str, _ResumePoint]] = None):
        self._points = points or {}

    def list_all(self) -> Dict[str, _ResumePoint]:
        return dict(self._points)


class _FakeEngineManager:
    """Install and repair state, without touching /opt.

    Defaults to "nothing installed, nothing broken" so each test states only the
    condition it is about.
    """

    def __init__(self, installed=(), needs_repair=(), missing_nets=None):
        self._installed = set(installed)
        self._needs_repair = set(needs_repair)
        self._missing_nets = missing_nets or {}

    def is_installed(self, name: str) -> bool:
        return name in self._installed

    def needs_repair(self, name: str) -> bool:
        return name in self._needs_repair

    def can_repair(self, name: str) -> bool:
        return name in self._missing_nets

    def missing_nets(self, name: str) -> FrozenSet[str]:
        return frozenset(self._missing_nets.get(name, ()))


@dataclass(frozen=True)
class _CustomEngine:
    """An operator-added engine as the registry stores it."""

    id: str
    display_name: str
    source: str
    url: str = ""


class _FakeCustomStore:
    def __init__(self, engines=()):
        self._engines = list(engines)

    def list(self):
        return list(self._engines)


def build_rows(**overrides):
    """Build the rows with every dependency faked, overriding one at a time."""
    kwargs = dict(
        engine_manager=_FakeEngineManager(),
        arch=ARCH_64,
        has_neon=True,
        resume_store=_FakeResumeStore(),
        custom_store=_FakeCustomStore(),
        failure_payload=lambda name: None,
        custom_binary_installed=lambda engine: False,
    )
    kwargs.update(overrides)
    return build_engine_rows(**kwargs)


def row_named(rows, name):
    """Return the one row for an engine, failing loudly if it is missing."""
    matches = [r for r in rows if r.name == name]
    assert len(matches) == 1, f"expected exactly one {name} row, got {len(matches)}"
    return matches[0]


class TestTheListCoversTheCatalogExactlyOnce:
    def test_every_catalog_engine_appears(self):
        """No engine is dropped and none is listed twice.

        Why: the board's builder and the endpoint's loop both walked the catalog
        independently, so a filter added to one silently gave the two surfaces
        different lists. One builder makes that a single fact worth pinning.

        How a regression manifests: an engine vanishes from both surfaces at once
        (a filter in the loop) or the count doubles (custom engines merged in
        twice), instead of the two quietly disagreeing.
        """
        names = [r.name for r in build_rows() if not r.is_custom]

        assert sorted(names) == sorted(ENGINES)
        assert len(names) == len(set(names))

    def test_custom_engines_follow_the_catalog(self):
        """Operator-added engines are listed, after the catalog, marked custom.

        Why: they existed only on the web. The board's builder never read the
        registry, so an engine the operator uploaded was invisible on the device
        it was uploaded for.

        How a regression manifests: the custom row disappears from the list, or
        lands among the rated engines where its absent rating would sort it
        arbitrarily.
        """
        custom = _CustomEngine(id="my-engine", display_name="My Engine", source="upload")
        rows = build_rows(custom_store=_FakeCustomStore([custom]))

        assert rows[-1].name == "my-engine"
        assert rows[-1].is_custom is True
        assert [r.is_custom for r in rows[:-1]] == [False] * len(ENGINES)

    def test_a_custom_engine_reports_whether_its_binary_is_there(self):
        """A custom engine is installed exactly when its binary is present.

        Why: it has no catalog entry to infer from, so the only truth is the file
        on disk, resolved through the containment guard by the caller.

        How a regression manifests: every custom engine reports installed (or
        none does), so Uninstall is offered for a binary that was never there.
        """
        custom = _CustomEngine(id="mine", display_name="Mine", source="upload")
        present = build_rows(
            custom_store=_FakeCustomStore([custom]),
            custom_binary_installed=lambda engine: True,
        )
        absent = build_rows(custom_store=_FakeCustomStore([custom]))

        assert row_named(present, "mine").installed is True
        assert row_named(absent, "mine").installed is False


class TestOrderAndGroupingComeFromTheRating:
    def test_rows_arrive_strongest_first(self):
        """The catalog is ordered by rating, so neither surface sorts it.

        Why: the board sorted installed-first then alphabetically, which is why
        the strongest engine appeared mid-list there and first on the web.

        How a regression manifests: the order follows the catalog's source order
        or an alphabetical sort, and Reckless is no longer first.
        """
        rows = [r for r in build_rows() if not r.is_custom]
        rated = [r.elo for r in rows if r.elo is not None]

        assert rows[0].name == "reckless"
        assert rated == sorted(rated, reverse=True)

    def test_install_state_does_not_reorder_the_list(self):
        """An installed engine keeps its place instead of jumping to the top.

        Why: this is the board's old rule, and it is the one being retired. It
        made the list's order depend on what happened to be installed, so the
        same catalog read differently on two devices.

        How a regression manifests: installing an engine moves its row, and the
        two surfaces show the same catalog in different orders again.
        """
        untouched = [r.name for r in build_rows() if not r.is_custom]
        with_weak_installed = [
            r.name
            for r in build_rows(engine_manager=_FakeEngineManager(installed=["claudia"]))
            if not r.is_custom
        ]
        assert with_weak_installed == untouched

    def test_each_row_carries_the_group_it_belongs_to(self):
        """Every row states its tier, so no client re-derives it.

        Why: the web used to group by hardcoded name lists, and anything absent
        fell through to Specialty -- which is how the strongest engine was filed
        among the novelty ones.

        How a regression manifests: a tier is empty or missing and its group
        disappears from the page, or an engine shows up in the wrong group.
        """
        rows = [r for r in build_rows() if not r.is_custom]
        for row in rows:
            assert row.tier == ENGINES[row.name].tier


class TestThisDeviceDecidesWhatCanBeInstalled:
    def test_an_unsupported_engine_says_so_and_why(self):
        """A row the CPU cannot run is marked unsupported, with the reason.

        Why: the board never consulted architecture support, so it offered a
        plain Install row for an engine that cannot build here. The install is
        refused up front, so nothing is destroyed -- but the user only learns
        that by pressing a button that could never work.

        How a regression manifests: `supported` is True for every engine on a
        32-bit board, and the reason text is empty, so the row looks installable.
        """
        rows_32 = [r for r in build_rows(arch=ARCH_32) if not r.is_custom]
        unsupported = [r for r in rows_32 if not r.supported]

        assert unsupported, "expected the 32-bit board to exclude some engines"
        for row in unsupported:
            assert row.unsupported_reason, f"{row.name} unsupported with no reason"

    def test_the_same_engines_are_installable_on_a_64_bit_board(self):
        """Support is a property of the device, not of the engine alone.

        Why: an always-False (or always-True) gate would satisfy the test above
        while being useless. This pins that the answer actually varies with the
        architecture passed in.

        How a regression manifests: the arch argument stops reaching the check
        and every board reports the same support, hiding engines that do run.
        """
        rows_64 = [r for r in build_rows(arch=ARCH_64) if not r.is_custom]
        rows_32 = [r for r in build_rows(arch=ARCH_32) if not r.is_custom]

        assert all(r.supported for r in rows_64)
        assert all(r.unsupported_reason is None for r in rows_64)
        assert sum(not r.supported for r in rows_32) > 0

    def test_a_custom_engine_is_always_installable(self):
        """Custom engines declare no architecture, so nothing excludes them.

        Why: the support gate reads `supported_archs` off a catalog definition,
        and a custom engine has none. Defaulting it to unsupported would hide the
        operator's own binary behind a reason that does not apply to it.

        How a regression manifests: uploaded engines render greyed out with an
        empty explanation.
        """
        custom = _CustomEngine(id="mine", display_name="Mine", source="upload")
        row = row_named(build_rows(custom_store=_FakeCustomStore([custom]), arch=ARCH_32), "mine")

        assert row.supported is True
        assert row.unsupported_reason is None


class TestBrokenAndPausedEnginesAreVisibleInTheList:
    def test_a_row_reports_that_it_needs_repair(self):
        """A net-backed engine missing its weights is installed but broken.

        Why: the board showed no repair state in the list, so a broken engine was
        indistinguishable from a working one until it was opened and failed.

        How a regression manifests: needs_repair is always False and the badge
        never appears, so the list claims a broken engine is fine.
        """
        manager = _FakeEngineManager(
            installed=["maia"], needs_repair=["maia"], missing_nets={"maia": ["1100"]}
        )
        row = row_named(build_rows(engine_manager=manager), "maia")

        assert row.installed is True
        assert row.needs_repair is True

    def test_a_healthy_engine_does_not_claim_to_need_repair(self):
        """The repair flag distinguishes engines rather than being set for all.

        Why: a flag that is always True is as useless as one always False, and
        would put a "needs repair" badge on every installed engine.

        How a regression manifests: every installed row shows the repair badge.
        """
        manager = _FakeEngineManager(installed=[SUPPORTED_ENGINE])
        row = row_named(build_rows(engine_manager=manager), SUPPORTED_ENGINE)

        assert row.installed is True
        assert row.needs_repair is False

    def test_a_paused_install_travels_with_its_engine(self):
        """A resume point reaches the row for the engine it belongs to.

        Why: several installs can be paused at once, so the record cannot come
        from the single-install status poll -- keying off that poll is what
        limited the page to one recoverable install.

        How a regression manifests: the resume point attaches to the wrong engine
        or to all of them, offering Resume for a build that does not exist.
        """
        point = _ResumePoint(engine="berserk", ref="v9", percent=42)
        rows = build_rows(resume_store=_FakeResumeStore({"berserk": point}))

        assert row_named(rows, "berserk").resume_point is point
        assert row_named(rows, SUPPORTED_ENGINE).resume_point is None

    def test_a_failure_reaches_the_row_that_failed(self):
        """The last failure is attached per engine, not shared across the list.

        Why: the payload is what the surfaces render as "this one failed", and
        the board rendered nothing at all.

        How a regression manifests: the failure lands on every row, so one bad
        install marks the whole catalog as failed.
        """
        failure = {"phase": "install", "reason_code": "build_failed"}
        rows = build_rows(
            failure_payload=lambda name: failure if name == "zahak" else None
        )

        assert row_named(rows, "zahak").last_failure == failure
        assert row_named(rows, SUPPORTED_ENGINE).last_failure is None

    def test_a_custom_engine_reports_its_failure_too(self):
        """Adding a custom engine can fail, and the operator must see why.

        Why: adding one by URL downloads a binary, which fails like any other
        install. A custom row that always reported no failure would leave the
        operator with an engine that simply did not appear, and no reason.

        How a regression manifests: the custom row's failure is hardcoded to
        None, so a failed URL add shows a silent, healthy-looking row.
        """
        failure = {"phase": "install", "reason_code": "download_failed"}
        custom = _CustomEngine(id="mine", display_name="Mine", source="url", url="http://x/e")
        rows = build_rows(
            custom_store=_FakeCustomStore([custom]),
            failure_payload=lambda name: failure if name == "mine" else None,
        )

        assert row_named(rows, "mine").last_failure == failure


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
