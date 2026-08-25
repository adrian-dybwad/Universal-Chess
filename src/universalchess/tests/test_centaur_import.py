"""Tests for the Centaur SD-image import service.

The service loop-mounts an uploaded ext4 image, locates the Centaur app inside
it, validates the file set, and copies it to the managed CENTAUR_HOME with debug
cruft stripped. The privileged mount/umount and the gzip decompression are the
injected side-effect boundary; detection, validation, cruft-filtering, and the
copy/exec-bit logic are exercised here through the public entry points.
"""

import errno
import gzip
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from universalchess.services import event_log
from universalchess.services.centaur_import import (
    CentaurImportError,
    InstallResult,
    centaur_app_installed,
    detect_app_dir,
    ensure_factory_marker,
    ignore_cruft,
    install_from_image,
    validate_app_dir,
)
from universalchess.services.centaur_import.events import EVENT_CATEGORY
from universalchess.services.centaur_import.installer import (
    ARMHF_SETUP_HELPER,
    MOUNT_HELPER,
    ensure_armhf_support,
)

# Debug artifacts the managed copy must never carry over from the SD image.
_CRUFT = ("core.1234", "engine-in.log", "_trace", "_dbg_run.sh")


def _make_app_tree(base: Path, subdir: str = "home/pi/centaur", *, cruft: bool = False) -> Path:
    """Create a realistic Centaur app directory under ``base``.

    Mirrors the official DGT Buildroot layout: an executable ``centaur`` next to
    ``engines/`` and ``fonts/``. ``cruft=True`` adds the debug files the import
    is expected to strip.
    """
    app = base / subdir
    (app / "engines").mkdir(parents=True)
    (app / "fonts").mkdir(parents=True)
    binary = app / "centaur"
    binary.write_text("#!fake centaur\n")
    binary.chmod(0o755)
    (app / "engines" / "stockfish_pi").write_text("#!engine\n")
    (app / "engines" / "stockfish_pi").chmod(0o755)
    (app / "fonts" / "Font.ttf").write_bytes(b"\x00\x01")
    if cruft:
        (app / "core.1234").write_bytes(b"\x7fELFcoredump")
        (app / "engine-in.log").write_text("position startpos\n")
        (app / "_trace").mkdir()
        (app / "_trace" / "spi.bin").write_bytes(b"\x00")
        (app / "_dbg_run.sh").write_text("#!/bin/sh\n")
    return app


# ---------------------------------------------------------------------------
# Pure detection / validation
# ---------------------------------------------------------------------------


def test_detect_app_dir_finds_app_regardless_of_depth(tmp_path):
    """The app must be located wherever it sits in the image.

    DGT Buildroot can place the app at /home/pi/centaur, /opt/centaur, etc. If
    detection regressed to a hardcoded path it would return None for this nested
    fixture and the import would wrongly report "app not found".
    """
    app = _make_app_tree(tmp_path / "mnt", subdir="opt/centaur")
    assert detect_app_dir(tmp_path / "mnt") == app


def test_detect_app_dir_returns_none_without_centaur_executable(tmp_path):
    """A directory of data files with no ``centaur`` executable is not the app.

    Guards against matching some unrelated ``engines``/``fonts`` directory: the
    executable named ``centaur`` is the unique anchor. Without it, detection must
    return None so the caller emits a clear "couldn't find app" error.
    """
    root = tmp_path / "mnt"
    (root / "engines").mkdir(parents=True)
    (root / "fonts").mkdir(parents=True)
    assert detect_app_dir(root) is None


def test_detect_app_dir_ignores_non_executable_named_centaur(tmp_path):
    """A plain data file literally named ``centaur`` must not match.

    The anchor is an *executable*; if detection matched any file named centaur it
    could pick a log or config and copy the wrong directory.
    """
    root = tmp_path / "mnt"
    (root / "data").mkdir(parents=True)
    plain = root / "data" / "centaur"
    plain.write_text("not a binary")
    plain.chmod(0o644)
    assert detect_app_dir(root) is None


def test_detect_app_dir_uses_mode_bits_not_exec_access(tmp_path, monkeypatch):
    """Detection must key off the file's execute mode bits, not os.access(X_OK).

    The image is loop-mounted read-only with ``noexec`` for safety. On Linux,
    access(X_OK) is denied on a noexec mount even when the file is mode 0755, so
    a detector built on os.access finds zero executables and the import wrongly
    reports "could not find the Centaur software" on a perfectly good image.
    This forces os.access to deny execute (what noexec does) and asserts the app
    is still located via its stat mode bits. If detection regresses to
    os.access, detect_app_dir returns None here and the assertion fails.
    """
    app = _make_app_tree(tmp_path / "mnt", subdir="home/pi/centaur")
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    assert detect_app_dir(tmp_path / "mnt") == app


