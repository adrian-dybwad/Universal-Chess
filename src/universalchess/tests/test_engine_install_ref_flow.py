"""Integration tests for ref-aware install dispatch and ref recording.

These enter through ``EngineManager.install_engine`` (the highest deterministic
level) with the prebuilt/source workers stubbed, so the test verifies the
decision logic and the durable recording without compiling an engine or hitting
the network. Arasan is used because it is the real pinned engine the picker was
built for (pin ``v25.4``, has a prebuilt).
"""

import pytest

from universalchess.managers.engine_manager import EngineManager
from universalchess.services.engine_install_record import (
    DEFAULT_REF,
    EngineInstallRecordStore,
)
from universalchess.services.github_tag_cache import GitHubTagCacheStore


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """An EngineManager on a temp engines dir + temp record store, forced arm64.

    Arch is pinned to arm64 so Arasan (arm64-only) passes the support gate on any
    host running the suite. The prebuilt/source workers are stubbed per-test.
    """
    store = EngineInstallRecordStore(path=tmp_path / "record.json")
    mgr = EngineManager(
        engines_dir=str(tmp_path / "engines"),
        record_store=store,
        tag_cache=GitHubTagCacheStore(path=tmp_path / "cache.json"),
    )
    monkeypatch.setattr(mgr, "_get_arch", lambda: "arm64")
    return mgr


def _stub_workers(manager, monkeypatch):
    """Stub both install workers to record how they were called and succeed.

    Returns a dict capturing whether the prebuilt path ran and the ref_label the
    source path received, so a test can assert which branch the dispatch chose.
    """
    calls = {"prebuilt": False, "source_ref_label": None}

    def fake_prebuilt(engine, update_progress):
        calls["prebuilt"] = True
        return True

    def fake_source(engine, update_progress, ref_label=None):
        calls["source_ref_label"] = ref_label
        return True

    monkeypatch.setattr(manager, "_try_install_prebuilt", fake_prebuilt)
    monkeypatch.setattr(manager, "_install_from_source", fake_source)
    return calls


def test_default_request_uses_prebuilt_and_records_pin(manager, monkeypatch):
    """No ref -> canonical: the prebuilt is used and the pin is recorded.

    Why this test exists: the common case (user clicks Install, no tag chosen)
    must keep using the fast prebuilt and record the engine as installed from its
    pinned ref so the UI shows the right version.

    How it manifests: a regression that forced source builds would flip
    calls["prebuilt"] to False; a recording regression would leave installed_ref
    as None.
    """
    calls = _stub_workers(manager, monkeypatch)

    assert manager.install_engine("arasan") is True
    assert calls["prebuilt"] is True
    assert calls["source_ref_label"] is None
    assert manager._record_store.installed_ref("arasan") == "v25.4"


def test_noncanonical_ref_forces_source_build(manager, monkeypatch):
    """A non-pin tag skips the prebuilt and builds that exact ref from source.

    Why this test exists: there is no prebuilt for an arbitrary tag, so using the
    pinned archive would install the wrong version while claiming the requested
    one. The requested ref must reach the source builder and be recorded.

    How it manifests: if prebuilt gating broke, calls["prebuilt"] would be True
    and the wrong binary would install; if the ref were not threaded through,
    source_ref_label would not equal the requested tag.
    """
    calls = _stub_workers(manager, monkeypatch)

    assert manager.install_engine("arasan", ref="v25.5") is True
    assert calls["prebuilt"] is False
    assert calls["source_ref_label"] == "v25.5"
    assert manager._record_store.installed_ref("arasan") == "v25.5"


def test_default_branch_sentinel_forces_source_for_pinned(manager, monkeypatch):
    """Selecting the default branch on a pinned engine builds it from source.

    Why this test exists: the picker offers "default branch" (DEFAULT_REF) so the
    latest code can be tried. For a pinned engine that is non-canonical, so it must
    build from source with the sentinel threaded through (which the source builder
    maps to an unpinned clone), and record the default-branch label.

    How it manifests: a regression treating DEFAULT_REF as canonical would wrongly
    use the v25.4 prebuilt; a recording bug would store the pin instead of the
    default sentinel.
    """
    calls = _stub_workers(manager, monkeypatch)

    assert manager.install_engine("arasan", ref=DEFAULT_REF) is True
    assert calls["prebuilt"] is False
    assert calls["source_ref_label"] == DEFAULT_REF
    assert manager._record_store.installed_ref("arasan") == DEFAULT_REF


def test_failed_install_is_not_recorded(manager, monkeypatch):
    """A failed build records nothing, so the picker never marks it working.

    Why this test exists: known-working status must reflect real successes only.
    Recording on failure would falsely mark a broken ref as known-working and show
    it as installed.

    How it manifests: if recording moved before the success check, installed_ref
    would be set despite the source builder returning False.
    """
    monkeypatch.setattr(manager, "_try_install_prebuilt", lambda e, u: False)
    monkeypatch.setattr(manager, "_install_from_source", lambda e, u, ref_label=None: False)

    assert manager.install_engine("arasan", ref="v99.9") is False
    assert manager._record_store.installed_ref("arasan") is None
    assert manager._record_store.working_refs("arasan") == []


def test_uninstall_clears_installed_ref_keeps_history(manager, monkeypatch):
    """Uninstall clears the installed ref but the working history persists.

    Why this test exists: end-to-end check that the manager wires uninstall to the
    record store, satisfying "know what ever worked in the past" even after
    removal.

    How it manifests: if uninstall did not call the store, installed_ref would
    remain set; if it cleared history, working_refs would be emptied.
    """
    _stub_workers(manager, monkeypatch)
    manager.install_engine("arasan", ref="v25.4")
    # Stub the filesystem-touching parts of uninstall so it runs without a real
    # binary present; the record clearing is what this test asserts.
    monkeypatch.setattr(manager, "_get_arch", lambda: "arm64")

    assert manager.uninstall_engine("arasan") is True
    assert manager._record_store.installed_ref("arasan") is None
    assert manager._record_store.working_refs("arasan") == ["v25.4"]
