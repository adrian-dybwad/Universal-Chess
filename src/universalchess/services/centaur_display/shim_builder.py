"""Build the centaur display shim (``spishim.so``) on-device from shipped source.

In "translate" mode the original centaur binary is launched with the shim
``LD_PRELOAD``ed so it virtualizes centaur's panel and forwards its SPI stream
to UC's gateway. The shim is a small C shared object that MUST match centaur's
32-bit armhf ABI (the linker refuses to preload a differently-typed object), so
it is built on-device rather than shipped as a binary. On a 32-bit ARM host the
native ``gcc`` produces armhf directly; on a 64-bit ``aarch64`` host it must be
cross-compiled with the armhf toolchain (see ``_resolve_compiler``), which the
package pulls in via ``Recommends: gcc-arm-linux-gnueabihf``.

Nothing else builds it: the SD import only preserves an existing ``spishim.so``,
and the package ships the *source* (next to this module) but not a binary. So
this module is the single place that compiles it, and ``ensure_display_shim`` is
called before a translate-mode launch (and from the deb postinst) to build it on
demand. The compile command lives here only -- the dev script
``tools/centaur-display-shim/build.sh`` mirrors these exact flags.

A source-hash stamp beside the ``.so`` records which source it was built from, so
a later change to ``spishim.c`` (a shim fix shipped with a code update) triggers
a rebuild on already-deployed boards instead of silently keeping the old binary.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess  # nosec B404 - runs the configured C compiler on shipped source only
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from universalchess.paths import CENTAUR_DISPLAY_SHIM

# Canonical shim source, shipped inside the package so it travels with the code
# (both the .deb and the deploy-to-pi rsync include src/universalchess).
SHIM_SOURCE = Path(__file__).resolve().parent / "shim" / "spishim.c"

# armhf cross compiler, needed on 64-bit hosts (see _resolve_compiler).
ARMHF_CROSS_COMPILER = "arm-linux-gnueabihf-gcc"

# Native compiler, correct only on a 32-bit ARM host.
NATIVE_COMPILER = "gcc"

# Back-compat alias (was the hard-coded default); resolution now happens in
# _resolve_compiler so a 64-bit host does not silently try to build armhf with
# an aarch64 gcc.
DEFAULT_COMPILER = NATIVE_COMPILER


def _resolve_compiler(compiler: Optional[str]) -> str:
    """Pick a compiler that emits centaur's 32-bit armhf ABI.

    centaur is a 32-bit ARM (armhf) binary and the shim is ``LD_PRELOAD``ed into
    it, so the shim MUST also be armhf. On a 32-bit ARM host the native ``gcc``
    already targets armhf. On a 64-bit ``aarch64`` host the native ``gcc`` targets
    aarch64 and physically cannot emit armhf (the compile fails on armhf-only
    ``mcontext_t.arm_*`` fields and rejects ``_TIME_BITS=32`` under 64-bit
    glibc), so an armhf cross compiler is required.

    An explicit ``compiler`` argument or the ``UC_CENTAUR_SHIM_CC`` environment
    variable always wins (for boards with a differently named toolchain).
    """
    if compiler:
        return compiler
    override = os.environ.get("UC_CENTAUR_SHIM_CC")
    if override:
        return override
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return ARMHF_CROSS_COMPILER
    return NATIVE_COMPILER


class ShimBuildError(Exception):
    """Raised when the display shim cannot be compiled.

    Surfaced to the translate-mode launch so it fails loudly rather than running
    centaur un-shimmed -- an un-shimmed launch silently drives the real panel
    (the confusing "translate mode does nothing" failure this guards against).
    """


def _stamp_path(shim_path) -> Path:
    """Sidecar path holding the sha256 of the source the ``.so`` was built from."""
    return Path(str(shim_path) + ".srchash")


def _read_stamp(stamp_path) -> str:
    """Return the recorded source hash, or "" if the stamp is absent/unreadable.

    An absent or unreadable stamp (e.g. a ``.so`` carried across a re-import with
    no stamp) reads as "" so it never matches the current source hash, forcing a
    rebuild -- the safe default when we cannot prove what the ``.so`` was built
    from.
    """
    try:
        return Path(stamp_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _compile_command(compiler: str, src, out) -> List[str]:
    """The exact ``gcc`` command that builds the shim.

    Non-LFS (32-bit ``off_t``/``time_t``) is load-bearing: under armhf's default
    ``_FILE_OFFSET_BITS=64`` / ``_TIME_BITS=64`` a C function named ``mmap`` is
    exported as the symbol ``mmap64``, but centaur's bundled ``RPi/_GPIO.so``
    imports the plain ``mmap@GLIBC_2.4``, so the shim must export ``mmap`` --
    which requires 32-bit ``off_t``. centaur is 32-bit ARM, so this matches its
    ABI. Keep in sync with ``tools/centaur-display-shim/build.sh``.
    """
    return [
        compiler, "-shared", "-fPIC", "-O2", "-Wall", "-Wextra",
        "-U_FILE_OFFSET_BITS", "-D_FILE_OFFSET_BITS=32",
        "-U_TIME_BITS", "-D_TIME_BITS=32",
        str(src), "-o", str(out), "-ldl", "-lpthread",
    ]


def build_shim(
    out_path,
    *,
    source_path=SHIM_SOURCE,
    compiler: Optional[str] = None,
    runner: Callable = subprocess.run,
) -> None:
    """Compile the shim source to ``out_path``; raise ShimBuildError on failure.

    Compiles to a temp file in the destination directory and atomically moves it
    into place, so an interrupted or failed build never leaves a partial ``.so``
    that would then be ``LD_PRELOAD``ed. ``runner`` is injected for tests. The
    compiler defaults to an ABI-appropriate choice (see :func:`_resolve_compiler`)
    -- an armhf cross compiler on a 64-bit host.
    """
    compiler = _resolve_compiler(compiler)
    source_path = Path(source_path)
    out_path = Path(out_path)
    if not source_path.is_file():
        raise ShimBuildError(f"shim source not found: {source_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".spishim-", suffix=".so", dir=str(out_path.parent))
    os.close(fd)
    tmp_path: Optional[Path] = Path(tmp_name)
    try:
        cmd = _compile_command(compiler, source_path, tmp_path)
        try:
            result = runner(cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError as e:
            # Compiler absent: a clear, actionable message beats a raw OSError.
            # On a 64-bit host the armhf cross compiler is the usual miss.
            hint = ""
            if compiler == ARMHF_CROSS_COMPILER:
                hint = " (install it with: sudo apt install gcc-arm-linux-gnueabihf)"
            raise ShimBuildError(f"compiler '{compiler}' not found{hint}") from e
        except OSError as e:
            raise ShimBuildError(f"could not run compiler '{compiler}': {e}") from e

        if getattr(result, "returncode", 0) != 0:
            stderr = (getattr(result, "stderr", "") or "").strip()
            raise ShimBuildError(f"shim compile failed: {stderr or 'unknown compiler error'}")
        if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
            raise ShimBuildError("shim compile reported success but produced no output")

        os.replace(tmp_path, out_path)
        tmp_path = None
    finally:
        # Remove the temp artifact unless it was moved into place above.
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def _source_hash(source_path) -> str:
    """Return the sha256 hex of the shim source, or raise ShimBuildError."""
    try:
        return hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
    except OSError as e:
        raise ShimBuildError(f"shim source not found: {source_path}") from e


def ensure_display_shim(
    shim_path=CENTAUR_DISPLAY_SHIM,
    *,
    source_path=SHIM_SOURCE,
    compiler: Optional[str] = None,
    runner: Callable = subprocess.run,
) -> bool:
    """Ensure ``spishim.so`` exists and is built from the current shipped source.

    Builds the shim when it is missing or when the stamped source hash does not
    match the shipped source (so a shim fix shipped with a code update rebuilds
    on already-deployed boards). On a successful build the source hash is stamped
    beside the ``.so``.

    Returns True if it (re)built, False if the existing ``.so`` was already
    current. Raises ShimBuildError if a needed build fails -- the translate-mode
    launch relies on this to abort rather than run centaur un-shimmed.
    """
    shim_path = Path(shim_path)
    src_hash = _source_hash(source_path)
    stamp = _stamp_path(shim_path)

    if shim_path.is_file() and _read_stamp(stamp) == src_hash:
        return False

    build_shim(shim_path, source_path=source_path, compiler=compiler, runner=runner)
    stamp.write_text(src_hash, encoding="utf-8")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry: ``python -m universalchess.services.centaur_display.shim_builder``.

    Builds the shim into the default location for the *current user's* home (the
    deb postinst runs this as the service user so ``~`` resolves to that user's
    home, matching where the launcher reads it). Best-effort: a build failure
    here is reported and returns non-zero, but the translate-mode launch path
    will retry/heal on demand.
    """
    import sys

    try:
        rebuilt = ensure_display_shim()
    except ShimBuildError as e:
        print(f"centaur display shim build failed: {e}", file=sys.stderr)
        return 1
    where = CENTAUR_DISPLAY_SHIM
    print(f"centaur display shim {'built' if rebuilt else 'already current'} at {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
