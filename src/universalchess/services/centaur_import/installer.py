"""Orchestrate decompress -> loop-mount -> extract -> install of a Centaur image.

The privileged loop-mount/umount (root) and gzip decompression are injected so
the flow is testable without root; the default wiring uses the pinned sudo helper
``scripts/centaur-import-mount`` and a streaming gunzip.
"""

import gzip
import os
import shutil
import subprocess  # nosec B404 - only the pinned, path-validated mount/runtime helpers are invoked
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from universalchess.paths import CENTAUR_HOME, SCRIPTS_DIR, TMP_DIR
from universalchess.services.centaur_import.detection import (
    detect_app_dir,
    ignore_cruft,
    validate_app_dir,
)
from universalchess.services.centaur_import.import_state import ImportStage

# Pinned root helper that loop-mounts/unmounts the image read-only at a fixed
# mountpoint (see scripts/centaur-import-mount and the postinst sudoers grant).
MOUNT_HELPER = os.path.join(SCRIPTS_DIR, "centaur-import-mount")

# Pinned root helper that provisions armhf support for the imported (armhf)
# Centaur binary on a 64-bit host: the armhf runtime (so it can execute) and the
# armhf cross toolchain (so the display shim can be built for its ABI). See
# scripts/centaur-armhf-setup and the postinst sudoers grant.
ARMHF_SETUP_HELPER = os.path.join(SCRIPTS_DIR, "centaur-armhf-setup")

# Surfaced to the client when the helper cannot run or exits non-zero. Apt
# failures on a fresh Armbian image are often stale or missing indexes, which
# Settings > System -> Check for OS updates refreshes; a generic "check the
# network" line left no in-app next step. Author-written and path-free so it
# is safe to return over HTTP (CWE-209).
ARMHF_SUPPORT_FAILED_MSG = (
    "Could not install the 32-bit armhf support Centaur needs to run on "
    "this system. In Settings > System, use Check for OS updates, then try "
    "the import again."
)

# Fixed read-only mountpoint the helper uses; kept under TMP_DIR so it shares the
# service-owned, writable tree and matches the helper's own path allow-list.
DEFAULT_MOUNT_ROOT = os.path.join(TMP_DIR, "centaur-mnt")

# UC artifacts in CENTAUR_HOME that are not part of the SD app and must survive a
# re-import (the install wipes the destination before copying the fresh app).
# The shim's source-hash stamp is preserved alongside the shim itself so a
# re-import does not orphan the stamp and force a needless rebuild on next launch.
_PRESERVE_ON_REINSTALL = ("spishim.so", "spishim.so.srchash")

# Relative path of the marker Centaur writes when its one-time factory hardware-
# test + field-calibration sequence completes (see ``ensure_factory_marker``).
_FACTORY_MARKER_RELPATH = ("settings", "factory.info")


class CentaurImportError(Exception):
    """Raised when an uploaded image cannot be turned into a usable install.

    Message text is surfaced to the user (e.g. "missing required files"), so it
    is written to be actionable and free of internal paths.
    """


@dataclass(frozen=True)
class InstallResult:
    """Successful-install summary returned to the caller / web response."""

    app_dir: str
    installed_path: str
    file_count: int


def _gunzip_to(src, dst) -> None:
    """Stream-decompress a .gz image to ``dst`` without buffering it in memory."""
    with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=4 * 1024 * 1024)


def _run_helper(runner: Callable, verb: str, *helper_args) -> None:
    """Invoke the pinned mount helper with ``sudo -n`` and raise on failure.

    ``sudo -n`` fails fast if the grant is missing rather than blocking on a
    password prompt. A non-zero result means the mount/umount did not happen, so
    it is escalated as a CentaurImportError instead of silently continuing.
    """
    cmd = ["sudo", "-n", MOUNT_HELPER, verb, *helper_args]
    result = runner(cmd, capture_output=True, timeout=120)
    if getattr(result, "returncode", 0) != 0:
        raise CentaurImportError(f"Failed to {verb} the uploaded image.")


def _apply_exec_bits(dest: Path) -> None:
    """Restore execute bits on the launchable files after the copy.

    copytree preserves source mode, but the read-only mount can present engine
    files without an exec bit; the launcher and Centaur both exec these, so set
    0o755 explicitly on the binary and every engine file.
    """
    binary = dest / "centaur"
    if binary.is_file():
        binary.chmod(0o755)  # nosec B103 - executable must carry the exec bit; 0o755 is least-permissive
    engines = dest / "engines"
    if engines.is_dir():
        for path in engines.rglob("*"):
            if path.is_file():
                path.chmod(0o755)  # nosec B103 - engines are executed; 0o755 is least-permissive


