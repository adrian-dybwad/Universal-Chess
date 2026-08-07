"""Stopping an install, and what survives it.

Background / why these tests exist
----------------------------------
A source build can run for an hour on this hardware. There was no way to stop one:
the only exits were completion, a stall, or the ceiling, and every exit ran
``_cleanup_build_dir``, so an install abandoned by rebooting the board threw away
all the compile work it had done.

Stopping therefore has to do two things the existing exits do not. It must end the
compiler promptly -- the build runs in its own process group precisely so the whole
tree can be killed -- and it must leave the build tree behind, because a preserved
tree is the entire value of resuming (cargo's ``target/`` and make's object files
are what make the second attempt short).

Preservation is the dangerous half. ``_cleanup_build_dir`` exists because stale
trees were found consuming hundreds of MB on a constrained board, so "keep the
tree" must apply to a stop and to nothing else: a completed install has already
copied its binary out, and a failed one holds a partial tree of no value. Those
cases must still be reclaimed, which is what most of these tests check.

The manager deliberately knows nothing about resume points (see
services/install_resume). It preserves the tree and reports that it was stopped;
the web layer records why and at what ref. Reuse is likewise driven by the caller
passing ``reuse_tree_at_ref``, so the manager never has to read a marker file.
"""

import contextlib
import subprocess
import threading
import time

import pytest

from universalchess.managers.engine_manager import (
    EngineDefinition,
    EngineManager,
    InstallCancelled,
)
from universalchess.services.engine_install_record import EngineInstallRecordStore
from universalchess.services.build_progress import ProcessInfo

# Arasan is pinned at this tag in the catalog; a resumed install must rebuild the
# same ref it was stopped at, so the reuse tests turn on matching this exactly.
PINNED_REF = "v25.4"
OTHER_REF = "v25.5"


def _live_process_table(root_pid: int, cpu_ticks: int = 100) -> dict:
    """A one-process tree under ``root_pid`` that is consuming CPU.

    Keeps the stall detector satisfied, so a test that means to exercise
    cancellation cannot accidentally pass because the command was killed for
    stalling instead.
    """
    child_pid = root_pid + 1
    return {
        child_pid: ProcessInfo(
            pid=child_pid, ppid=root_pid, comm="cc1plus", cpu_ticks=cpu_ticks,
            args=("/usr/libexec/gcc/cc1plus", "-quiet", "src/search.cpp"),
        )
    }


def _manager(tmp_path) -> EngineManager:
    """An EngineManager confined to the test sandbox."""
    manager = EngineManager(
        engines_dir=str(tmp_path / "engines"),
        record_store=EngineInstallRecordStore(path=tmp_path / "record.json"),
    )
    manager.build_tmp = tmp_path / "build"
    return manager


@pytest.fixture
def manager(tmp_path, monkeypatch) -> EngineManager:
    """A manager that can reach the source-build branch on a dev/CI host.

    Arch is pinned to arm64 so Arasan passes the support gate anywhere, and the
    build-memory reservation is stubbed as acquired because there is no sudo grant
    off-board (the gate itself is covered by test_engine_install_build_memory).
    """
    mgr = _manager(tmp_path)
    monkeypatch.setattr(mgr, "_get_arch", lambda: "arm64")

    @contextlib.contextmanager
    def _fake_build_memory(*args, **kwargs):
        yield True

    monkeypatch.setattr(
        "universalchess.managers.engine_manager.build_memory", _fake_build_memory
    )
    return mgr


def _seed_tree(manager: EngineManager, engine_name: str, marker: str = "partial.o"):
    """Create a build tree with a recognisable artifact in it."""
    tree = manager.build_tmp / engine_name
    tree.mkdir(parents=True, exist_ok=True)
    (tree / marker).write_text("compiled work")
    return tree


def _source_engine(repo_url=None) -> EngineDefinition:
    """A throwaway source-built engine with no apt dependencies."""
    return EngineDefinition(
        name="dummy",
        display_name="Dummy",
        summary="",
        description="",
        repo_url=repo_url,
        build_commands=["true"],
        binary_path="dummy",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=[],
    )


