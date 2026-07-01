"""Tests for the on-device centaur display-shim builder.

Background / why these tests exist
----------------------------------
Translate mode ``LD_PRELOAD``s ``CENTAUR_HOME/spishim.so`` into centaur. The shim
must be compiled natively on the Pi (32-bit ARM ABI) and is never shipped as a
binary, so it is built on demand by ``ensure_display_shim`` (before launch and
from the deb postinst). The regression these guard: the shim is missing and
nothing builds it, so the launch silently runs centaur un-shimmed (real panel,
no interception). The builder must therefore build when missing, rebuild when
the shipped source changes, and FAIL LOUDLY (never produce a partial/looks-ok
``.so``) when the compile fails -- the launch depends on that to abort.

The C compiler is the external boundary, so it is mocked via an injected
``runner``; a fake "successful" compiler writes a stub file to the ``-o`` target
so the builder's output check sees a real artifact.
"""

import subprocess

import pytest

from universalchess.services.centaur_display.shim_builder import (
    ARMHF_CROSS_COMPILER,
    NATIVE_COMPILER,
    ShimBuildError,
    _compile_command,
    _resolve_compiler,
    _stamp_path,
    build_shim,
    ensure_display_shim,
)


@pytest.fixture
def shim_source(tmp_path):
    """A stand-in shim source file (content is irrelevant to the builder)."""
    src = tmp_path / "spishim.c"
    src.write_text("int main(void){return 0;}\n", encoding="utf-8")
    return src


def _fake_compiler(record, *, returncode=0, stderr="", write_output=True):
    """Return a subprocess.run stand-in that records argv and fakes a compile.

    On a "successful" (returncode 0) run it writes a stub to the ``-o`` target so
    the builder's non-empty-output check passes, mirroring what gcc would do.
    """
    def _run(cmd, **kwargs):
        record.append(list(cmd))
        if returncode == 0 and write_output:
            out = cmd[cmd.index("-o") + 1]
            with open(out, "wb") as fh:
                fh.write(b"\x7fELF-stub")
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)
    return _run


# --- _resolve_compiler --------------------------------------------------------


def test_resolve_compiler_uses_armhf_cross_on_aarch64(monkeypatch):
    """On a 64-bit host the builder must select the armhf cross compiler.

    Why this test exists: the shim is LD_PRELOADed into a 32-bit armhf centaur,
    so it must be armhf; a native aarch64 gcc cannot emit armhf and the compile
    fails (armhf-only mcontext_t.arm_* fields, _TIME_BITS=32 rejected under
    64-bit glibc). This is the exact "building shim failed" regression on a CM5.
    Manifests as gcc being chosen on aarch64, reintroducing the build failure.
    """
    monkeypatch.delenv("UC_CENTAUR_SHIM_CC", raising=False)
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    assert _resolve_compiler(None) == ARMHF_CROSS_COMPILER


def test_resolve_compiler_uses_native_gcc_on_32bit_arm(monkeypatch):
    """On a 32-bit ARM host the native gcc already targets armhf.

    Why this test exists: pulling in a cross compiler where the native one is
    correct would be needless (and the cross package may not even exist for an
    armhf host). Manifests as the cross compiler being demanded on armv7l/armv6l.
    """
    monkeypatch.delenv("UC_CENTAUR_SHIM_CC", raising=False)
    monkeypatch.setattr("platform.machine", lambda: "armv7l")
    assert _resolve_compiler(None) == NATIVE_COMPILER


def test_resolve_compiler_honors_explicit_and_env_override(monkeypatch):
    """An explicit argument or UC_CENTAUR_SHIM_CC overrides arch detection.

    Why this test exists: boards with a differently named toolchain must be able
    to override without a code change; the explicit argument must take priority
    over the env var, which takes priority over arch detection. Manifests as an
    override being ignored (arch default used instead).
    """
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    monkeypatch.setenv("UC_CENTAUR_SHIM_CC", "my-gcc")
    assert _resolve_compiler(None) == "my-gcc"  # env beats arch default
    assert _resolve_compiler("explicit-gcc") == "explicit-gcc"  # arg beats env


def test_build_shim_invokes_cross_compiler_on_aarch64(monkeypatch, tmp_path, shim_source):
    """build_shim compiles with the armhf cross compiler when on aarch64.

    Why this test exists: guards the wiring from _resolve_compiler through to the
    actual compile invocation, not just the resolver in isolation. Manifests as
    the compile being invoked with plain gcc on a 64-bit host (build fails on
    hardware).
    """
    monkeypatch.delenv("UC_CENTAUR_SHIM_CC", raising=False)
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    calls = []

    build_shim(tmp_path / "spishim.so", source_path=shim_source, runner=_fake_compiler(calls))

    assert calls[0][0] == ARMHF_CROSS_COMPILER