def test_centaur_app_installed_requires_complete_tree(tmp_path):
    """The launch gate must reject a partial install, accept a complete one.

    Why this exists: a copy that lands the executable but not engines/fonts (the
    exact failure mode that shipped a half-installed Centaur) must not be treated
    as launchable -- launching it shows the splash and hangs with no engine/fonts.
    Asserts both directions so the gate can't regress to "executable exists".
    """
    app = tmp_path / "centaur"
    app.mkdir()
    binary = app / "centaur"
    binary.write_text("#!x")
    binary.chmod(0o755)
    # Executable present but engines/ and fonts/ absent: not launchable.
    assert centaur_app_installed(app) is False

    (app / "engines").mkdir()
    (app / "fonts").mkdir()
    # Full required set present: launchable.
    assert centaur_app_installed(app) is True


def test_validate_app_dir_ok_when_complete(tmp_path):
    """A complete app directory validates with no missing entries.

    The positive case for the precise-error contract below.
    """
    app = _make_app_tree(tmp_path)
    result = validate_app_dir(app)
    assert result.ok is True
    assert result.missing == ()


def test_validate_app_dir_reports_each_missing_entry(tmp_path):
    """Validation must name exactly which required entries are absent.

    A precise message ("missing: engines, fonts") is the difference between a
    user knowing the image is incomplete and a generic failure. If validation
    only checked one marker, a partial image would pass or fail opaquely.
    """
    app = tmp_path / "app"
    app.mkdir()
    binary = app / "centaur"
    binary.write_text("#!x")
    binary.chmod(0o755)
    result = validate_app_dir(app)
    assert result.ok is False
    assert set(result.missing) == {"engines", "fonts"}


def test_ignore_cruft_filters_only_debug_artifacts(tmp_path):
    """The copytree ignore filter drops debug cruft and keeps app files.

    Asserts both directions: real app entries survive and every cruft pattern
    (core dumps, *.log, _trace/, _dbg*.sh) is removed. A one-sided check would
    miss either over-deletion of app files or leaked debug artifacts.
    """
    names = [
        "centaur", "engines", "fonts",
        "core.1234", "core.999", "engine-in.log", "engine-err.log",
        "_trace", "_dbg_run.sh", "_dbgshim.sh",
    ]
    ignored = ignore_cruft(str(tmp_path), names)
    assert ignored == {
        "core.1234", "core.999", "engine-in.log", "engine-err.log",
        "_trace", "_dbg_run.sh", "_dbgshim.sh",
    }


# ---------------------------------------------------------------------------
# Orchestration (gzip + mount/umount injected)
# ---------------------------------------------------------------------------


class _FakeMounter:
    """Stands in for the privileged ``sudo`` mount/umount helper.

    On ``mount`` it populates the mountpoint with a fixture app tree (simulating
    the loop-mounted ext4); on ``stage`` it copies the named app dir into the
    named staging dir (simulating the root copy the real helper does); on
    ``umount`` it records the call. Records the ordered command verbs so tests can
    assert mount-before-stage-before-umount.
    """

    def __init__(self, mount_root: Path, *, cruft: bool = True, populate: bool = True):
        self.mount_root = Path(mount_root)
        self.cruft = cruft
        self.populate = populate
        self.calls: list[str] = []
        self.commands: list[list] = []

    def __call__(self, cmd, *args, **kwargs):
        self.commands.append(list(cmd))
        verb = next((c for c in cmd if c in ("mount", "stage", "umount")), None)
        self.calls.append(verb)
        if verb == "mount" and self.populate:
            self.mount_root.mkdir(parents=True, exist_ok=True)
            _make_app_tree(self.mount_root, subdir="home/pi/centaur", cruft=self.cruft)
        elif verb == "stage":
            # `stage <src> <dst>`: copy the detected app dir to the staging dir,
            # the way the privileged helper does before the unprivileged copy.
            idx = cmd.index("stage")
            src, dst = cmd[idx + 1], cmd[idx + 2]
            shutil.copytree(src, dst)
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def _fake_decompress(src: Path, dst: Path) -> None:
    """Stand-in for gunzip: the mount is faked, so content is irrelevant."""
    Path(dst).write_bytes(b"\x00")


