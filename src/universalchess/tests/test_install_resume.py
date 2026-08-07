"""Resume points: the per-engine record that makes a stopped install resumable.

Background / why these tests exist
----------------------------------
A stopped install leaves a half-built tree under ``build_tmp/<engine>``. Something
has to remember that the tree is worth keeping, which ref it was built at, and how
far it got -- otherwise the tree is indistinguishable from the stale leftovers that
``_cleanup_build_dir`` exists to reclaim.

That record deliberately lives *inside* the engine's own build tree rather than in
a shared list, because several engines can sit paused at once. A central registry
would have to be kept in step with N directories by hand: one engine's install
overwriting the shared file (which is exactly what the single-slot
``InstallStateStore`` does on every ``start()``) would strand another engine's tree
with nothing offering to resume or discard it. Keeping the record in the tree makes
the isolation structural -- an install can only reach its own directory -- and makes
the record and the artifact impossible to desync, since discarding is one rmtree
that takes both.
"""

import json

import pytest

from universalchess.services.install_resume import (
    RESUME_MARKER_NAME,
    ResumePoint,
    ResumePointStore,
)

ENGINE = "reckless"
OTHER_ENGINE = "berserk"
REF = "v2.1.0"
STAGE = "building"
MESSAGE = "Building Reckless: crate 41 of ~120"
PERCENT = 61
STOPPED_AT = 1_700_000_000.0


def _point(engine: str = ENGINE, **overrides) -> ResumePoint:
    """A resume point with every field populated, so round-trips are total."""
    fields = {
        "engine": engine,
        "ref": REF,
        "stage": STAGE,
        "message": MESSAGE,
        "percent": PERCENT,
        "stopped_at": STOPPED_AT,
        "reason": "stopped",
    }
    fields.update(overrides)
    return ResumePoint(**fields)


@pytest.fixture
def store(tmp_path) -> ResumePointStore:
    """A store rooted in the test sandbox (never the production /opt path)."""
    return ResumePointStore(build_root=tmp_path / "engine_build")


class TestRoundTrip:
    """Writing a point and reading it back must preserve every field."""

    def test_a_written_point_reads_back_whole(self, store):
        """Every field survives the write/read round-trip.

        Why: each field drives a different consumer -- ``ref`` decides whether the
        preserved tree may be reused, ``percent`` and ``message`` render the paused
        card, ``stopped_at`` orders and ages the entry. A partial round-trip would
        surface as a resumed install rebuilding at the wrong ref or a card showing
        0%, both far from the field that was actually dropped.

        How a regression manifests: a field is None/absent in the value read back.
        """
        store.write(_point())

        assert store.read(ENGINE) == _point()

    def test_a_point_is_written_even_before_the_tree_exists(self, store, tmp_path):
        """Stopping before the clone still records a resume point.

        Why: an install can be stopped during dependency installation, before
        ``build_tmp/<engine>`` is ever created. There is no build work to preserve
        then, but the install is still paused and must still offer Resume rather
        than vanishing from the UI. The store therefore creates the directory it
        needs.

        How a regression manifests: write raises FileNotFoundError, or read returns
        None, and a stop during INSTALLING_DEPS silently loses the install.
        """
        assert not (tmp_path / "engine_build" / ENGINE).exists()

        store.write(_point(stage="installing_deps", percent=18))

        assert store.read(ENGINE).stage == "installing_deps"

    def test_a_rewrite_replaces_the_previous_point(self, store):
        """Writing twice leaves one point, the newer one.

        Why: an engine stopped, resumed, and stopped again must describe where it
        got to the second time. How it manifests: stale percent/ref from the first
        stop, so a resume rebuilds at the wrong ref.
        """
        store.write(_point(percent=10, ref="v1.0.0"))
        store.write(_point(percent=88, ref="v2.1.0"))

        point = store.read(ENGINE)
        assert (point.percent, point.ref) == (88, "v2.1.0")


