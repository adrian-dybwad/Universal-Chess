"""Tests for the durable engine install-ref record store.

The store answers "what ref is installed now?" and "what refs ever built here?"
for the tag picker. These tests pin the two non-obvious behaviors -- history is
retained across uninstall, and the file survives a fresh process -- because a
regression in either silently degrades the picker (a tag the user verified would
stop showing as known-working, or the installed tag would be forgotten on
restart).
"""

import json

import pytest

from universalchess.services.engine_install_record import (
    DEFAULT_REF,
    EngineInstallRecordStore,
)


@pytest.fixture
def store(tmp_path):
    """A record store backed by a throwaway file in a temp dir."""
    return EngineInstallRecordStore(path=tmp_path / "record.json")


def test_records_installed_ref_and_marks_working(store):
    """A successful install sets the current ref and adds it to history.

    Why this test exists: the picker reads both the current ref (to show what is
    installed) and the working history (to mark known-working). A regression that
    wrote only one would break one half of the UI.

    How it manifests: if record_install stopped setting installed_ref, the
    installed-ref assertion drops to None; if it stopped appending to history,
    working_refs would be empty.
    """
    store.record_install("arasan", "v25.4")

    assert store.installed_ref("arasan") == "v25.4"
    assert store.working_refs("arasan") == ["v25.4"]
    record = store.get("arasan")
    assert record is not None
    assert record.installed_at is not None


def test_working_history_accumulates_without_duplicates(store):
    """Installing several refs accumulates each once, in first-seen order.

    Why this test exists: history must be a de-duplicated, ordered set so the
    picker can list every ref that ever worked without repeats when a ref is
    reinstalled.

    How it manifests: dropping the dedup check would append "v25.4" twice after
    the reinstall; losing order would shuffle the list.
    """
    store.record_install("arasan", "v25.4")
    store.record_install("arasan", "v25.5")
    store.record_install("arasan", "v25.4")  # reinstall of an already-working ref

    assert store.working_refs("arasan") == ["v25.4", "v25.5"]
    assert store.installed_ref("arasan") == "v25.4"


def test_uninstall_clears_current_ref_but_keeps_history(store):
    """Uninstall clears the installed ref yet retains the working history.

    Why this test exists: the requirement is to know what "ever worked in the
    past" -- so uninstalling must not erase the history, only the current state.

    How it manifests: if record_uninstall also cleared working_refs, a tag the
    user verified would stop showing as known-working after an uninstall.
    """
    store.record_install("arasan", "v25.4")
    store.record_uninstall("arasan")

    assert store.installed_ref("arasan") is None
    assert store.get("arasan").installed_at is None
    assert store.working_refs("arasan") == ["v25.4"]


def test_default_ref_is_recordable(store):
    """The default-branch sentinel is a normal recordable ref value.

    Why this test exists: unpinned engines install from the default branch; that
    must be representable distinctly from a tag and from "no record" so the UI can
    label it. The sentinel is just a ref string to the store.

    How it manifests: if the store rejected the sentinel, default-branch installs
    could not be recorded at all.
    """
    store.record_install("weiss", DEFAULT_REF)

    assert store.installed_ref("weiss") == DEFAULT_REF
    assert DEFAULT_REF in store.working_refs("weiss")


def test_empty_ref_is_rejected(store):
    """An empty ref label is a programming error and must raise.

    Why this test exists: callers resolve None to a concrete label (a tag or the
    default sentinel) before recording; recording an empty/None ref would corrupt
    the "installed" state into something the UI cannot interpret.

    How it manifests: removing the guard would silently store an empty installed
    ref instead of failing fast at the call site.
    """
    with pytest.raises(ValueError):
        store.record_install("arasan", "")


def test_record_persists_across_new_store_instance(tmp_path):
    """A fresh store instance reads back what a prior instance wrote.

    Why this test exists: the record must survive a process restart (the board
    reboots); a new EngineInstallRecordStore models the next process. This guards
    the on-disk round-trip, including history.

    How it manifests: a broken save/load (e.g. non-atomic write lost, or schema
    mismatch on read) would return None for the installed ref from the new
    instance even though the first instance recorded it.
    """
    path = tmp_path / "record.json"
    first = EngineInstallRecordStore(path=path)
    first.record_install("arasan", "v25.4")
    first.record_install("arasan", "v25.5")

    second = EngineInstallRecordStore(path=path)
    assert second.installed_ref("arasan") == "v25.5"
    assert second.working_refs("arasan") == ["v25.4", "v25.5"]


def test_corrupt_file_is_treated_as_no_records(tmp_path):
    """A corrupt record file degrades to "no records" instead of crashing.

    Why this test exists: a partially-written or hand-edited file must not crash
    the status endpoint or block an install; the next install rewrites it.

    How it manifests: without the guard, json.load raises and the first read of
    the store propagates the exception to the caller.
    """
    path = tmp_path / "record.json"
    path.write_text("{ not valid json", encoding="utf-8")

    store = EngineInstallRecordStore(path=path)
    assert store.get("arasan") is None
    # And a subsequent install still works, overwriting the corrupt file.
    store.record_install("arasan", "v25.4")
    assert json.loads(path.read_text())["arasan"]["installed_ref"] == "v25.4"