class TestStoppingARunningCommand:
    """A stop request must end the compiler, not wait for it."""

    def test_a_stop_request_ends_the_command_promptly(self, tmp_path):
        """A running build stops within moments of the request.

        Why: a build that is genuinely working refreshes its liveness signal
        forever, so neither the stall window nor the ceiling will end it -- the
        stop request is the only thing that can. The injected process table here
        reports steady CPU consumption for exactly that reason: if cancellation
        were not wired in, the command would run its full 60 seconds.

        How a regression manifests: no InstallCancelled is raised and the test
        hangs until the sleep finishes, far past the elapsed-time assertion.
        """
        manager = _manager(tmp_path)
        threading.Timer(0.5, manager.request_stop).start()

        started = time.monotonic()
        with pytest.raises(InstallCancelled):
            manager._run_monitored_command(
                "sleep 60", tmp_path, on_line=lambda _line: None,
                stall_seconds=30, ceiling_seconds=300,
                read_processes=lambda root_pid: _live_process_table(root_pid),
            )

        assert time.monotonic() - started < 10

    def test_a_stop_is_distinguishable_from_a_timeout(self, tmp_path):
        """Cancellation raises its own exception, not TimeoutExpired.

        Why: the two have opposite meanings to the user and to the tree. A timeout
        is a failure whose partial tree is reclaimed and reported as an error; a
        stop is a pause whose tree is kept and reported as resumable. Reusing
        TimeoutExpired would make them indistinguishable at every layer above, and
        a stopped install would be shown as "Build stalled".

        How a regression manifests: InstallCancelled is not raised, so this fails
        on the exception type rather than silently mislabelling stops in the UI.
        """
        manager = _manager(tmp_path)
        manager.request_stop()

        with pytest.raises(InstallCancelled):
            manager._run_monitored_command(
                "sleep 30", tmp_path, on_line=lambda _line: None,
                stall_seconds=30, ceiling_seconds=300,
                read_processes=lambda root_pid: _live_process_table(root_pid),
            )

    def test_an_unstopped_command_still_runs_to_completion(self, tmp_path):
        """Adding cancellation must not disturb the ordinary path.

        Why: the cancellation check runs on the same poll loop as stall and
        ceiling detection. A flag inverted or initialised wrong would abort every
        build on the board immediately.

        How a regression manifests: InstallCancelled is raised for a command
        nobody stopped, so this fails instead of returning 0.
        """
        manager = _manager(tmp_path)

        returncode, _tail = manager._run_monitored_command(
            "true", tmp_path, on_line=lambda _line: None,
            stall_seconds=30, ceiling_seconds=300,
            read_processes=lambda root_pid: _live_process_table(root_pid),
        )

        assert returncode == 0


class TestTreePreservation:
    """Which exits keep the build tree, and which reclaim it."""

    def test_a_stopped_install_keeps_its_build_tree(self, manager, monkeypatch):
        """The compile work survives a stop.

        Why: this is the point of the feature. Reckless takes about an hour; a
        stop at 61% that discarded the tree would make Resume a synonym for
        Install, and the user would have gained nothing by stopping.

        How a regression manifests: the finally-block cleanup runs unconditionally
        as it did before, and the seeded artifact is gone.
        """
        tree = _seed_tree(manager, "arasan")

        def stopped(engine, update_progress, ref_label=None, reuse_tree_at_ref=None):
            raise InstallCancelled()

        monkeypatch.setattr(manager, "_install_from_source", stopped)

        assert manager.install_engine("arasan", ref=OTHER_REF) is False

        assert (tree / "partial.o").exists(), "a stopped install must keep its tree"

    def test_a_stopped_install_reports_that_it_was_stopped(self, manager, monkeypatch):
        """The manager distinguishes a stop from a failure to its caller.

        Why: ``install_engine`` returns False for both, but the web layer must
        record one as CANCELLED (resumable, no error) and the other as FAILED. With
        no way to tell them apart, a user who pressed Stop would be shown an
        install failure.

        How a regression manifests: was_stopped() is False after a stop, and the
        UI reports a spurious error.
        """
        def stopped(engine, update_progress, ref_label=None, reuse_tree_at_ref=None):
            raise InstallCancelled()

        monkeypatch.setattr(manager, "_install_from_source", stopped)

        manager.install_engine("arasan", ref=OTHER_REF)

        assert manager.was_stopped() is True

    def test_a_failed_install_still_reclaims_its_tree(self, manager, monkeypatch):
        """An ordinary build failure is not a pause.

        Why: preserving trees on failure would reintroduce exactly the disk leak
        ``_cleanup_build_dir`` was written to stop, and a partial tree from a
        broken build is not worth resuming -- the next attempt should start clean.

        How a regression manifests: the preservation check is too broad (keying off
        "not success" rather than "stopped") and the failed tree survives.
        """
        tree = _seed_tree(manager, "arasan")
        monkeypatch.setattr(
            manager, "_install_from_source",
            lambda engine, update_progress, ref_label=None, reuse_tree_at_ref=None: False,
        )

        assert manager.install_engine("arasan", ref=OTHER_REF) is False

        assert not tree.exists()
        assert manager.was_stopped() is False

    def test_a_successful_install_still_reclaims_its_tree(self, manager, monkeypatch):
        """Success reclaims the tree as it always did.

        Why: the binary has been copied out by this point, so the tree is pure
        waste. This is the case the original cleanup was written for and the one a
        preservation bug is most likely to break.

        How a regression manifests: every successful source install leaves its tree
        behind, filling the board's disk.
        """
        tree = _seed_tree(manager, "arasan")
        monkeypatch.setattr(
            manager, "_install_from_source",
            lambda engine, update_progress, ref_label=None, reuse_tree_at_ref=None: True,
        )

        assert manager.install_engine("arasan", ref=OTHER_REF) is True

        assert not tree.exists()

    def test_stopping_one_install_leaves_another_engines_tree(self, manager, monkeypatch):
        """A stop touches only the engine being stopped.

        Why: several engines can sit paused at once, and the whole multi-install
        requirement rests on one engine's install never reaching another's
        directory.

        How a regression manifests: a cleanup (or preservation) that operates on
        build_tmp as a whole takes the sibling's tree with it.
        """
        _seed_tree(manager, "arasan")
        other = _seed_tree(manager, "reckless", marker="reckless.o")

        def stopped(engine, update_progress, ref_label=None, reuse_tree_at_ref=None):
            raise InstallCancelled()

        monkeypatch.setattr(manager, "_install_from_source", stopped)
        manager.install_engine("arasan", ref=OTHER_REF)

        assert (other / "reckless.o").exists()

    def test_a_later_install_of_another_engine_leaves_a_paused_tree(self, manager, monkeypatch):
        """Starting a different install does not disturb a paused one.

        Why: this is the user-visible requirement -- pause Reckless, install
        Berserk, and Reckless must still be resumable afterwards. The cleanup runs
        on the installing engine's name, so a paused sibling must be untouched by
        both the successful install and its cleanup.

        How a regression manifests: the paused tree is gone after an unrelated
        install completes, so Resume rebuilds from scratch.
        """
        paused = _seed_tree(manager, "reckless", marker="reckless.o")
        monkeypatch.setattr(
            manager, "_install_from_source",
            lambda engine, update_progress, ref_label=None, reuse_tree_at_ref=None: True,
        )

        assert manager.install_engine("arasan", ref=OTHER_REF) is True

        assert (paused / "reckless.o").exists()