class TestAbsentAndUnreadable:
    """A missing, bare, or corrupt tree is "nothing to resume", never an error."""

    def test_no_tree_means_no_point(self, store):
        """An engine that was never installed has no resume point.

        Why: read is called for every engine on every list render. How it
        manifests: an exception here breaks the whole engine list.
        """
        assert store.read(ENGINE) is None

    def test_a_tree_without_a_marker_is_not_resumable(self, store, tmp_path):
        """A build tree carrying no marker offers no resume.

        Why: this is the critical distinction. Trees predating this feature, and
        trees left by a crash mid-cleanup, are stale leftovers -- not paused work.
        Treating an unmarked tree as resumable would offer to "resume" an install
        nobody stopped, at an unknown ref, from a tree of unknown provenance.

        How a regression manifests: read infers a point from the directory's mere
        existence and every leftover tree sprouts a Resume button.
        """
        (tmp_path / "engine_build" / ENGINE / "src").mkdir(parents=True)

        assert store.read(ENGINE) is None

    def test_a_corrupt_marker_is_treated_as_absent(self, store, tmp_path):
        """A truncated or malformed marker degrades to "no resume point".

        Why: the marker is written while the board may lose power. A crash mid-write
        must not make the engine list endpoint throw on every poll thereafter.

        How a regression manifests: json.JSONDecodeError escapes read and 500s the
        engine list rather than the engine simply offering a fresh install.
        """
        marker = tmp_path / "engine_build" / ENGINE / RESUME_MARKER_NAME
        marker.parent.mkdir(parents=True)
        marker.write_text('{"engine": "reckless", "ref"')

        assert store.read(ENGINE) is None

    def test_a_marker_missing_required_fields_is_treated_as_absent(self, store, tmp_path):
        """A marker from an incompatible version is ignored, not half-applied.

        Why: a forward/backward-incompatible marker with only some keys would
        otherwise construct a ResumePoint with missing attributes and fail at the
        point of use -- inside the install thread, not at the read.

        How a regression manifests: TypeError from the dataclass constructor
        escaping read.
        """
        marker = tmp_path / "engine_build" / ENGINE / RESUME_MARKER_NAME
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"engine": ENGINE}))

        assert store.read(ENGINE) is None


class TestDiscard:
    """Discarding throws away the tree and the record in one operation."""

    def test_discard_removes_the_whole_tree(self, store, tmp_path):
        """Discard reclaims the build tree, not just the marker.

        Why: the tree is the reason the record exists -- hundreds of MB on a
        constrained board. Deleting only the marker would leak the space while
        making it un-offerable, the worst of both.

        How a regression manifests: the directory survives and the space is never
        reclaimed by anything.
        """
        store.write(_point())
        tree = tmp_path / "engine_build" / ENGINE
        (tree / "target").mkdir(parents=True)
        (tree / "target" / "big.o").write_text("x" * 1024)

        store.discard(ENGINE)

        assert not tree.exists()
        assert store.read(ENGINE) is None

    def test_discarding_what_is_not_there_is_a_no_op(self, store):
        """Discard of an absent tree succeeds silently.

        Why: discard races the UI. Two clicks, or a discard of an install whose tree
        was already reclaimed, must not raise. How it manifests: FileNotFoundError
        turning a harmless double-click into a 500.
        """
        store.discard(ENGINE)

        assert store.read(ENGINE) is None


class TestClear:
    """Clearing retires the record while the tree it describes stays put."""

    def test_clear_removes_the_marker_and_keeps_the_tree(self, store, tmp_path):
        """Clear ends the paused state without touching the preserved work.

        Why: resuming an install must retire its resume point -- the engine is no
        longer paused -- but the tree is the whole reason resuming is worth doing,
        and the resumed build is about to reuse it. Discard cannot serve here: it
        deletes the tree, which would turn every resume into a fresh build.

        How a regression manifests: if clear rmtrees like discard, the resumed
        build finds no objects and recompiles from scratch; if it does nothing,
        the card keeps showing "Stopped at N%" beside the running install.
        """
        store.write(_point())
        tree = tmp_path / "engine_build" / ENGINE
        (tree / "target").mkdir(parents=True)
        (tree / "target" / "partial.o").write_text("object")

        store.clear(ENGINE)

        assert store.read(ENGINE) is None
        assert (tree / "target" / "partial.o").read_text() == "object"

    def test_clearing_what_is_not_there_is_a_no_op(self, store):
        """Clear of an engine with no point succeeds silently.

        Why: every install start clears its engine's point, and most installs were
        never paused. How it manifests: FileNotFoundError from a routine install of
        an engine that has no marker, failing the install before it begins.
        """
        store.clear(ENGINE)

        assert store.read(ENGINE) is None

    def test_clearing_one_engine_leaves_the_other_paused(self, store):
        """Clear is scoped to its engine, like every other operation here.

        Why: this is the multi-install requirement applied to the new operation --
        resuming one engine must not retire another engine's paused state. How it
        manifests: the sibling's Resume button disappears when an unrelated install
        starts, stranding its tree with nothing offering to resume or discard it.
        """
        store.write(_point(ENGINE))
        store.write(_point(OTHER_ENGINE))

        store.clear(ENGINE)

        assert store.read(OTHER_ENGINE) == _point(OTHER_ENGINE)