def _install(tmp_path, **overrides):
    image = tmp_path / "centaur-sd.img.gz"
    image.write_bytes(b"\x1f\x8b")  # gzip magic; never actually decompressed
    dest = overrides.pop("dest", tmp_path / "dest" / "centaur")
    mount_root = overrides.pop("mount_root", tmp_path / "mnt")
    runner = overrides.pop("runner", _FakeMounter(mount_root))
    # The armhf runtime step is a real system reconfiguration; default it to a
    # no-op so the mount-focused tests are not perturbed by it (it has its own
    # dedicated tests). Tests that care pass their own ``ensure_runtime``.
    ensure_runtime = overrides.pop("ensure_runtime", lambda _runner: True)
    decompress = overrides.pop("decompress", _fake_decompress)
    return runner, install_from_image(
        image,
        dest,
        tmp_dir=tmp_path / "tmp",
        mount_root=mount_root,
        runner=runner,
        decompress=decompress,
        ensure_runtime=ensure_runtime,
        **overrides,
    )


def test_install_from_image_extracts_app_strips_cruft_and_sets_exec_bits(tmp_path):
    """End-to-end happy path: mounted app lands in dest, clean and runnable.

    Enters through the public entry point with only the true boundaries (gzip,
    sudo mount) faked. Asserts the full shape: app files present, every cruft
    pattern absent, the centaur binary and engine executable carry an exec bit,
    and mount precedes umount. A single presence check would miss cruft leakage
    or lost exec bits (which silently break launch).
    """
    runner, result = _install(tmp_path)
    dest = tmp_path / "dest" / "centaur"

    assert isinstance(result, InstallResult)
    assert Path(result.installed_path) == dest
    assert (dest / "centaur").is_file()
    assert (dest / "engines" / "stockfish_pi").is_file()
    assert (dest / "fonts" / "Font.ttf").is_file()
    # Cruft stripped.
    assert not (dest / "core.1234").exists()
    assert not (dest / "engine-in.log").exists()
    assert not (dest / "_trace").exists()
    assert not (dest / "_dbg_run.sh").exists()
    # Exec bits restored on the launchable files.
    assert os.access(dest / "centaur", os.X_OK)
    assert os.access(dest / "engines" / "stockfish_pi", os.X_OK)
    # Mounted, staged (root copy), then unmounted, in that order.
    assert runner.calls == ["mount", "stage", "umount"]
    # Temp decompressed image and staging copy cleaned up.
    assert not (tmp_path / "tmp" / "centaur-sd.img").exists()
    assert not (tmp_path / "tmp" / "centaur-stage").exists()


def test_install_from_image_invokes_helper_with_fixed_mountpoint_contract(tmp_path):
    """The mount helper is called with exactly the argv it accepts.

    Regression guard: the helper pins its own mountpoint, so its grammar is
    ``mount <image>`` (2 tokens) and ``umount`` (1 token). A previous bug passed
    the mountpoint as an extra argument, which pushed the token count past the
    helper's ``$# -eq N`` check, so the helper printed usage and exited 2 and
    every real import failed with "Failed to mount the uploaded image." Asserting
    the full argv (not just the verb) is what catches that extra-argument drift;
    a verb-only check passed straight through it.
    """
    runner, _ = _install(tmp_path)
    raw_image = str(tmp_path / "tmp" / "centaur-sd.img")
    app_dir = str(tmp_path / "mnt" / "home" / "pi" / "centaur")
    staging = str(tmp_path / "tmp" / "centaur-stage")

    mount_cmd = next(c for c in runner.commands if "mount" in c and "umount" not in c)
    stage_cmd = next(c for c in runner.commands if "stage" in c)
    umount_cmd = next(c for c in runner.commands if "umount" in c)

    assert mount_cmd == ["sudo", "-n", MOUNT_HELPER, "mount", raw_image]
    assert stage_cmd == ["sudo", "-n", MOUNT_HELPER, "stage", app_dir, staging]
    assert umount_cmd == ["sudo", "-n", MOUNT_HELPER, "umount"]


def test_install_from_image_raises_and_unmounts_when_app_absent(tmp_path):
    """A valid-looking image with no app fails clearly and still unmounts.

    If the image contains no ``centaur`` executable the import must raise rather
    than install an empty dir, and the mountpoint must be released regardless
    (the umount is in a finally) so a failed import does not wedge a loop device.
    """
    mount_root = tmp_path / "mnt"

    def runner(cmd, *a, **k):
        verb = next((c for c in cmd if c in ("mount", "umount")), None)
        runner.calls.append(verb)
        if verb == "mount":
            (mount_root / "etc").mkdir(parents=True, exist_ok=True)
        return types.SimpleNamespace(returncode=0)

    runner.calls = []
    with pytest.raises(CentaurImportError):
        _install(tmp_path, mount_root=mount_root, runner=runner)
    assert runner.calls == ["mount", "umount"]
    assert not (tmp_path / "tmp" / "centaur-sd.img").exists()