def ensure_factory_marker(app_dir=CENTAUR_HOME) -> bool:
    """Ensure ``settings/factory.info`` exists so Centaur boots to play, not test.

    Centaur writes this 0-byte marker only when its factory hardware-test + field-
    calibration sequence completes; on a normal boot it is never written. A real
    (factory-calibrated) board carries the marker from its one-time factory setup,
    so it boots straight to play. The SD import rebuilds ``settings/`` from scratch
    at runtime, so the marker is absent on a fresh install -- and without it
    Centaur re-enters the factory "Test Screen" on every launch. Worse, that
    calibration never completes in this integration, so the marker is never
    written and the board is trapped in the loop permanently.

    Creating the empty marker restores the board's true state (it was already
    factory-calibrated): this writes a presence flag, not calibration data, so it
    does not fabricate measurements -- the working reference card confirms the
    file is empty. ``settings/`` is created if missing. Idempotent.

    Returns True if the marker was created, False if it already existed.
    """
    marker = Path(app_dir).joinpath(*_FACTORY_MARKER_RELPATH)
    if marker.exists():
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    # Match the reference card's owner-only mode; content is irrelevant (presence
    # is the signal), so an empty 0o700 file is the minimal correct marker.
    marker.chmod(0o700)  # nosec B103 - owner-only is least-permissive for this flag
    return True


def ensure_armhf_support(runner: Callable = subprocess.run) -> None:
    """Provision the armhf runtime + shim toolchain the imported Centaur needs.

    The Centaur program is a 32-bit armhf ELF. On a 64-bit (arm64) host it cannot
    execute until the armhf foreign architecture is enabled and ``libc6:armhf`` is
    installed, and its display "translate" shim cannot be built until the armhf
    cross toolchain (``gcc-arm-linux-gnueabihf`` + ``libc6-dev-armhf-cross``) is
    present -- the native aarch64 ``gcc`` physically cannot emit centaur's armhf
    ABI.     Kernels without ``CONFIG_COMPAT=y`` also need ``qemu-user-static`` and a
    registered ``qemu-arm`` binfmt handler or the ELF fails with Exec format
    error. The helper installs that package and registers the handler only
    after running an AArch32 binary and seeing ``ENOEXEC``; Raspberry Pi OS
    64-bit executes AArch32 natively, so it never gets qemu-arm. This
    invokes the pinned ``centaur-armhf-setup`` helper via ``sudo -n``; the helper
    is arch-guarded and installs only what is missing, so it is a fast no-op
    (exit 0) on a native armhf host or an already-provisioned board.

    This is a required step, not best-effort: Centaur will neither run nor build
    its shim without these, so a failure raises CentaurImportError to fail the
    whole import. Reporting success on a half-provisioned system would hand back
    an install that cannot launch; failing loudly makes the user retry (a
    re-import wipes and redoes the tree). The message is ARMHF_SUPPORT_FAILED_MSG.
    """
    cmd = ["sudo", "-n", ARMHF_SETUP_HELPER]
    try:
        result = runner(cmd, capture_output=True, timeout=600)
    except OSError as exc:
        raise CentaurImportError(ARMHF_SUPPORT_FAILED_MSG) from exc
    if getattr(result, "returncode", 0) != 0:
        raise CentaurImportError(ARMHF_SUPPORT_FAILED_MSG)


