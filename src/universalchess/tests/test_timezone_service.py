"""Tests for the timezone service (services/timezone_service.py).

The service persists the chosen IANA zone in ``[system] timezone`` and applies
it to the OS via the pinned ``uc-set-timezone`` helper under ``sudo -n``. The
command runner is injected so the privileged invocation is asserted without
root, and Settings persistence is patched so the tests don't touch ``/opt``.

Each test states the regression it guards and how it would surface.
"""

import subprocess
from types import SimpleNamespace

import pytest

from universalchess.services import timezone_service as tzs


@pytest.fixture
def captured_writes(monkeypatch):
    """Capture Settings.write calls and stub Settings.read, off the /opt store."""
    writes = []
    monkeypatch.setattr(tzs.Settings, "write", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(tzs.Settings, "read", lambda section, key, default="": default)
    return writes


def _runner(returncode=0, stderr=""):
    calls = []

    def run(args, timeout):
        calls.append((list(args), timeout))
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_list_timezones_sorted_and_includes_utc():
    """list_timezones() returns a sorted list containing common zones.

    Guards the option-list source; a regression that returned an unsorted or
    empty set would break the Settings dropdown ordering/content.
    """
    zones = tzs.list_timezones()
    assert "UTC" in zones
    assert "Europe/Oslo" in zones
    assert zones == sorted(zones)


def test_get_timezone_defaults_to_utc(monkeypatch):
    """get_timezone() falls back to UTC when the OS zone and store are both empty.

    Guards the empty-store case: an unset key must not surface as "" (which the
    UI would show as a blank selection). The OS read is forced to None so this
    exercises the store->UTC fallback deterministically (independent of the test
    machine's real /etc/timezone).
    """
    monkeypatch.setattr(tzs, "_read_os_timezone", lambda: None)
    monkeypatch.setattr(tzs.Settings, "read", lambda section, key, default="": "")
    assert tzs.get_timezone() == "UTC"


def test_get_timezone_prefers_live_os_zone_over_store(monkeypatch):
    """get_timezone() returns the live OS zone even when the store disagrees.

    Why: the OS clock is what the e-paper actually displays; the selector must
    match it. How a regression manifests: reading only the persisted value shows
    the stored zone (e.g. the "UTC" default on a fresh device) while the clock
    runs in a different zone -- the exact mismatch this fix addresses.
    """
    monkeypatch.setattr(tzs, "_read_os_timezone", lambda: "America/Denver")
    monkeypatch.setattr(tzs.Settings, "read", lambda section, key, default="": "UTC")
    assert tzs.get_timezone() == "America/Denver"


def test_get_timezone_falls_back_to_store_when_os_unreadable(monkeypatch):
    """When the OS zone can't be read, the persisted setting is used.

    Guards the off-board/dev path: a stored zone must still surface rather than
    collapsing straight to UTC when /etc/timezone and /etc/localtime are absent.
    """
    monkeypatch.setattr(tzs, "_read_os_timezone", lambda: None)
    monkeypatch.setattr(tzs.Settings, "read", lambda section, key, default="": "Europe/Oslo")
    assert tzs.get_timezone() == "Europe/Oslo"


def test_read_os_timezone_reads_etc_timezone(monkeypatch, tmp_path):
    """_read_os_timezone() returns a valid IANA name from /etc/timezone.

    Why: /etc/timezone is the primary, symlink-free source of the OS zone. How a
    regression manifests: the name is not read (returns None) so get_timezone
    silently falls back to the store and can misreport the clock.
    """
    etc_tz = tmp_path / "timezone"
    etc_tz.write_text("Europe/Oslo\n", encoding="utf-8")
    monkeypatch.setattr(tzs, "_ETC_TIMEZONE", etc_tz)
    # Point localtime at a nonexistent path so only the /etc/timezone branch runs.
    monkeypatch.setattr(tzs, "_ETC_LOCALTIME", tmp_path / "localtime")
    assert tzs._read_os_timezone() == "Europe/Oslo"


def test_read_os_timezone_parses_localtime_symlink(monkeypatch, tmp_path):
    """_read_os_timezone() parses the zone from the /etc/localtime symlink target.

    Why: some images omit /etc/timezone and keep only the /etc/localtime symlink
    into the zoneinfo tree. How a regression manifests: the symlink is not parsed
    (returns None) and the OS zone is lost on those images.
    """
    monkeypatch.setattr(tzs, "_ETC_TIMEZONE", tmp_path / "timezone")  # absent
    link = tmp_path / "localtime"
    link.symlink_to("/usr/share/zoneinfo/America/Denver")
    monkeypatch.setattr(tzs, "_ETC_LOCALTIME", link)
    assert tzs._read_os_timezone() == "America/Denver"


def test_read_os_timezone_rejects_unknown_name(monkeypatch, tmp_path):
    """An unrecognised /etc/timezone value yields None (not a bogus zone).

    Why: get_timezone must not surface a name the selector/zoneinfo can't honour.
    How a regression manifests: a corrupt file leaks a non-IANA string into the
    UI and into set_timezone validation.
    """
    etc_tz = tmp_path / "timezone"
    etc_tz.write_text("Not/AZone\n", encoding="utf-8")
    monkeypatch.setattr(tzs, "_ETC_TIMEZONE", etc_tz)
    monkeypatch.setattr(tzs, "_ETC_LOCALTIME", tmp_path / "localtime")
    assert tzs._read_os_timezone() is None


def test_set_valid_timezone_persists_and_invokes_helper(captured_writes):
    """A valid zone is written to [system] timezone and applied via sudo helper.

    Guards the happy path and the exact privileged argv. Manifests as a missing
    write, a wrong section/key, or a mangled command line.
    """
    run = _runner(returncode=0)
    applied = tzs.set_timezone("Europe/Oslo", helper_path="/x/uc-set-timezone", run=run)

    assert applied is True
    assert captured_writes == [("system", "timezone", "Europe/Oslo", "UTC")]
    assert run.calls[0][0] == ["sudo", "-n", "/x/uc-set-timezone", "Europe/Oslo"]


def test_set_invalid_timezone_raises_and_does_not_persist_or_apply(captured_writes):
    """An unknown zone raises ValueError, with no write and no privileged call.

    This is the validation boundary: a regression would let an arbitrary string
    be written and passed to the helper. Manifests as a recorded write/call.
    """
    run = _runner(returncode=0)
    with pytest.raises(ValueError):
        tzs.set_timezone("Mars/Olympus_Mons", helper_path="/x/uc-set-timezone", run=run)
    assert captured_writes == []
    assert run.calls == []


def test_apply_failure_still_persists_and_returns_false(captured_writes):
    """A helper failure persists the choice but reports not-applied (False).

    Guards the best-effort contract: the user's selection must survive a failed
    privileged apply so the UI stays consistent and a retry can re-apply.
    Manifests as True (claiming applied) or a dropped write.
    """
    run = _runner(returncode=1, stderr="sudo: no password")
    applied = tzs.set_timezone("Europe/Oslo", helper_path="/x/uc-set-timezone", run=run)

    assert applied is False
    assert captured_writes == [("system", "timezone", "Europe/Oslo", "UTC")]


def test_apply_swallows_runner_exception(captured_writes):
    """An OSError from the runner is swallowed (saved, not applied).

    Guards against a missing helper/sudo raising out of set_timezone. Manifests
    as an exception propagating instead of returning False.
    """
    def run(args, timeout):
        raise OSError("sudo not found")

    applied = tzs.set_timezone("Europe/Oslo", helper_path="/x/uc-set-timezone", run=run)
    assert applied is False
    assert captured_writes == [("system", "timezone", "Europe/Oslo", "UTC")]
