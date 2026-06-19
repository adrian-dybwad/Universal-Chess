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
    STACK_PATCHED,
    STACK_STOCK,
    STACK_UNKNOWN,
    derive_status,
    make_status,
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