class TestIsolationBetweenEngines:
    """The property this design exists for: paused installs cannot touch each other."""

    def test_two_engines_hold_independent_points(self, store):
        """Points for different engines coexist with distinct contents.

        Why: this is the multi-install requirement. A single shared record would let
        the second write clobber the first, which is precisely how the single-slot
        InstallStateStore loses a paused engine when another install starts.

        How a regression manifests: reading the first engine returns the second
        engine's ref/percent, or None.
        """
        store.write(_point(ENGINE, ref="v2.1.0", percent=61))
        store.write(_point(OTHER_ENGINE, ref="v13", percent=22))

        first, second = store.read(ENGINE), store.read(OTHER_ENGINE)
        assert (first.ref, first.percent) == ("v2.1.0", 61)
        assert (second.ref, second.percent) == ("v13", 22)

    def test_discarding_one_engine_leaves_the_other_paused(self, store, tmp_path):
        """Discard is scoped to its engine's directory.

        Why: an over-broad rmtree (clearing all of build_root) would throw away
        every paused install when the user discards one. How it manifests: the
        sibling's tree and point disappear alongside the target's.
        """
        store.write(_point(ENGINE))
        store.write(_point(OTHER_ENGINE))
        (tmp_path / "engine_build" / OTHER_ENGINE / "keep.o").write_text("keep")

        store.discard(ENGINE)

        assert store.read(OTHER_ENGINE) == _point(OTHER_ENGINE)
        assert (tmp_path / "engine_build" / OTHER_ENGINE / "keep.o").exists()

    def test_list_all_reports_every_paused_engine(self, store):
        """list_all enumerates the paused installs and nothing else.

        Why: the engine list endpoint renders one Resume/Discard pair per paused
        engine from this, in a single directory scan rather than a stat per catalog
        engine. How it manifests: a paused engine missing from the map renders as
        freshly installable, orphaning its tree.
        """
        store.write(_point(ENGINE))
        store.write(_point(OTHER_ENGINE))

        assert set(store.list_all()) == {ENGINE, OTHER_ENGINE}
        assert store.list_all()[ENGINE].ref == REF

    def test_list_all_ignores_trees_without_a_marker(self, store, tmp_path):
        """Unmarked build trees are absent from the listing.

        Why: same distinction as the single read -- a stale leftover tree is not a
        paused install. How it manifests: every engine ever built shows a phantom
        Resume until its tree is reclaimed.
        """
        store.write(_point(ENGINE))
        (tmp_path / "engine_build" / "arasan" / "src").mkdir(parents=True)

        assert set(store.list_all()) == {ENGINE}

    def test_list_all_on_a_missing_build_root_is_empty(self, store):
        """A board that has never built anything lists nothing.

        Why: build_root does not exist until the first source install. How it
        manifests: the engine list endpoint 500s on a fresh install of the product.
        """
        assert store.list_all() == {}


class TestPathContainment:
    """The engine name reaches the filesystem, so it must be contained."""

    @pytest.mark.parametrize(
        "engine_name",
        ["../escape", "../../etc/passwd", "/absolute", "nested/name", ""],
        ids=["parent", "traversal", "absolute", "separator", "empty"],
    )
    def test_a_name_that_escapes_the_build_root_is_refused(self, store, engine_name, tmp_path):
        """Reads and discards outside the build root are refused, not performed.

        Why: the engine name arrives from an HTTP request on the resume/discard
        endpoints. Without containment, ``discard("../../etc")`` is an rmtree of a
        caller-chosen directory running as the service user -- arbitrary deletion,
        not merely a wrong answer. Endpoint-level validation against the catalog is
        the first line, but the store must not depend on its caller for this.

        How a regression manifests: read/discard/clear resolve outside build_root;
        here the sentinel outside the root is deleted, or a point is read from it.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("do not delete")
        (outside / RESUME_MARKER_NAME).write_text("{}")

        assert store.read(engine_name) is None
        store.discard(engine_name)
        store.clear(engine_name)

        assert (outside / "sentinel").exists()
        assert (outside / RESUME_MARKER_NAME).exists()