def test_install_from_image_preserves_existing_display_shim(tmp_path):
    """Re-import must not delete the UC-built display shim from CENTAUR_HOME.

    spishim.so is a UC artifact (not on the SD); translate mode needs it. The
    import wipes dest before copying the fresh app, so without explicit
    preservation a re-import would strip the shim and break translate mode. A
    stale app file present beforehand must still be gone after.
    """
    dest = tmp_path / "dest" / "centaur"
    dest.mkdir(parents=True)
    (dest / "spishim.so").write_bytes(b"SHIMDATA")
    (dest / "stale-old-file").write_text("remove me")

    _install(tmp_path, dest=dest)

    assert (dest / "spishim.so").read_bytes() == b"SHIMDATA"
    assert not (dest / "stale-old-file").exists()
    assert (dest / "centaur").is_file()


def test_install_from_image_installs_engine_proxy_hook(tmp_path):
    """Import must replace engines/stockfish_pi with the UC proxy launcher.

    The whole point of the engine hook is that, after import, Centaur execs the
    UC proxy instead of the patched Stockfish. The SD ships its own stockfish_pi
    wrapper; if the import did not overwrite it, Centaur would keep using the
    modified engine and bypass UC engines + game recording. Asserts the launcher
    runs the proxy module and stays executable.
    """
    _, result = _install(tmp_path)
    hook = Path(result.installed_path) / "engines" / "stockfish_pi"
    assert hook.is_file()
    assert os.access(hook, os.X_OK)
    assert "universalchess.services.centaur_engine_proxy" in hook.read_text()


# ---------------------------------------------------------------------------
# Factory marker (settings/factory.info)
# ---------------------------------------------------------------------------


def test_install_from_image_seeds_factory_marker(tmp_path):
    """A fresh import must leave settings/factory.info so Centaur boots to play.

    Without this 0-byte marker Centaur boots into its factory hardware-test +
    field-calibration "Test Screen" on every launch (the marker is only written
    when that calibration completes, which it never does in this integration). The
    SD app the importer copies has no settings/ at all, so the import must seed the
    marker itself. If this regresses, an imported board is trapped on the Test
    Screen. Asserts the file exists and is empty (presence, not data, is the flag).
    """
    _, result = _install(tmp_path)
    marker = Path(result.installed_path) / "settings" / "factory.info"
    assert marker.is_file()
    assert marker.stat().st_size == 0


def test_ensure_factory_marker_creates_empty_marker_when_missing(tmp_path):
    """ensure_factory_marker creates an empty settings/factory.info when absent.

    This is the guard both the launcher and the importer rely on. It must create
    the settings/ dir if needed and report True (created). A non-empty or absent
    file would mean Centaur still runs (or mis-reads) factory setup.
    """
    app = tmp_path / "centaur"
    app.mkdir()

    created = ensure_factory_marker(app)

    marker = app / "settings" / "factory.info"
    assert created is True
    assert marker.is_file()
    assert marker.stat().st_size == 0


# ---------------------------------------------------------------------------
# armhf support provisioning (sudo helper injected)
# ---------------------------------------------------------------------------


def test_install_from_image_provisions_armhf_support_with_import_runner(tmp_path):
    """Import must provision armhf support, passing it the same sudo runner.

    The imported binary is a 32-bit armhf ELF; on a 64-bit host it cannot exec
    until the runtime is installed, and its shim cannot be built until the cross
    toolchain is installed. If this step were dropped, an imported board would
    fail to launch Centaur (missing loader) or fail the shim build with no hint
    why. Asserts the step ran exactly once and received the injected runner (so
    the real call goes through the pinned sudo helper, not a shell).
    """
    seen = []

    def spy_runtime(runner):
        seen.append(runner)

    runner, _ = _install(tmp_path, ensure_runtime=spy_runtime)
    assert len(seen) == 1
    assert seen[0] is runner


def test_install_from_image_reports_ordered_progress_stages(tmp_path):
    """Import must emit each stage in order so the UI can show what it is doing.

    Why this test exists: the feature that motivated this is a progress bar that
    froze at 100% because the post-upload install emitted nothing. install_from_image
    must call the injected ``stage_callback`` once per phase, in pipeline order, so
    the polled status reflects decompress -> mount -> ... -> 32-bit support ->
    finalize. INSTALLING_ARMHF must be reported even though the runtime step is a
    no-op here, because on arm64 that is the long, previously-silent phase.

    How the regression manifests: dropping a callback at any step (or emitting them
    out of order) changes this list -- e.g. omitting INSTALLING_ARMHF would leave
    the bar stuck on "Installing Centaur software..." through the long apt run,
    the exact confusion this feature removes.
    """
    from universalchess.services.centaur_import.import_state import ImportStage

    stages = []
    _install(tmp_path, stage_callback=lambda stage, _message: stages.append(stage))

    assert stages == [
        ImportStage.DECOMPRESSING,
        ImportStage.MOUNTING,
        ImportStage.VALIDATING,
        ImportStage.STAGING,
        ImportStage.INSTALLING_FILES,
        ImportStage.INSTALLING_ARMHF,
        ImportStage.CONFIGURING,
        ImportStage.FINALIZING,
    ]


