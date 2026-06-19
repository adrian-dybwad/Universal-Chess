"""Tests for the patched-vs-stock BlueZ stack detector.

The board may run a locally rebuilt ``bluetoothd`` carrying a pre-release fix on
OS/kernel combinations where the released BlueZ is broken (see
``managers/bluez_patch_status``). Because a substituted binary stops receiving
distribution security updates, both the web and the device screen must warn the
operator -- and must do so only when the board is *actually* patched. These tests
guard:

1. ``patched`` is derived strictly from ``active`` (the two cannot disagree).
2. A missing/corrupt/forward-incompatible marker degrades to ``unknown`` (no
   false "stock" that would silently suppress the warning, and no exception
   leaking into the status snapshot).
3. ``warning_label`` fires only for the patched stack and carries the base
   version when known.
"""

import json

import pytest

from universalchess.managers.bluez_patch_status import (
    HEAL_APPLYING,
    HEAL_BUILDING,
    HEAL_PROBING_PATCH,
    HEAL_PROBING_STOCK,
    STACK_PATCHED,
    STACK_STOCK,
    STACK_UNKNOWN,
    derive_progress,
    derive_status,
    heal_label,
    idle_progress,
    make_progress,
    make_status,
    read_progress,
    read_status,
    stock_status,
    unknown_status,
    warning_label,
)


def test_patched_flag_follows_active_patched():
    # The whole point of the module: a patched marker must report patched=True
    # AND carry its provenance fields verbatim. Regression: dropping a field or
    # not setting patched would make the UI under-report the deviation.
    status = make_status(
        active=STACK_PATCHED,
        base_version="5.82-1.1+rpt1",
        fix="bluez 2a6968b: Add Ext Adv Data length",
        reason="kernel 6.18 ext-adv-data validation",
        applied_at="2026-06-19T10:00:00Z",
    )
    assert status["active"] == STACK_PATCHED
    assert status["patched"] is True
    assert status["base_version"] == "5.82-1.1+rpt1"
    assert status["fix"] == "bluez 2a6968b: Add Ext Adv Data length"
    assert status["reason"] == "kernel 6.18 ext-adv-data validation"
    assert status["applied_at"] == "2026-06-19T10:00:00Z"


@pytest.mark.parametrize(
    "active, expect_patched",
    [
        (STACK_STOCK, False),
        (STACK_UNKNOWN, False),
        (STACK_PATCHED, True),
    ],
)
def test_patched_is_true_only_for_patched(active, expect_patched):
    # Guards the closed-set contract: only 'patched' flips patched=True. If this
    # regressed (e.g. truthiness on a string), 'stock'/'unknown' would falsely
    # warn or 'patched' would fail to warn.
    assert make_status(active=active)["patched"] is expect_patched


def test_unrecognized_active_collapses_to_unknown_not_trusted():
    # A forward-incompatible/corrupt 'active' must NOT be trusted. Collapsing to
    # 'unknown' (patched=False, non-alarming) is safe; trusting it as-is could
    # let a bogus value render as a real state. Manifests as active!='unknown'.
    status = make_status(active="totally-bogus")
    assert status["active"] == STACK_UNKNOWN
    assert status["patched"] is False


@pytest.mark.parametrize("bad_marker", [None, [], "patched", 42])
def test_derive_status_degrades_non_dict_to_unknown(bad_marker):
    # read_status feeds derive_status whatever json.load returned; a non-dict
    # (corrupt file, top-level list/scalar) must degrade to unknown rather than
    # raise. Regression: an AttributeError here would break the whole bt_status
    # snapshot the web/device consume.
    assert derive_status(bad_marker) == unknown_status()


def test_derive_status_passes_marker_fields_through():
    # The marker the self-heal installer writes must round-trip into the status.
    # Failure manifests as missing provenance (base_version/fix/reason) in the UI.
    marker = {
        "active": "patched",
        "base_version": "5.82-1.1+rpt1",
        "fix": "bluez 2a6968b",
        "reason": "kernel ext-adv-data validation",
        "applied_at": "2026-06-19T10:00:00Z",
    }
    status = derive_status(marker)
    assert status["patched"] is True
    assert status["base_version"] == "5.82-1.1+rpt1"
    assert status["fix"] == "bluez 2a6968b"


def test_derive_status_missing_optional_fields_default_none():
    # A minimal marker (only 'active') must not KeyError; optional provenance
    # fields default to None. Guards a partial marker from breaking detection.
    status = derive_status({"active": "patched"})
    assert status["patched"] is True
    assert status["base_version"] is None
    assert status["fix"] is None
    assert status["applied_at"] is None


def test_warning_label_only_for_patched():
    # The warning must appear only when actually patched, and stay silent for
    # stock/unknown. Regression either nags on every stock board or hides a real
    # deviation.
    assert warning_label(stock_status()) is None
    assert warning_label(unknown_status()) is None
    assert warning_label(None) is None
    labelled = warning_label(make_status(active=STACK_PATCHED, base_version="5.82-1.1+rpt1"))
    assert labelled is not None
    assert "5.82-1.1+rpt1" in labelled
    assert "stock" in labelled.lower()


def test_warning_label_without_base_version_still_warns():
    # Even when base_version is unknown, a patched stack must still warn (the
    # deviation matters more than the version). Manifests as a missing warning.
    label = warning_label(make_status(active=STACK_PATCHED))
    assert label is not None
    assert "stock" in label.lower()


def test_read_status_missing_file_is_unknown(tmp_path):
    # No marker => self-heal never ran on this image => unknown (non-alarming),
    # never an exception. Guards stock images with no marker file.
    missing = tmp_path / "nope.json"
    assert read_status(str(missing)) == unknown_status()