def install_from_image(
    image_path,
    dest=CENTAUR_HOME,
    *,
    tmp_dir=TMP_DIR,
    mount_root=DEFAULT_MOUNT_ROOT,
    runner: Callable = subprocess.run,
    decompress: Callable = _gunzip_to,
    copytree: Callable = shutil.copytree,
    install_hook: Callable = None,
    ensure_runtime: Callable = ensure_armhf_support,
    stage_callback: Callable = None,
) -> InstallResult:
    """Install the Centaur app from a gzip ext4 image into ``dest``.

    Steps: decompress to a temp image, loop-mount it read-only, detect+validate
    the app, copy it to ``dest`` (debug cruft stripped, UC shim preserved), set
    exec bits, install the UC engine-proxy hook over ``engines/stockfish_pi``,
    then always unmount and delete the temp image. Raises CentaurImportError with
    an actionable message if the app is absent or the file set is incomplete.

    ``install_hook`` is the engine-proxy hook installer (injected for tests);
    defaults to the real one. Routing Centaur's engine through the proxy is part
    of producing a ready-to-use install, so it happens here.

    ``ensure_runtime`` provisions the 32-bit armhf runtime + shim toolchain the
    imported binary needs on a 64-bit host (injected for tests). It runs after the
    binary is in place and raises CentaurImportError on failure -- Centaur cannot
    run or build its shim without it, so a failure fails the whole import. See
    ``ensure_armhf_support``.

    ``stage_callback`` (optional) is called ``callback(ImportStage, message)`` at
    the start of each phase so a caller running this on a background thread can
    surface live progress to the web UI. It is null-safe: ``None`` (the default)
    means no reporting, so callers that do not track progress are unaffected. The
    long, otherwise-silent phase on a 64-bit host is INSTALLING_ARMHF (the apt run
    inside ``ensure_runtime``); it is reported before that call.
    """
    def report(stage, message):
        # Progress reporting is additive: a caller that passes no callback (every
        # non-web caller and the existing tests) must be unaffected.
        if stage_callback is not None:
            stage_callback(stage, message)

    if install_hook is None:
        from universalchess.services.centaur_engine_proxy.hook import install_engine_hook

        install_hook = install_engine_hook
    image_path = Path(image_path)
    dest = Path(dest)
    tmp_dir = Path(tmp_dir)
    mount_root = Path(mount_root)

    tmp_dir.mkdir(parents=True, exist_ok=True)
    raw_image = tmp_dir / "centaur-sd.img"
    # Root-owned, world-readable copy of the app made by the helper while the
    # image is mounted. The unprivileged copy below reads from here, not from the
    # mount: the SD app's engines/fonts/books dirs are commonly mode 0700 or owned
    # by another uid, so the service user cannot recurse into them off the
    # read-only mount and copytree fails with EPERM. Staging as root sidesteps
    # that without giving the copy step any privilege.
    staging = tmp_dir / "centaur-stage"
    report(ImportStage.DECOMPRESSING, "Decompressing image...")
    decompress(image_path, raw_image)

    try:
        # The helper pins its own read-only mountpoint (its security boundary),
        # so it takes only the image: `mount <image>`. ``mount_root`` here must
        # equal the helper's fixed MNT (both default to TMP_DIR/centaur-mnt); it
        # is where this process reads the mounted tree from, not an argument to
        # the helper. Passing it to the helper pushes its token count past the
        # `$# -eq 2` check and the mount silently fails.
        report(ImportStage.MOUNTING, "Mounting SD image...")
        _run_helper(runner, "mount", str(raw_image))
        try:
            # Detection/validation only need to *see* the centaur file and stat
            # engines/fonts (search on the parent), which the service user can do
            # off the mount; reading *into* the restricted dirs is what needs root.
            report(ImportStage.VALIDATING, "Validating Centaur files...")
            app_dir = detect_app_dir(mount_root)
            if app_dir is None:
                raise CentaurImportError(
                    "Could not find the Centaur software in the uploaded image "
                    "(no 'centaur' program next to 'engines' and 'fonts')."
                )
            validation = validate_app_dir(app_dir)
            if not validation.ok:
                raise CentaurImportError(
                    "The uploaded image is missing required Centaur files: "
                    + ", ".join(validation.missing)
                )
            report(ImportStage.STAGING, "Reading image contents...")
            _run_helper(runner, "stage", str(app_dir), str(staging))
        finally:
            # umount takes no argument -- the helper unmounts its fixed MNT.
            _run_helper(runner, "umount")

        # The mount is released; the staged copy is service-readable. Apply the
        # cruft-stripping / shim-preserving copy from staging into the install dir.
        report(ImportStage.INSTALLING_FILES, "Installing Centaur software...")
        _install_app_dir(staging, dest, copytree)
    finally:
        if raw_image.exists():
            raw_image.unlink()
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    _apply_exec_bits(dest)
    # The imported binary is a 32-bit armhf ELF; on a 64-bit host it cannot exec,
    # and its display shim cannot be built, until the armhf runtime and cross
    # toolchain are installed. Do it now, having just confirmed and made the binary
    # executable. Required, not optional: this raises and fails the import if the
    # support cannot be installed (Centaur would not run or would launch un-shimmed).
    # This is the long, otherwise-silent phase on a 64-bit host (apt), so it is
    # reported before the call; on a native armhf host it is a fast no-op.
    report(ImportStage.INSTALLING_ARMHF, "Installing 32-bit support...")
    ensure_runtime(runner)
    # Route Centaur's engine through the UC proxy (any UC engine + game recording).
    report(ImportStage.CONFIGURING, "Configuring engine proxy...")
    install_hook(dest / "engines")
    # A freshly imported tree has no settings/factory.info, so Centaur would boot
    # into the factory Test Screen. Seed the marker so the import yields a board
    # that boots straight to play (see ensure_factory_marker for the full why).
    report(ImportStage.FINALIZING, "Finalizing...")
    ensure_factory_marker(dest)
    file_count = sum(1 for p in dest.rglob("*") if p.is_file())
    return InstallResult(app_dir=str(app_dir), installed_path=str(dest), file_count=file_count)


def _install_app_dir(app_dir: Path, dest: Path, copytree: Callable) -> None:
    """Replace ``dest`` with a clean copy of ``app_dir``, preserving UC artifacts.

    The destination is wiped first so a re-import cannot leave stale files, but
    UC-built artifacts that are not on the SD (the display shim) are carried
    across the wipe so translate mode keeps working after a re-import.
    """
    preserved = {}
    for name in _PRESERVE_ON_REINSTALL:
        existing = dest / name
        if existing.is_file():
            preserved[name] = existing.read_bytes()

    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    copytree(app_dir, dest, ignore=ignore_cruft)

    for name, data in preserved.items():
        (dest / name).write_bytes(data)