def test_install_from_image_without_callback_still_installs(tmp_path):
    """A None stage_callback (the default) must not break the install.

    Why this test exists: stage reporting is additive; callers that do not care
    about progress (and every existing test) must keep working with no callback.
    Guards that the emission points are null-safe.

    How the regression manifests: calling ``stage_callback(...)`` without a None
    guard would raise TypeError here and abort every import that omits the arg.
    """
    _, result = _install(tmp_path, stage_callback=None)
    assert Path(result.installed_path).joinpath("centaur").is_file()


def test_install_from_image_fails_when_armhf_support_step_fails(tmp_path):
    """An armhf-provisioning failure must fail the whole import.

    Centaur is a 32-bit armhf binary that cannot run without the armhf runtime and
    cannot build its shim without the cross toolchain, so an install that copied
    the files but could not provision these is not usable. Returning success there
    would hand back an install that fails to launch with no explanation. Asserts
    the error propagates as CentaurImportError so the web layer surfaces it (400)
    and the user retries.
    """
    def failing_runtime(_runner):
        raise CentaurImportError("armhf support install failed")

    with pytest.raises(CentaurImportError):
        _install(tmp_path, ensure_runtime=failing_runtime)


def test_ensure_armhf_support_invokes_pinned_helper_via_sudo_n(tmp_path):
    """The helper must be invoked as `sudo -n <pinned helper>`, no args.

    The security boundary is a passwordless sudo grant to exactly this fixed
    helper. `sudo -n` fails fast if the grant is missing (rather than hanging on
    a prompt), and the helper takes no caller arguments so the grant cannot become
    a general apt-install. Asserts the exact argv; an extra token or a missing
    `-n` would either break the grant match or hang the import.
    """
    seen = {}

    def runner(cmd, *a, **k):
        seen["cmd"] = list(cmd)
        seen["kwargs"] = k
        return types.SimpleNamespace(returncode=0)

    # A zero exit is the success path: must return normally (no exception).
    ensure_armhf_support(runner)
    assert seen["cmd"] == ["sudo", "-n", ARMHF_SETUP_HELPER]
    # capture_output keeps helper chatter out of the app log; a bounded timeout
    # stops a wedged apt from hanging the web request forever.
    assert seen["kwargs"].get("capture_output") is True
    assert seen["kwargs"].get("timeout")


_ARMHF_SUPPORT_FAILED_MSG = (
    "Could not install the 32-bit armhf support Centaur needs to run on "
    "this system. In Settings > System, use Check for OS updates, then try "
    "the import again."
)


def test_ensure_armhf_support_raises_on_nonzero_exit(tmp_path):
    """A non-zero helper exit must raise CentaurImportError, not return quietly.

    The armhf support is required, so a failed install must abort the import rather
    than be swallowed. Asserts the raise so the required-step contract holds and
    the message names Settings > System and Check for OS updates -- the UI that
    refreshes apt indexes -- rather than a generic network hint that leaves the
    user with no in-app next step.
    """
    def runner(cmd, *a, **k):
        return types.SimpleNamespace(returncode=1)

    with pytest.raises(CentaurImportError) as exc:
        ensure_armhf_support(runner)
    assert str(exc.value) == _ARMHF_SUPPORT_FAILED_MSG


def test_ensure_armhf_support_raises_when_sudo_missing(tmp_path):
    """A missing sudo binary (OSError) must raise CentaurImportError.

    On a host without sudo, or if the helper path is absent, the subprocess call
    raises OSError. That is still a failure to provision the required support, so
    it must surface as a CentaurImportError (with a clean message) rather than a
    raw OSError leaking to the client. The same Settings > System copy as the
    apt-failure path is asserted so the two cannot drift into different advice.
    """
    def runner(cmd, *a, **k):
        raise FileNotFoundError("sudo")

    with pytest.raises(CentaurImportError) as exc:
        ensure_armhf_support(runner)
    assert str(exc.value) == _ARMHF_SUPPORT_FAILED_MSG


def test_ensure_factory_marker_is_idempotent_and_preserves_content(tmp_path):
    """A second call is a no-op: it must not recreate or truncate the marker.

    The marker can pre-exist (imported from a factory-set card, or seeded by a
    prior launch). Re-running the guard must report False (not created) and leave
    any existing file untouched, so it never clobbers real state. If it truncated
    unconditionally, it would be destructive on every launch.
    """
    app = tmp_path / "centaur"
    (app / "settings").mkdir(parents=True)
    marker = app / "settings" / "factory.info"
    marker.write_bytes(b"preexisting")

    created = ensure_factory_marker(app)

    assert created is False
    assert marker.read_bytes() == b"preexisting"


