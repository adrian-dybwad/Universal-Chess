"""Tests for the persistent application event log (services.event_log).

The event log is the data source for the Settings "Event Log" viewer and the
record of how long installs took. These tests pin the on-disk contract (JSON
Lines, newest-first reads, duration capture), the best-effort guarantee
(logging never raises into callers), resilience to torn lines, and the bash CLI
that the root-run self-heal uses to emit its completion event.
"""

import json
import subprocess
import sys

from universalchess.services import event_log


def _read_lines(path):
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_log_event_writes_one_json_line_with_expected_fields(tmp_path):
    # Guards the core on-disk contract the viewer parses. Manifestation if the
    # record shape drifts (missing ts/level/category/message): the viewer rows
    # render blank or the endpoint cannot group by category.
    log_path = tmp_path / "events.jsonl"
    event_log.log_event("engine_install", "Installed Zahak (v25.5)",
                         level="info", duration_ms=152000, path=log_path)

    lines = _read_lines(log_path)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["category"] == "engine_install"
    assert rec["message"] == "Installed Zahak (v25.5)"
    assert rec["level"] == "info"
    assert rec["duration_ms"] == 152000
    # ts is an ISO-8601 UTC instant with the trailing Z the format promises.
    assert rec["ts"].endswith("Z")
    assert "T" in rec["ts"]


def test_duration_omitted_when_not_provided(tmp_path):
    # A non-timed event must not fabricate a duration; the viewer shows a
    # duration column only when present. Manifestation: a spurious duration_ms
    # (e.g. 0) would make instantaneous events look like they "took 0ms".
    log_path = tmp_path / "events.jsonl"
    event_log.log_event("system", "Service started", path=log_path)

    rec = _read_lines(log_path)[0]
    assert "duration_ms" not in rec


def test_unknown_level_degrades_to_info(tmp_path):
    # The viewer styles a closed set of levels; an unknown level would lose its
    # styling. Pin that it is normalized rather than passed through.
    log_path = tmp_path / "events.jsonl"
    event_log.log_event("system", "msg", level="catastrophe", path=log_path)
    assert _read_lines(log_path)[0]["level"] == "info"


def test_read_events_returns_newest_first_and_respects_limit(tmp_path):
    # The viewer shows the most recent activity at the top, bounded. This
    # verifies both ordering (reverse of append order) and the limit slice.
    # Manifestation if reversed/limit logic breaks: stale events show first, or
    # an unbounded read floods the UI.
    log_path = tmp_path / "events.jsonl"
    for i in range(5):
        event_log.log_event("system", f"event-{i}", path=log_path)

    newest_two = event_log.read_events(limit=2, path=log_path)
    assert [e["message"] for e in newest_two] == ["event-4", "event-3"]


def test_read_events_skips_torn_or_malformed_lines(tmp_path):
    # A crash mid-write can leave a partial final line; one bad line must not
    # blank the whole viewer. Manifestation if json errors propagate: read_events
    # raises (500 on the endpoint) instead of returning the good records.
    log_path = tmp_path / "events.jsonl"
    event_log.log_event("system", "good-1", path=log_path)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026', )  # torn line, no newline-terminated valid JSON
        fh.write("\n")
    event_log.log_event("system", "good-2", path=log_path)

    messages = [e["message"] for e in event_log.read_events(path=log_path)]
    assert messages == ["good-2", "good-1"]


def test_read_events_returns_empty_when_no_log(tmp_path):
    # Fresh device: no events file yet. The endpoint must return an empty list,
    # not error. Manifestation: a missing-file exception -> 500 on first load.
    assert event_log.read_events(path=tmp_path / "nope.jsonl") == []


def test_log_event_never_raises_on_unwritable_path(tmp_path, caplog):
    # Best-effort contract: an audit line must never crash the install it
    # reports on. Point the log at a path whose parent is a *file* (mkdir
    # fails), and assert the call returns without raising.
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x")
    bad_path = not_a_dir / "events.jsonl"

    event_log.log_event("system", "should not raise", path=bad_path)  # no exception
    assert event_log.read_events(path=bad_path) == []


def test_timed_event_logs_duration_and_reraises(tmp_path):
    # timed_event must record a duration on BOTH success and failure, then
    # re-raise on failure so it does not swallow errors. Manifestation if it
    # swallows: a failing operation looks successful and no exception surfaces.
    log_path = tmp_path / "events.jsonl"
    with event_log.timed_event("bluez_selfheal", "ok-block", path=log_path):
        pass

    raised = False
    try:
        with event_log.timed_event("bluez_selfheal", "bad-block", path=log_path):
            raise ValueError("boom")
    except ValueError:
        raised = True
    assert raised

    records = event_log.read_events(path=log_path)
    assert [r["message"] for r in records] == ["bad-block", "ok-block"]
    assert all("duration_ms" in r for r in records)


def test_cli_emits_event_by_file_path(tmp_path, monkeypatch):
    # The root-run bash self-heal emits its completion event by invoking
    # event_log.py BY FILE PATH (not `python -m`), because the system python3 it
    # runs under cannot import the package (services/__init__ pulls in
    # third-party deps). Run it the same way here to guard that the module is
    # stdlib-only and works standalone, and that its record matches the
    # in-process format. Manifestation if a non-stdlib import creeps in: this
    # subprocess fails to import and returns non-zero, so the self-heal would
    # silently stop logging on-device.
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(log_path))

    result = subprocess.run(
        [sys.executable, event_log.__file__,
         "--category", "bluez_selfheal", "--level", "info", "--duration-ms", "152000",
         "--", "Self-heal complete: patched bluetoothd"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    rec = _read_lines(log_path)[0]
    assert rec["category"] == "bluez_selfheal"
    assert rec["message"] == "Self-heal complete: patched bluetoothd"
    assert rec["duration_ms"] == 152000
    assert rec["level"] == "info"
