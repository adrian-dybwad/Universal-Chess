"""Tests that a source build hard-requires the build-memory swap.

The per-engine compile-memory guards (-j1/-j2) were removed in favor of a
temporary swap acquired around the build (services.build_memory). A build now
runs at full parallelism, so it MUST NOT proceed if that swap could not be
reserved -- doing so would OOM mid-compile. These tests pin that install_engine
aborts loudly (visible error, build never started) when memory is unavailable,
and proceeds normally when it is.
"""

import contextlib

from universalchess.managers import engine_manager as em
from universalchess.managers.engine_manager import EngineManager

# A source-built engine whose arch gate is open everywhere (supported_archs=None),
# so the test reaches the source-build branch regardless of the host CPU.
_SOURCE_ENGINE = "zahak"


def _patch_build_memory(monkeypatch, *, acquired):
    """Replace build_memory with a context manager yielding the given outcome."""
    @contextlib.contextmanager
    def fake_build_memory(*args, **kwargs):
        yield acquired

    monkeypatch.setattr(em, "build_memory", fake_build_memory)


def _make_manager(monkeypatch, *, build_result=True):
    """An EngineManager whose prebuilt path is forced off and whose source build
    is a tracked stub, so install_engine deterministically exercises the
    source-build branch without touching the network/disk."""
    mgr = EngineManager()
    monkeypatch.setattr(mgr, "_try_install_prebuilt", lambda *a, **k: False)
    monkeypatch.setattr(mgr._record_store, "record_install", lambda *a, **k: None)
    calls = {"build": 0}

    def fake_install_from_source(*args, **kwargs):
        calls["build"] += 1
        return build_result

    monkeypatch.setattr(mgr, "_install_from_source", fake_install_from_source)
    return mgr, calls


def test_source_install_aborts_when_memory_not_acquired(monkeypatch):
    # Regression guard: if the swap cannot be reserved (missing helper/sudo grant/
    # zram failure), the build must NOT start. Without this, the unguarded
    # full-parallelism compile would OOM -- the exact crash the swap exists to
    # prevent. Manifests as _install_from_source being called (build started)
    # and/or a successful return despite no memory.
    _patch_build_memory(monkeypatch, acquired=False)
    mgr, calls = _make_manager(monkeypatch)

    result = mgr.install_engine(_SOURCE_ENGINE)

    assert result is False
    assert calls["build"] == 0, "build must not start when memory is unavailable"
    assert "memory" in mgr.get_install_error().lower()


def test_source_install_proceeds_when_memory_acquired(monkeypatch):
    # The happy path: with swap reserved, the source build runs and its result is
    # returned. Guards that the fail-loud gate does not block normal installs.
    _patch_build_memory(monkeypatch, acquired=True)
    mgr, calls = _make_manager(monkeypatch, build_result=True)

    result = mgr.install_engine(_SOURCE_ENGINE)

    assert result is True
    assert calls["build"] == 1