def test_build_shim_missing_cross_compiler_hints_package(monkeypatch, tmp_path, shim_source):
    """A missing armhf cross compiler yields an install hint, not a bare error.

    Why this test exists: on a fresh 64-bit board without the toolchain the user
    must get an actionable message naming the package to install. Manifests as a
    generic "not found" with no remediation, leaving translate mode unfixable by
    a non-expert.
    """
    monkeypatch.delenv("UC_CENTAUR_SHIM_CC", raising=False)
    monkeypatch.setattr("platform.machine", lambda: "aarch64")

    def _missing(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    with pytest.raises(ShimBuildError) as exc:
        build_shim(tmp_path / "spishim.so", source_path=shim_source, runner=_missing)

    assert "gcc-arm-linux-gnueabihf" in str(exc.value)


# --- _compile_command ---------------------------------------------------------


def test_compile_command_uses_non_lfs_abi_flags(tmp_path):
    """The compile command must pin 32-bit off_t/time_t so ``mmap`` is exported.

    Why: under armhf's default 64-bit file/time ABI a C ``mmap`` is exported as
    ``mmap64``, but centaur's _GPIO.so imports plain ``mmap@GLIBC_2.4``; building
    LFS would silently produce a shim whose mmap hook never binds, so GPIO/DC
    tracking is dead. This pins the exact flags that keep ``mmap`` exported.

    How a regression manifests: a flag is dropped/changed here and the shim
    builds but does not intercept on hardware (hard to diagnose post-build).
    """
    cmd = _compile_command("gcc", tmp_path / "spishim.c", tmp_path / "spishim.so")

    assert cmd[0] == "gcc"
    assert "-shared" in cmd and "-fPIC" in cmd
    assert "-D_FILE_OFFSET_BITS=32" in cmd
    assert "-U_FILE_OFFSET_BITS" in cmd
    assert "-D_TIME_BITS=32" in cmd
    assert "-U_TIME_BITS" in cmd
    assert cmd[-3:] == ["-o", str(tmp_path / "spishim.so"), "-ldl"] or (
        "-ldl" in cmd and "-lpthread" in cmd
    )
    # Source and output are both present and ordered (source before -o target).
    assert str(tmp_path / "spishim.c") in cmd
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "spishim.so")


# --- build_shim ---------------------------------------------------------------


def test_build_shim_compiles_source_to_output(tmp_path, shim_source):
    """A successful compile leaves the .so at out_path and invokes gcc once.

    Why: this is the core build step the launch depends on. Asserts the compiler
    is called with the shipped source and the requested output path, and the
    artifact lands where the launcher LD_PRELOADs it.

    How a regression manifests: the .so is missing after a "successful" build
    (wrong -o target, temp not moved into place) -> launch finds no shim.
    """
    out = tmp_path / "install" / "spishim.so"
    calls = []

    build_shim(out, source_path=shim_source, runner=_fake_compiler(calls))

    assert out.is_file()
    assert out.read_bytes() == b"\x7fELF-stub"
    assert len(calls) == 1
    assert str(shim_source) in calls[0]
    # The compiler writes to a temp in the dest dir, not directly to out_path,
    # so a crash mid-compile cannot leave a partial spishim.so in place.
    o_target = calls[0][calls[0].index("-o") + 1]
    assert o_target != str(out)
    assert str(out.parent) in o_target


def test_build_shim_raises_and_leaves_no_output_on_compiler_error(tmp_path, shim_source):
    """A non-zero compile must raise ShimBuildError and leave no .so behind.

    Why: the launch treats a present .so as usable. If a failed compile left a
    partial/zero-byte file, it would be LD_PRELOADed and silently do nothing.
    Asserts failure raises (so the launch aborts) and no stale artifact remains.

    How a regression manifests: build_shim swallows the error or the temp file
    is promoted to out_path on failure -> out_path exists after a failed build.
    """
    out = tmp_path / "spishim.so"
    calls = []

    with pytest.raises(ShimBuildError) as exc:
        build_shim(
            out,
            source_path=shim_source,
            runner=_fake_compiler(calls, returncode=1, stderr="undefined reference"),
        )

    assert "undefined reference" in str(exc.value)
    assert not out.exists()
    # No leftover temp files in the destination dir either.
    assert list(out.parent.glob(".spishim-*")) == []


def test_build_shim_raises_when_compiler_missing(tmp_path, shim_source):
    """A missing gcc must raise a clear ShimBuildError, not a raw OSError.

    Why: boards without a toolchain should get an actionable message, and the
    launch must still abort (loud failure) rather than proceed un-shimmed.

    How a regression manifests: the FileNotFoundError from runner propagates raw
    (or is swallowed), so the caller cannot distinguish/abort cleanly.
    """
    def _missing(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "gcc")

    with pytest.raises(ShimBuildError) as exc:
        build_shim(tmp_path / "spishim.so", source_path=shim_source, runner=_missing)

    assert "not found" in str(exc.value).lower()


