"""Tests for Centaur-import progress state (services.centaur_import.import_state).

Context: the Centaur SD import runs on a background thread after the upload
finishes, and the web UI polls a status endpoint to show what the install is
doing (decompress -> mount -> ... -> install 32-bit support -> finalize). This
module owns that structured state (stage, message, derived percent) and persists
it so a fresh page load mid-install still reflects real progress and a stale
"active" state left by a killed process is reconciled rather than spinning
forever.

These pin the stage->percent mapping (the pure, testable core), the terminal
freeze behaviour, and the persistence/reconcile contract.
"""

import time

from universalchess.services.centaur_import.import_state import (
    ImportStage,
    ImportStateStore,
    compute_percent,
)

# Ordered non-terminal stages a real import passes through, used to assert the
# bar only ever moves forward. Kept here (not imported) so a reordering of the
# enum is caught by this explicit expectation rather than silently accepted.
_FORWARD_STAGES = [
    ImportStage.STARTING,
    ImportStage.DECOMPRESSING,
    ImportStage.MOUNTING,
    ImportStage.VALIDATING,
    ImportStage.STAGING,
    ImportStage.INSTALLING_FILES,
    ImportStage.INSTALLING_ARMHF,
    ImportStage.CONFIGURING,
    ImportStage.FINALIZING,
]


def _store(tmp_path):
    """A store backed by a temp file so tests never touch the real state path."""
    return ImportStateStore(tmp_path / "centaur_import_state.json")


def test_start_marks_active_at_starting_stage(tmp_path):
    """start() must begin an active import at the STARTING stage.

    Why this test exists: the status endpoint and banner key off ``active``; if
    start() did not set it, the UI would never show the import as running. The
    percent must be low (not 0, not mid-bar) so the bar appears immediately but
    does not overstate progress.

    How the regression manifests: if start() left active False or an unset stage,
    active would be False here and the stage assertion would fail.
    """
    store = _store(tmp_path)
    state = store.start()

    assert state.active is True
    assert state.stage == ImportStage.STARTING
    status = store.status_dict()
    assert status["active"] is True
    assert status["stage"] == "starting"
    assert 0 < status["percent"] < 100


def test_update_advances_stage_and_message(tmp_path):
    """update() must record the new stage/message and reflect it in status_dict.

    Why this test exists: the whole feature is showing WHAT is happening; the
    message string ("Installing 32-bit support...") is what the user reads. If
    update did not persist the message/stage, the bar would sit on the initial
    "Starting..." text through the entire install.

    How the regression manifests: a no-op update would leave stage/message at the
    STARTING values, failing the assertions below.
    """
    store = _store(tmp_path)
    store.start()
    store.update(ImportStage.MOUNTING, "Mounting SD image...")

    status = store.status_dict()
    assert status["stage"] == "mounting"
    assert status["message"] == "Mounting SD image..."


def test_percent_is_monotonic_across_forward_stages(tmp_path):
    """Percent must never decrease as the import advances through its stages.

    Why this test exists: a bar that jumps backwards between stages reads as a
    restart/glitch to the user. The stage->band mapping must be monotonic across
    the real forward order.

    How the regression manifests: mis-ordering a band (e.g. CONFIGURING below
    STAGING) makes the sequence non-increasing and trips the assertion, naming
    the pair that regressed.
    """
    store = _store(tmp_path)
    store.start()
    now = time.time()

    last = -1
    for stage in _FORWARD_STAGES:
        store.update(stage, stage.value)
        percent = store.status_dict(now=now)["percent"]
        assert percent >= last, f"{stage} percent {percent} < previous {last}"
        last = percent


def test_armhf_stage_creeps_with_elapsed_time_but_never_completes(tmp_path):
    """The long armhf-install stage must advance over time and stay below 100.

    Why this test exists: on arm64 the "install 32-bit support" step is an apt
    run with no measurable progress -- the exact phase where the old UI froze at
    100%. It must creep (so the bar is not stuck) yet never reach 100 while still
    running (which would look finished). This mirrors the engine BUILDING creep.

    How the regression manifests: a point value here would not move between polls
    (frozen bar); an uncapped creep would hit 100 mid-install (false "done"). The
    two assertions catch each failure mode independently.
    """
    store = _store(tmp_path)
    store.start()
    store.update(ImportStage.INSTALLING_ARMHF, "Installing 32-bit support...")
    state = store.get()
    base = state.stage_started_at

    early = compute_percent(state, base + 1)
    later = compute_percent(state, base + 60)
    capped = compute_percent(state, base + 100_000)

    assert later > early, "armhf percent must increase with elapsed time"
    assert capped < 100, "armhf creep must never reach 100 while running"


