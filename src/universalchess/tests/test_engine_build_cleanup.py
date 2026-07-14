"""Tests for build-artifact cleanup in EngineManager.

Background / why these tests exist
----------------------------------
Source-built engines (e.g. Ethereal, Arasan) are cloned and compiled under
``build_tmp/<name>``. Historically that clone/build tree was removed only on
uninstall, so successful installs left hundreds of MB of source trees lingering
under ``build_tmp`` -- a stale Ethereal tree was found consuming space and
memory on a constrained board in production. ``install_engine`` now reclaims the
build tree after every attempt (success or failure) via ``_cleanup_build_dir``.
"""

from pathlib import Path

from universalchess.managers.engine_manager import EngineManager
from universalchess.services.engine_install_record import EngineInstallRecordStore


def _manager(tmp_path):
    """An EngineManager pointed at temp engines and build directories."""
    manager = EngineManager(
        engines_dir=str(tmp_path / "engines"),
        record_store=EngineInstallRecordStore(path=tmp_path / "record.json"),
    )
    # Redirect the build temp dir into the test sandbox (the default is the fixed
    # production /opt path, which tests must never touch).
    manager.build_tmp = tmp_path / "build"
    return manager


def test_cleanup_build_dir_removes_source_tree(tmp_path):
    """_cleanup_build_dir removes an engine's clone/build tree.

    Why: this is the reclamation that stops multi-hundred-MB source trees from
    accumulating under build_tmp. How it manifests: if the tree is not removed,
    the directory (and its files) still exist after the call.
    """
    manager = _manager(tmp_path)
    build_dir = manager.build_tmp / "ethereal"
    (build_dir / "src").mkdir(parents=True)
    (build_dir / "src" / "ethereal.c").write_text("int main(){}")

    manager._cleanup_build_dir("ethereal")

    assert not build_dir.exists()


def test_cleanup_build_dir_missing_is_noop(tmp_path):
    """_cleanup_build_dir tolerates a missing build tree.

    Why: system-package and bundled installs never create a build tree, so the
    unconditional cleanup call in install_engine's finally must be a safe no-op.
    How it manifests: a missing directory would raise and turn a successful
    install into a failure if this were not guarded.
    """
    manager = _manager(tmp_path)

    # Must not raise even though build_tmp/ethereal was never created.
    manager._cleanup_build_dir("ethereal")

    assert not (manager.build_tmp / "ethereal").exists()


def test_cleanup_build_dir_leaves_other_engines(tmp_path):
    """_cleanup_build_dir removes only the named engine's tree.

    Why: installing one engine must not wipe another engine's in-progress or
    cached build tree. How it manifests: an over-broad rmtree (e.g. clearing all
    of build_tmp) would delete the sibling directory too.
    """
    manager = _manager(tmp_path)
    target = manager.build_tmp / "ethereal"
    other = manager.build_tmp / "arasan"
    target.mkdir(parents=True)
    other.mkdir(parents=True)
    (other / "keep.txt").write_text("keep")

    manager._cleanup_build_dir("ethereal")

    assert not target.exists()
    assert other.exists()
    assert (other / "keep.txt").read_text() == "keep"


def test_install_cleans_build_dir_on_success(tmp_path):
    """install_engine reclaims the build tree even on a non-build install.

    Why: this proves the finally-block wiring runs _cleanup_build_dir for the
    engine being installed regardless of install route. A bundled engine
    (Worstfish) is used because it installs without compiling or network access,
    yet a stale build tree for its name must still be reaped.

    How it manifests: if the finally cleanup were missing, the pre-seeded stale
    build tree would survive a successful install.
    """
    manager = _manager(tmp_path)
    stale = manager.build_tmp / "worstfish"
    stale.mkdir(parents=True)
    (stale / "leftover.o").write_text("junk")

    assert manager.install_engine("worstfish") is True

    assert not stale.exists(), "install must reclaim the stale build tree"