# ---------------------------------------------------------------------------
# Event-log diagnostics
# ---------------------------------------------------------------------------
# Users who could not import their original Centaur left no diagnosable trail:
# the mount/stage/armhf helpers ran with capture_output=True and their stderr was
# thrown away, and any other failure reached only the root logger's ~/debug.log,
# which is truncated on every boot. These tests pin the contract that every step
# and every failure lands in the persistent event log the Settings > System
# viewer reads, carrying the detail (argv, exit code, helper output) that
# distinguishes a missing sudoers grant from a stale apt index from a full disk.


@pytest.fixture
def recorded_events(tmp_path, monkeypatch):
    """Redirect the event log to a temp file and read it back on demand.

    The real log lives at /var/lib/universalchess/logs/events.jsonl, which is
    neither writable nor isolated in tests. Returns a callable (not a snapshot)
    so each assertion reads the events written by the call it just made.
    """
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(path))
    # The module caches one logger per resolved path; a stale handler from an
    # earlier test's tmp_path would send this test's records to a deleted file.
    monkeypatch.setattr(event_log, "_loggers", {})
    return lambda: event_log.read_events(path=path)


def _errors(events):
    """The messages of every error-level record, newest first."""
    return [e["message"] for e in events if e["level"] == "error"]


def _only_error(events):
    """The single error-level message, asserting exactly one was recorded."""
    errors = _errors(events)
    assert len(errors) == 1, f"expected exactly one error event, got {errors}"
    return errors[0]


class _FailingHelper(_FakeMounter):
    """A mount helper that fails one named verb with realistic helper output.

    Extends the happy-path fake so the steps before the failing verb behave
    normally (the image mounts, the app is detected), which is what makes the
    failure land at the intended stage.
    """

    def __init__(self, mount_root, verb, *, returncode, stderr):
        super().__init__(mount_root)
        self.failing_verb = verb
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, cmd, *args, **kwargs):
        result = super().__call__(cmd, *args, **kwargs)
        if self.calls[-1] == self.failing_verb:
            return types.SimpleNamespace(returncode=self.returncode, stdout=b"", stderr=self.stderr)
        return result


@pytest.mark.parametrize(
    "verb, returncode, stderr",
    [
        ("mount", 32, b"mount: /tmp/centaur-mnt: wrong fs type, bad option, bad superblock.\n"),
        ("stage", 1, b"cp: cannot open 'engines/stockfish_pi' for reading: Permission denied\n"),
        ("umount", 16, b"umount: /tmp/centaur-mnt: target is busy.\n"),
    ],
)
def test_helper_failure_records_argv_exit_code_and_output(
    tmp_path, recorded_events, verb, returncode, stderr
):
    """Every failing mount-helper verb must log its argv, exit code and stderr.

    Why this test exists: the helper ran with capture_output=True and the result
    was checked only for a non-zero returncode -- the captured stderr was
    discarded, so "Failed to mount the uploaded image." was the entire record of
    a failed import. The three causes users actually hit (a missing sudoers
    grant, an unreadable app dir on the SD, a busy mountpoint) are only
    distinguishable from that text.

    How the regression manifests: dropping the stderr capture leaves an error
    event with no helper output, so the stderr substring assertion fails; losing
    the argv or exit code makes the corresponding assertion fail while the
    import still reports the same generic message to the user.
    """
    runner = _FailingHelper(tmp_path / "mnt", verb, returncode=returncode, stderr=stderr)

    with pytest.raises(CentaurImportError):
        _install(tmp_path, runner=runner)

    message = _only_error(recorded_events())
    assert MOUNT_HELPER in message
    assert f"sudo -n {MOUNT_HELPER} {verb}" in message
    assert f"exit code {returncode}" in message
    assert stderr.decode().strip() in message


def test_helper_timeout_records_the_limit_it_exceeded(tmp_path, recorded_events):
    """A helper that never returns must log the timeout, not vanish.

    Why this test exists: a wedged loop-mount raises TimeoutExpired, which is not
    an OSError and was caught by nothing in the installer. It surfaced as the web
    worker's generic "Import failed" with the reason only in the boot-truncated
    debug log, so a board that consistently times out looked identical to every
    other failure.

    How the regression manifests: without the TimeoutExpired branch the
    exception escapes install_from_image as a bare SubprocessError, so the
    pytest.raises(CentaurImportError) fails before the log is even read.
    """
    def runner(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=b"", stderr=b"")

    with pytest.raises(CentaurImportError):
        _install(tmp_path, runner=runner)

    message = _only_error(recorded_events())
    assert "timed out" in message
    assert "120s" in message