def test_finish_success_completes_at_full_percent(tmp_path):
    """finish(success=True) must mark COMPLETED, inactive, at 100%.

    Why this test exists: the frontend stops polling and shows the success text
    on the active->inactive transition; the bar must read 100 and the result must
    carry success so the message is correct.

    How the regression manifests: leaving active True would loop the poll forever;
    a percent below 100 would show an install that "finished" at, say, 98%.
    """
    store = _store(tmp_path)
    store.start()
    store.finish(success=True)

    status = store.status_dict()
    assert status["stage"] == "completed"
    assert status["active"] is False
    assert status["percent"] == 100
    assert status["result"] == {"success": True, "error": None}


def test_finish_failure_freezes_percent_and_reports_error(tmp_path):
    """finish(success=False) must freeze the bar where it stopped and hold the error.

    Why this test exists: a failed import must not snap the bar to 0 or 100 -- the
    user should see it stopped mid-way, with the actionable error text. The percent
    is snapshotted at the failing stage so it holds that position on later polls.

    How the regression manifests: recomputing percent after the terminal flag flips
    (instead of using the snapshot) could read 0; dropping the error would hide why
    the import failed.
    """
    store = _store(tmp_path)
    store.start()
    store.update(ImportStage.STAGING, "Reading image contents...")
    frozen = store.status_dict()["percent"]

    store.finish(success=False, error="Could not install 32-bit support.")

    status = store.status_dict()
    assert status["stage"] == "failed"
    assert status["active"] is False
    assert status["percent"] == frozen
    assert status["result"] == {"success": False, "error": "Could not install 32-bit support."}
    assert status["message"] == "Could not install 32-bit support."


def test_reconcile_interrupted_flags_orphaned_active_import(tmp_path):
    """A persisted *active* import in a fresh process is reconciled to INTERRUPTED.

    Why this test exists: if the process/board restarts mid-import, no thread
    exists to finish the work, but the persisted state still says active. Left
    as-is the banner would show a perpetual "importing" and the panel would poll a
    dead install. Reconcile (run once at startup) flips it to an inactive terminal
    state so the UI stops waiting.

    How the regression manifests: if reconcile ignored active state, the fresh
    store would still report active True and the banner would never clear.
    """
    path = tmp_path / "centaur_import_state.json"
    writer = ImportStateStore(path)
    writer.start()
    writer.update(ImportStage.INSTALLING_ARMHF, "Installing 32-bit support...")

    fresh = ImportStateStore(path)
    reconciled = fresh.reconcile_interrupted()

    assert reconciled is not None
    assert reconciled.stage == ImportStage.INTERRUPTED
    assert reconciled.active is False
    assert fresh.status_dict()["active"] is False


def test_reconcile_interrupted_ignores_finished_import(tmp_path):
    """Reconcile must not touch an import that already finished cleanly.

    Why this test exists: a COMPLETED state persisted from a prior successful
    import must survive a restart unchanged (so a returning client still sees the
    success), not be rewritten to interrupted.

    How the regression manifests: reconciling any persisted state regardless of
    ``active`` would clobber the completed result and mislabel a success as
    interrupted.
    """
    path = tmp_path / "centaur_import_state.json"
    writer = ImportStateStore(path)
    writer.start()
    writer.finish(success=True)

    fresh = ImportStateStore(path)
    assert fresh.reconcile_interrupted() is None
    assert fresh.status_dict()["stage"] == "completed"


def test_status_dict_shape_when_idle(tmp_path):
    """With no import ever started, status_dict is a stable idle snapshot.

    Why this test exists: the endpoint is polled before any import runs; it must
    return a well-formed idle payload (active False, percent 0) rather than null
    or a partial dict the frontend would choke on.

    How the regression manifests: returning None/{} for the empty case would break
    the poll's destructuring on the client.
    """
    store = _store(tmp_path)
    status = store.status_dict()

    assert status["active"] is False
    assert status["stage"] is None
    assert status["percent"] == 0
    assert status["message"] == ""
    assert status["result"] is None


def test_state_persists_across_store_instances(tmp_path):
    """A second store reading the same file sees the first store's live state.

    Why this test exists: the status endpoint may read through a different code
    path/instance than the writer thread; persistence is what lets a fresh page
    load (or another browser) see the in-progress stage. This guards the
    atomically-written JSON round-trips the stage and message.

    How the regression manifests: if state lived only in memory, the reader store
    would report idle while an import is actually running.
    """
    path = tmp_path / "centaur_import_state.json"
    writer = ImportStateStore(path)
    writer.start()
    writer.update(ImportStage.INSTALLING_FILES, "Installing Centaur software...")

    reader = ImportStateStore(path)
    status = reader.status_dict()
    assert status["active"] is True
    assert status["stage"] == "installing_files"
    assert status["message"] == "Installing Centaur software..."