class TestStopFlagLifecycle:
    """A stop must apply to the install it was aimed at, and no other."""

    def test_a_stop_does_not_carry_into_the_next_install(self, manager, monkeypatch):
        """The next install starts un-stopped.

        Why: the flag lives on the manager, and a stopped install leaves it set. If
        it were not cleared when the next install begins, the engine the user
        started next would be cancelled the moment it reached its first monitored
        command -- an install that stops instantly for no visible reason.

        How a regression manifests: the second install reports was_stopped() True
        and never ran its source step.
        """
        def stopped(engine, update_progress, ref_label=None, reuse_tree_at_ref=None):
            raise InstallCancelled()

        monkeypatch.setattr(manager, "_install_from_source", stopped)
        manager.install_engine("arasan", ref=OTHER_REF)
        assert manager.was_stopped() is True

        ran = {"source": False}

        def succeeds(engine, update_progress, ref_label=None, reuse_tree_at_ref=None):
            ran["source"] = True
            return True

        monkeypatch.setattr(manager, "_install_from_source", succeeds)
        assert manager.install_engine("arasan", ref=OTHER_REF) is True

        assert ran["source"] is True
        assert manager.was_stopped() is False

    def test_stopping_when_nothing_runs_is_harmless(self, manager):
        """A stop request with no install in flight is a no-op.

        Why: the endpoint and the board button can both fire just as an install
        finishes. Raising here would turn a lost race into a 500.

        How a regression manifests: request_stop() raises, or leaves the flag set
        so the next install dies on arrival (covered above).
        """
        manager.request_stop()

        assert manager.was_stopped() is False