def test_missing_sudo_binary_is_recorded_as_a_start_failure(tmp_path, recorded_events):
    """A helper that cannot be started at all must say so, distinctly.

    Why this test exists: when the sudoers grant or the helper file is missing
    the subprocess raises OSError before any exit code exists. That is a
    deployment fault with a different fix than a non-zero exit, so the log must
    name the exception rather than report a fabricated exit code.

    How the regression manifests: reporting "exit code None" (or letting the
    OSError escape as an unexpected error) loses the distinction, and the
    OSError-name assertion below fails.
    """
    def runner(cmd, *args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", "sudo")

    with pytest.raises(CentaurImportError):
        _install(tmp_path, runner=runner)

    message = _only_error(recorded_events())
    assert "could not be started" in message
    assert "FileNotFoundError" in message
    assert "exit code" not in message


def test_armhf_helper_failure_records_the_apt_output(recorded_events):
    """The armhf provisioning failure must carry the apt output that explains it.

    Why this test exists: this step is the longest and the most failure-prone
    (it runs apt on a board whose indexes are often stale), and its user-facing
    message is a single fixed sentence. Without the helper's own output there is
    nothing to distinguish "no network" from "unable to locate package" from
    "dpkg was interrupted" -- three problems with three different fixes.

    How the regression manifests: dropping the capture leaves the error event
    without the apt line, so the "Unable to locate package" assertion fails
    while the user-facing message is unchanged.
    """
    def runner(cmd, *args, **kwargs):
        return types.SimpleNamespace(
            returncode=100,
            stdout=b"Reading package lists...\n",
            stderr=b"E: Unable to locate package libc6:armhf\n",
        )

    with pytest.raises(CentaurImportError):
        ensure_armhf_support(runner)

    message = _only_error(recorded_events())
    assert ARMHF_SETUP_HELPER in message
    assert "exit code 100" in message
    assert "E: Unable to locate package libc6:armhf" in message
    # Both streams are kept: the helper reports progress on stdout and the
    # reason on stderr, and which one carries the diagnosis varies by failure.
    assert "Reading package lists..." in message


def test_armhf_helper_timeout_is_recorded(recorded_events):
    """A stuck apt run must be logged as a timeout with its limit.

    Why this test exists: apt can block for the full ten-minute budget on a lock
    or an unreachable mirror. That was indistinguishable from an instant failure
    because neither the duration nor the timeout was recorded anywhere.

    How the regression manifests: without the TimeoutExpired branch the
    exception escapes as SubprocessError instead of CentaurImportError and the
    raises assertion fails.
    """
    def runner(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    with pytest.raises(CentaurImportError):
        ensure_armhf_support(runner)

    message = _only_error(recorded_events())
    assert "timed out" in message
    assert "600s" in message


def test_recorded_helper_output_is_one_bounded_line_keeping_the_tail(recorded_events):
    """Verbose helper output must be collapsed, bounded, and tail-preserving.

    Why this test exists: an apt run emits thousands of progress lines. Each
    event is one JSON-lines record rendered on one row, and the file rotates at
    1 MB, so logging the output verbatim would both break the viewer's layout
    and evict the rest of the board's history. apt prints the reason for a
    failure last, so the tail is the part worth keeping.

    How the regression manifests: logging the raw stream leaves embedded
    newlines (the single-line assertion fails) and an unbounded message (the
    length assertion fails); truncating the tail instead of the head drops the
    "E:" line the whole record exists for.
    """
    noise = b"Get:1 http://deb.debian.org/debian bookworm InRelease [151 kB]\n" * 500

    def runner(cmd, *args, **kwargs):
        return types.SimpleNamespace(
            returncode=100, stdout=noise, stderr=b"E: dpkg was interrupted\n"
        )

    with pytest.raises(CentaurImportError):
        ensure_armhf_support(runner)

    message = _only_error(recorded_events())
    assert "\n" not in message
    assert len(message) < 1200
    assert "E: dpkg was interrupted" in message
    assert message.count("Get:1 http") < 20


def test_corrupt_upload_records_the_decompress_failure_with_an_actionable_error(
    tmp_path, recorded_events
):
    """A truncated or corrupt .img.gz must name decompression as the failure.

    Why this test exists: an interrupted browser upload is the single most likely
    cause of "I can't import my Centaur", and it lands in gzip. That exception
    was caught only by the web worker's catch-all, which stored the word "Import
    failed" and wrote the real reason to the boot-truncated debug log -- so the
    most common failure was the least diagnosable one.

    How the regression manifests: without the decompress guard the gzip error
    propagates as a bare BadGzipFile (this raises-CentaurImportError assertion
    fails) and nothing reaches the event log.
    """
    def bad_decompress(src, dst):
        raise gzip.BadGzipFile("Not a gzipped file (b'\\x00\\x00')")

    with pytest.raises(CentaurImportError) as exc:
        _install(tmp_path, decompress=bad_decompress)

    # The user gets a step-specific next action, not the old generic failure.
    assert "decompress" in str(exc.value).lower()
    message = _only_error(recorded_events())
    assert "BadGzipFile" in message
    assert "Not a gzipped file" in message


def test_image_without_the_app_records_where_it_looked(tmp_path, recorded_events):
    """An image with no Centaur program must log the detection failure.

    Why this test exists: "could not find the Centaur software" is reported to
    the user but was never recorded, so a support request about a wrongly-imaged
    SD card (the boot partition instead of the root partition) left nothing to
    confirm the image really was searched and really was empty.

    How the regression manifests: no error event is written, so _only_error
    raises on an empty list.
    """
    mount_root = tmp_path / "mnt"

    def runner(cmd, *args, **kwargs):
        if "mount" in cmd and "umount" not in cmd:
            (mount_root / "etc").mkdir(parents=True, exist_ok=True)
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with pytest.raises(CentaurImportError):
        _install(tmp_path, mount_root=mount_root, runner=runner)

    message = _only_error(recorded_events())
    assert "no centaur program found" in message.lower()
    assert str(mount_root) in message


def test_incomplete_image_records_each_missing_entry(tmp_path, recorded_events):
    """An image missing engines/fonts must log exactly which entries were absent.

    Why this test exists: validation already names the missing entries to the
    user, but that message disappears with the page. Recording it is what lets a
    later support request confirm the SD card itself was incomplete rather than
    the import being at fault.

    How the regression manifests: the error event omits the entry names, so the
    "fonts" assertion fails even though the import still fails correctly.
    """
    mount_root = tmp_path / "mnt"

    def runner(cmd, *args, **kwargs):
        if "mount" in cmd and "umount" not in cmd:
            app = mount_root / "home" / "pi" / "centaur"
            (app / "engines").mkdir(parents=True, exist_ok=True)
            binary = app / "centaur"
            binary.write_text("#!fake centaur\n")
            binary.chmod(0o755)
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with pytest.raises(CentaurImportError):
        _install(tmp_path, mount_root=mount_root, runner=runner)

    # fonts is the only absent entry, so it must be named and engines must not:
    # a record that listed every required entry would not distinguish a partial
    # card from an empty one.
    message = _only_error(recorded_events())
    assert "fonts" in message
    assert "engines" not in message


def test_full_disk_during_the_copy_records_the_errno_and_free_space(tmp_path, recorded_events):
    """A failed copy into CENTAUR_HOME must log the OS error and the space left.

    Why this test exists: the import writes a ~2 GB raw image and then a full
    copy of the app, so ENOSPC is a real outcome on a small card. It surfaced as
    the generic "Import failed" with no hint that the board was simply out of
    room. Recording the free space alongside the errno is what turns that into a
    one-line diagnosis.

    How the regression manifests: without the copy guard the OSError reaches the
    web worker's catch-all (this raises-CentaurImportError assertion fails); with
    the guard but no disk-usage detail, the "free space" assertion fails.
    """
    def failing_copytree(src, dst, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(CentaurImportError):
        _install(tmp_path, copytree=failing_copytree)

    message = _only_error(recorded_events())
    assert "No space left on device" in message
    assert "free space" in message


def test_successful_import_records_each_stage_and_a_timed_completion(tmp_path, recorded_events):
    """A successful import must leave an ordered, timed record of every stage.

    Why this test exists: live progress is lost the moment the page closes or the
    process restarts, so an import that stalls (or one a user says "did nothing")
    could not be placed at a stage after the fact. The persistent timeline is
    what identifies the step that hung; the completion record is the baseline a
    slow import is compared against.

    How the regression manifests: dropping the event write from a stage removes
    that entry and shifts the list, failing the equality assertion; omitting the
    completion record fails the duration/file-count assertions. All records must
    carry the centaur_import category, which is what the viewer badges on.
    """
    _, result = _install(tmp_path)

    events = recorded_events()
    assert [e["category"] for e in events] == [EVENT_CATEGORY] * len(events)
    assert [e["level"] for e in events] == ["info"] * len(events)

    # read_events returns newest first; the pipeline order reads oldest first.
    oldest_first = list(reversed(events))
    assert [e["message"] for e in oldest_first[:8]] == [
        "Decompressing image...",
        "Mounting SD image...",
        "Validating Centaur files...",
        "Reading image contents...",
        "Installing Centaur software...",
        "Installing 32-bit support...",
        "Configuring engine proxy...",
        "Finalizing...",
    ]

    completion = events[0]
    assert completion["duration_ms"] >= 0
    assert str(result.file_count) in completion["message"]
    assert len(events) == 9