def test_read_status_malformed_file_is_unknown(tmp_path):
    # A truncated/garbage marker must degrade to unknown, not raise. Regression:
    # a JSON error escaping read_status would break the status snapshot.
    bad = tmp_path / "bluez-patch.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert read_status(str(bad)) == unknown_status()


def test_read_status_reads_patched_marker(tmp_path):
    # The happy path: a well-formed patched marker is read back as patched with
    # its provenance. Failure manifests as the board reporting stock/unknown
    # while actually running the substituted binary.
    marker = {
        "active": "patched",
        "base_version": "5.82-1.1+rpt1",
        "fix": "bluez 2a6968b: Add Ext Adv Data length",
        "reason": "kernel 6.18 ext-adv-data validation",
        "applied_at": "2026-06-19T10:00:00Z",
    }
    path = tmp_path / "bluez-patch.json"
    path.write_text(json.dumps(marker), encoding="utf-8")
    status = read_status(str(path))
    assert status["patched"] is True
    assert status["active"] == STACK_PATCHED
    assert status["base_version"] == "5.82-1.1+rpt1"


def test_read_status_reads_stock_marker(tmp_path):
    # When self-heal determines stock works (e.g. distro shipped the fix), it
    # writes a stock marker; the board must report stock and NOT warn.
    path = tmp_path / "bluez-patch.json"
    path.write_text(json.dumps({"active": "stock"}), encoding="utf-8")
    status = read_status(str(path))
    assert status["active"] == STACK_STOCK
    assert status["patched"] is False
    assert warning_label(status) is None


# --- self-heal progress -----------------------------------------------------
# Guard the transient signal that drives the "self-heal in progress" status,
# so the UI shows a reassuring heal message during the multi-minute on-board
# rebuild instead of the bare ADV_FAILED that stock BlueZ produces meanwhile.


def test_make_progress_not_running_clears_phase():
    # An idle record must never carry a phase/started_at, even if passed: a stale
    # phase left in a not-running record could otherwise make the UI claim a heal
    # is underway. Manifests as phase leaking through when running is False.
    p = make_progress(running=False, phase="building", started_at="2026-06-19T17:00:00Z")
    assert p["running"] is False
    assert p["phase"] is None
    assert p["started_at"] is None


def test_make_progress_running_keeps_phase():
    # The happy path: a running record carries its phase + start time verbatim so
    # the UI can show which step is underway. Failure manifests as a dropped phase.
    p = make_progress(running=True, phase=HEAL_BUILDING, started_at="2026-06-19T17:00:00Z")
    assert p["running"] is True
    assert p["phase"] == HEAL_BUILDING
    assert p["started_at"] == "2026-06-19T17:00:00Z"


@pytest.mark.parametrize("bad", [None, [], "running", 7])
def test_derive_progress_non_dict_is_idle(bad):
    # read_progress feeds derive_progress whatever json.load returned; a non-dict
    # must degrade to idle (running False) rather than raise, so a corrupt file
    # cannot break the status snapshot or wedge the UI in a fake "healing" state.
    assert derive_progress(bad) == idle_progress()


def test_heal_label_none_when_idle():
    # No heal running => no healing label on either surface. Regression: a label
    # here would make the UI perpetually claim a self-heal is in progress.
    assert heal_label(idle_progress()) is None
    assert heal_label(None) is None
    assert heal_label(make_progress(running=False, phase=HEAL_BUILDING)) is None


@pytest.mark.parametrize(
    "phase, fragment",
    [
        (HEAL_PROBING_STOCK, "Checking"),
        (HEAL_BUILDING, "Building"),
        (HEAL_APPLYING, "Applying"),
        (HEAL_PROBING_PATCH, "Verifying"),
    ],
)
def test_heal_label_phase_wording(phase, fragment):
    # Each known phase maps to its own human wording so the user can see which
    # step is running. Regression: a wrong/blank mapping would show a misleading
    # or empty progress message.
    label = heal_label(make_progress(running=True, phase=phase))
    assert label is not None
    assert fragment in label


def test_heal_label_unknown_phase_uses_generic_label():
    # A phase the board does not recognize (e.g. a newer installer) must still
    # produce a sensible generic message, never expose the raw token. Manifests
    # as the raw phase string leaking into the UI.
    label = heal_label(make_progress(running=True, phase="some-future-phase"))
    assert label is not None
    assert "some-future-phase" not in label
    assert "Repairing" in label


def test_read_progress_missing_is_idle(tmp_path):
    # No progress file is the normal case (no heal running) => idle, never an
    # exception. Guards every board that is not mid-heal.
    assert read_progress(str(tmp_path / "nope.progress")) == idle_progress()


def test_read_progress_malformed_is_idle(tmp_path):
    # A truncated/garbage progress file must degrade to idle, not raise, and not
    # wedge the UI into a fake healing state.
    bad = tmp_path / "bluez-selfheal.progress"
    bad.write_text("{not json", encoding="utf-8")
    assert read_progress(str(bad)) == idle_progress()


def test_read_progress_reads_running_record(tmp_path):
    # The happy path: a well-formed running record is read back with its phase so
    # the board can surface the in-progress label. Failure manifests as the board
    # never showing "self-heal in progress" while the rebuild runs.
    path = tmp_path / "bluez-selfheal.progress"
    path.write_text(
        json.dumps({"running": True, "phase": HEAL_BUILDING, "started_at": "2026-06-19T17:00:00Z"}),
        encoding="utf-8",
    )
    progress = read_progress(str(path))
    assert progress["running"] is True
    assert progress["phase"] == HEAL_BUILDING
    assert heal_label(progress) is not None