class TestTreeReuseOnResume:
    """A preserved tree may be reused only when it holds the right ref."""

    def _stub_git_and_build(self, manager, monkeypatch):
        """Let git and the build appear to succeed, recording what git ran."""
        git_calls = []

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            git_calls.append(list(cmd))
            return _Result()

        monkeypatch.setattr(
            "universalchess.managers.engine_manager.subprocess.run", fake_run
        )
        monkeypatch.setattr(
            manager, "_run_monitored_command",
            lambda cmd, cwd, on_line, **kwargs: (0, ""),
        )
        return git_calls

    def test_a_matching_ref_keeps_the_preserved_tree(self, manager, monkeypatch):
        """Resuming at the ref the tree was built at reuses the tree.

        Why: ``_install_from_source`` wipes an existing checkout whenever a
        specific ref is targeted, because a leftover tree may hold a different ref
        or stale objects from another build type. Every catalog install targets a
        resolved ref, so without an exemption a resume would always start from a
        clean clone and discard the work it was meant to continue.

        How a regression manifests: the preserved object file is gone and the
        installer re-clones, so resuming Reckless costs the full hour again.
        """
        engine = _source_engine(repo_url="https://example.invalid/dummy.git")
        tree = _seed_tree(manager, "dummy")
        self._stub_git_and_build(manager, monkeypatch)

        manager._install_from_source(
            engine, lambda *a, **k: None,
            ref_label=PINNED_REF, reuse_tree_at_ref=PINNED_REF,
        )

        assert (tree / "partial.o").exists(), "a resumed build must reuse its tree"

    def test_a_different_ref_discards_the_preserved_tree(self, manager, monkeypatch):
        """A tree built at another ref is not reused.

        Why: the marker records which ref the preserved tree holds precisely so
        this case can be caught. Reusing a v25.4 checkout to build v25.5 would
        silently produce a binary of the wrong version while the UI reported the
        requested one -- worse than rebuilding, because it is undetectable.

        How a regression manifests: the exemption checks only that a resume is in
        progress, not that the refs agree, and the stale tree survives.
        """
        engine = _source_engine(repo_url="https://example.invalid/dummy.git")
        tree = _seed_tree(manager, "dummy")
        self._stub_git_and_build(manager, monkeypatch)

        manager._install_from_source(
            engine, lambda *a, **k: None,
            ref_label=OTHER_REF, reuse_tree_at_ref=PINNED_REF,
        )

        assert not (tree / "partial.o").exists()

    def test_an_ordinary_install_still_starts_from_a_clean_checkout(self, manager, monkeypatch):
        """Without a reuse ref the existing wipe behaviour is unchanged.

        Why: this is the pre-existing guarantee that a ref-targeted build gets
        exactly that ref. The reuse path is an exemption to it and must not become
        the default, or a stale tree from any earlier install would be built
        blindly.

        How a regression manifests: the wipe is skipped whenever a tree exists, and
        ref-targeted installs silently build whatever was there before.
        """
        engine = _source_engine(repo_url="https://example.invalid/dummy.git")
        tree = _seed_tree(manager, "dummy")
        self._stub_git_and_build(manager, monkeypatch)

        manager._install_from_source(engine, lambda *a, **k: None, ref_label=PINNED_REF)

        assert not (tree / "partial.o").exists()

    def test_the_reuse_ref_reaches_the_source_installer(self, manager, monkeypatch):
        """install_engine threads the caller's reuse ref through to the build.

        Why: the web layer reads the resume marker and passes its ref; if that
        argument is dropped in the middle of the call chain the reuse check always
        compares against None and every resume re-clones. The wiring is invisible
        from the outside, so it is pinned here rather than inferred from timing.

        How a regression manifests: source_args records None and the preserved tree
        is discarded on every resume.
        """
        seen = {}

        def record(engine, update_progress, ref_label=None, reuse_tree_at_ref=None):
            seen["ref_label"] = ref_label
            seen["reuse_tree_at_ref"] = reuse_tree_at_ref
            return True

        monkeypatch.setattr(manager, "_install_from_source", record)

        manager.install_engine("arasan", ref=OTHER_REF, reuse_tree_at_ref=OTHER_REF)

        assert seen == {"ref_label": OTHER_REF, "reuse_tree_at_ref": OTHER_REF}


class TestStopDuringDependencyInstall:
    """apt is a boundary that must not be left mid-transaction."""

    def test_a_stop_during_apt_is_reported_as_a_stop(self, manager, monkeypatch):
        """Cancelling the dependency step ends the install as stopped.

        Why: the dependency install runs through the same monitored command as a
        build, so it is cancellable too, and an engine can be stopped before it
        ever reaches the compiler. That still has to present as a pause rather than
        a dependency failure, which is what the user would otherwise be told.

        How a regression manifests: InstallCancelled from the apt step is caught by
        the generic error handling and reported as "Could not install required
        build dependencies".
        """
        def stopped_in_deps(engine, update_progress, ref_label=None, reuse_tree_at_ref=None):
            raise InstallCancelled()

        monkeypatch.setattr(manager, "_install_from_source", stopped_in_deps)

        assert manager.install_engine("arasan", ref=OTHER_REF) is False

        assert manager.was_stopped() is True
        assert "depend" not in (manager.get_install_error() or "").lower()