def test_build_shim_raises_when_source_missing(tmp_path):
    """A missing source file must raise before invoking the compiler at all.

    Why: a misdeployed package (source not shipped) must fail clearly instead of
    handing gcc a nonexistent path and surfacing a cryptic compiler error.

    How a regression manifests: the runner is called with a bad source and the
    error surfaces as a confusing compile failure rather than "source not found".
    """
    calls = []
    with pytest.raises(ShimBuildError) as exc:
        build_shim(tmp_path / "spishim.so", source_path=tmp_path / "nope.c",
                   runner=_fake_compiler(calls))

    assert "source not found" in str(exc.value).lower()
    assert calls == []


# --- ensure_display_shim ------------------------------------------------------


def test_ensure_builds_when_missing_and_stamps_source_hash(tmp_path, shim_source):
    """When no .so exists, ensure builds it and writes the source-hash stamp.

    Why: the fresh-install / first-launch case. The stamp must record the source
    it built from so later runs can detect staleness.

    How a regression manifests: ensure returns without building (launch finds no
    shim), or builds but writes no stamp (every launch rebuilds needlessly).
    """
    out = tmp_path / "spishim.so"
    calls = []

    built = ensure_display_shim(out, source_path=shim_source, runner=_fake_compiler(calls))

    assert built is True
    assert out.is_file()
    assert len(calls) == 1
    assert _stamp_path(out).is_file()
    assert _stamp_path(out).read_text(encoding="utf-8").strip() != ""


def test_ensure_is_noop_when_so_present_and_stamp_matches(tmp_path, shim_source):
    """A present .so whose stamp matches the current source is left untouched.

    Why: re-launching must not recompile every time (e-paper handoff is already
    slow). Asserts no compiler invocation and the existing artifact is kept.

    How a regression manifests: ensure rebuilds unconditionally (compiler called)
    despite an up-to-date shim, adding latency to every launch.
    """
    out = tmp_path / "spishim.so"
    # First build establishes the .so + stamp.
    ensure_display_shim(out, source_path=shim_source, runner=_fake_compiler([]))
    out.write_bytes(b"existing-binary")

    calls = []
    built = ensure_display_shim(out, source_path=shim_source, runner=_fake_compiler(calls))

    assert built is False
    assert calls == []
    assert out.read_bytes() == b"existing-binary"


def test_ensure_rebuilds_when_source_hash_changes(tmp_path, shim_source):
    """A changed shim source (new stamp mismatch) forces a rebuild.

    Why: shipping a shim fix in a code update must rebuild on deployed boards;
    the .so alone cannot signal that the source changed -- the stamp does.

    How a regression manifests: ensure keeps the old .so after the source
    changed (stale shim, the very bug a shim fix was meant to address).
    """
    out = tmp_path / "spishim.so"
    ensure_display_shim(out, source_path=shim_source, runner=_fake_compiler([]))

    shim_source.write_text("int main(void){return 1;}\n", encoding="utf-8")  # source changed
    calls = []
    built = ensure_display_shim(out, source_path=shim_source, runner=_fake_compiler(calls))

    assert built is True
    assert len(calls) == 1
    # Stamp now reflects the new source so the next run is a no-op.
    again = ensure_display_shim(out, source_path=shim_source, runner=_fake_compiler([]))
    assert again is False


def test_ensure_rebuilds_when_stamp_missing(tmp_path, shim_source):
    """A present .so with no stamp (e.g. carried across a re-import) rebuilds once.

    Why: a re-import preserves spishim.so; if the stamp is absent the builder
    cannot prove the .so matches the shipped source, so it rebuilds to be safe.

    How a regression manifests: ensure trusts an unstamped .so and skips the
    build, potentially keeping a shim built from a different source.
    """
    out = tmp_path / "spishim.so"
    out.write_bytes(b"orphan-binary-no-stamp")
    calls = []

    built = ensure_display_shim(out, source_path=shim_source, runner=_fake_compiler(calls))

    assert built is True
    assert len(calls) == 1
    assert _stamp_path(out).is_file()


def test_ensure_propagates_build_failure(tmp_path, shim_source):
    """A failed rebuild must raise (and not stamp) so the launch aborts loudly.

    Why: the whole point of failing loudly is to avoid an un-shimmed launch. If
    a needed build fails, ensure must raise and must not leave a stamp implying
    success.

    How a regression manifests: ensure swallows the failure (launch proceeds
    un-shimmed) or writes a stamp despite no build (next run wrongly no-ops).
    """
    out = tmp_path / "spishim.so"
    calls = []

    with pytest.raises(ShimBuildError):
        ensure_display_shim(
            out, source_path=shim_source,
            runner=_fake_compiler(calls, returncode=1, stderr="boom"),
        )

    assert not _stamp_path(out).exists()
    assert not out.exists()
