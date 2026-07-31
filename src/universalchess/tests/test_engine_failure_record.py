"""Tests for the durable per-engine failure record store.

Why these tests exist
---------------------
Two engine failures were previously written to the log and nowhere else: an
install that failed, and a post-install UCI probe that failed. The second is the
worse of the two, because the binary is on disk afterwards, so the engines list
reports "Installed" while every feature behind it -- the strength ladder, the
profile editor, playing a game -- is unavailable. Anyone reporting the problem
can only send a screenshot, and the screenshot says nothing.

This store is what lets that reason outlive the request that produced it. The
behaviors pinned here are the ones a regression would quietly undo: a failure
survives a restart, a later success clears it (so a fixed engine stops being
flagged), dismissal acknowledges one failure without muting the next, and the
store never persists anything derived from exception text.
"""

import json

import pytest

from universalchess.services.engine_failure_record import (
    PHASE_INITIALIZE,
    PHASE_INSTALL,
    EngineFailureStore,
)

REASON_INCOMPATIBLE = "incompatible_binary"
REASON_CRASHED = "crashed_at_startup"
REASON_BUILD_FAILED = "build_failed"


@pytest.fixture
def store(tmp_path):
    """A failure store backed by a throwaway file in a temp dir."""
    return EngineFailureStore(path=tmp_path / "failures.json")


def test_records_phase_and_reason_with_a_timestamp(store):
    """A recorded failure carries which phase failed, why, and when.

    Why this test exists: the UI needs all three -- the phase chooses the
    sentence ("could not be installed" vs "installed but did not start"), the
    reason chooses the remedy, and the timestamp lets a stale record be
    recognised after a later reinstall.

    How a regression manifests: a store that keeps only the reason makes install
    and initialize failures indistinguishable, so the card offers the wrong
    action (Repair for something that needs a rebuild).
    """
    store.record_failure("ct800", phase=PHASE_INITIALIZE, reason_code=REASON_INCOMPATIBLE)

    failure = store.get("ct800")
    assert failure is not None
    assert failure.phase == PHASE_INITIALIZE
    assert failure.reason_code == REASON_INCOMPATIBLE
    assert failure.failed_at is not None


def test_records_the_technical_detail_alongside_the_reason(store):
    """The short technical token is stored with the reason.

    Why this test exists: the reason code drives the sentence the UI shows, but
    the card also offers the underlying detail so a user can screenshot
    something a maintainer can act on. Storing only the code would leave the
    expandable details panel with nothing to show.

    How a regression manifests: detail is None and the details panel renders an
    empty row, which looks like a rendering bug rather than a missing field.
    """
    store.record_failure(
        "ct800",
        phase=PHASE_INITIALIZE,
        reason_code=REASON_INCOMPATIBLE,
        detail="OSError ENOEXEC",
    )

    failure = store.get("ct800")
    assert failure is not None
    assert failure.detail == "OSError ENOEXEC"


def test_a_new_failure_is_not_dismissed(store):
    """A freshly recorded failure starts visible.

    Why this test exists: dismissal is per-failure acknowledgement, not a
    permanent mute for the engine. A user who dismisses one failure, reinstalls,
    and hits a new one must see the new one.

    How a regression manifests: dismissed defaults to True (or is carried over
    from the previous record) and the second failure is silent -- strictly worse
    than the original bug, because now the system knows and says nothing.
    """
    store.record_failure("ct800", phase=PHASE_INITIALIZE, reason_code=REASON_INCOMPATIBLE)

    failure = store.get("ct800")
    assert failure is not None
    assert failure.dismissed is False


def test_dismiss_hides_the_failure_without_forgetting_it(store):
    """Dismissing marks the record acknowledged but keeps it readable.

    Why this test exists: dismissal only silences the card notice. The engine is
    still broken, so the record has to survive for the badge and for anyone
    inspecting state afterwards -- deleting it would make the card look healthy
    again.

    How a regression manifests: get() returns None after dismiss, so the reason
    is lost the moment the user acknowledges it.
    """
    store.record_failure("ct800", phase=PHASE_INITIALIZE, reason_code=REASON_INCOMPATIBLE)

    store.dismiss("ct800")

    failure = store.get("ct800")
    assert failure is not None
    assert failure.dismissed is True
    assert failure.reason_code == REASON_INCOMPATIBLE


def test_a_new_failure_reopens_a_dismissed_one(store):
    """Recording again after a dismissal makes the notice visible once more.

    Why this test exists: the same engine failing again after the user
    acknowledged the previous attempt is new information. Staying dismissed
    would hide a repeat failure, which is the case where the user most needs to
    know nothing changed.

    How a regression manifests: dismissed remains True after a fresh
    record_failure and the card shows no notice for the new failure.
    """
    store.record_failure("ct800", phase=PHASE_INITIALIZE, reason_code=REASON_INCOMPATIBLE)
    store.dismiss("ct800")

    store.record_failure("ct800", phase=PHASE_INITIALIZE, reason_code=REASON_CRASHED)

    failure = store.get("ct800")
    assert failure is not None
    assert failure.dismissed is False
    assert failure.reason_code == REASON_CRASHED


def test_dismiss_is_a_noop_for_an_engine_with_no_record(store):
    """Dismissing an engine that never failed does nothing and does not raise.

    Why this test exists: the dismiss endpoint is reachable with any engine name,
    including one whose record was cleared by a concurrent successful reinstall.

    How a regression manifests: a KeyError becomes a 500 on a button that should
    always be harmless.
    """
    store.dismiss("stockfish")

    assert store.get("stockfish") is None


def test_get_returns_none_for_a_healthy_engine(store):
    """An engine with no recorded failure reports None, not a fabricated record.

    Why this test exists: the null case is what the overwhelming majority of
    engines are, and every consumer branches on it. A store that invented an
    empty record would put a warning badge on every healthy engine.

    How a regression manifests: get() returns a record whose reason_code is
    empty, and the card renders a failure note for an engine that works.
    """
    assert store.get("stockfish") is None


def test_a_later_success_clears_the_failure(store):
    """clear() removes the record so a fixed engine stops being flagged.

    Why this test exists: the record is written on failure and must be retracted
    on the next success, otherwise a user who reinstalls and fixes the engine
    keeps the warning forever and learns to ignore it.

    How a regression manifests: get() still returns the old failure after clear,
    so the card shows a permanent warning on a working engine.
    """
    store.record_failure("ct800", phase=PHASE_INITIALIZE, reason_code=REASON_CRASHED)

    store.clear("ct800")

    assert store.get("ct800") is None


def test_clear_is_a_noop_for_an_engine_with_no_record(store):
    """Clearing an engine that never failed does nothing and does not raise.

    Why this test exists: clear() runs on every successful install and seed,
    which for almost every engine means there was never a record. It has to be
    the cheap, silent path.

    How a regression manifests: a KeyError propagates out of the success path and
    turns a working install into a reported failure.
    """
    store.clear("stockfish")

    assert store.get("stockfish") is None


def test_a_new_failure_replaces_the_previous_one(store):
    """Only the most recent failure per engine is kept.

    Why this test exists: the user needs the current reason, not a history. A
    stale first reason displayed after a second, different failure would send
    them after the wrong fix.

    How a regression manifests: get() returns the install-phase build failure
    after the engine has since installed and failed to start, so the card asks
    them to retry an install that already succeeded.
    """
    store.record_failure("ct800", phase=PHASE_INSTALL, reason_code=REASON_BUILD_FAILED)

    store.record_failure("ct800", phase=PHASE_INITIALIZE, reason_code=REASON_INCOMPATIBLE)

    failure = store.get("ct800")
    assert failure is not None
    assert (failure.phase, failure.reason_code) == (PHASE_INITIALIZE, REASON_INCOMPATIBLE)


def test_failures_are_tracked_per_engine(store):
    """Recording one engine's failure leaves the others untouched.

    Why this test exists: the engines list reads this store once per card. A
    single shared slot would smear one engine's failure across every card.

    How a regression manifests: arasan's lookup returns ct800's failure, so
    healthy engines display a reason belonging to a different engine.
    """
    store.record_failure("ct800", phase=PHASE_INITIALIZE, reason_code=REASON_INCOMPATIBLE)
    store.record_failure("arasan", phase=PHASE_INSTALL, reason_code=REASON_BUILD_FAILED)

    ct800 = store.get("ct800")
    arasan = store.get("arasan")
    assert ct800 is not None and arasan is not None
    assert ct800.reason_code == REASON_INCOMPATIBLE
    assert arasan.reason_code == REASON_BUILD_FAILED
    assert store.get("weiss") is None


def test_record_survives_a_fresh_process(tmp_path):
    """A failure written by one process is readable by the next.

    Why this test exists: the probe fails inside the install thread, and the user
    reads the result after a page reload or a service restart. An in-memory-only
    store would forget precisely when it matters -- and the durability of this
    file is why it lives under CONFIG_DIR rather than TMP_DIR.

    How a regression manifests: the second store reports None and the card is
    back to a bare "Installed" badge after any restart.
    """
    path = tmp_path / "failures.json"
    EngineFailureStore(path=path).record_failure(
        "ct800", phase=PHASE_INITIALIZE, reason_code=REASON_INCOMPATIBLE
    )

    reloaded = EngineFailureStore(path=path).get("ct800")

    assert reloaded is not None
    assert reloaded.reason_code == REASON_INCOMPATIBLE


def test_a_corrupt_file_reads_as_no_failures(tmp_path):
    """An unparseable store degrades to empty instead of raising.

    Why this test exists: this file is read while rendering the engines list. A
    truncated write (power loss mid-install) must cost one forgotten reason, not
    the entire page.

    How a regression manifests: json.JSONDecodeError escapes and
    GET /api/engines/all returns 500, so no engine renders at all.
    """
    path = tmp_path / "failures.json"
    path.write_text("{not valid json")

    assert EngineFailureStore(path=path).get("ct800") is None


def test_rejects_an_empty_reason_code(store):
    """A blank reason is refused rather than stored as an unexplained failure.

    Why this test exists: the reason is the entire value of the record. A record
    with no reason produces a warning badge that says nothing, which is worse
    than the silent failure this replaces because it looks like the system knows
    something it does not.

    How a regression manifests: an empty-string reason is accepted and the card
    renders a failure note with a missing translation key.
    """
    with pytest.raises(ValueError):
        store.record_failure("ct800", phase=PHASE_INITIALIZE, reason_code="")


def test_persisted_reason_is_the_bare_code(tmp_path):
    """Nothing beyond the fixed code reaches disk (and therefore the API).

    Why this test exists: the record is serialized straight to the client, so any
    implementation that helpfully appended the exception's message would publish
    absolute filesystem paths -- the stack-trace exposure finding this design
    exists to avoid. Asserting on the file, not the accessor, catches a leak that
    a getter might filter.

    How a regression manifests: the engine path below appears in the JSON, and
    from there in the browser.
    """
    path = tmp_path / "failures.json"
    store = EngineFailureStore(path=path)
    store.record_failure(
        "ct800",
        phase=PHASE_INITIALIZE,
        reason_code=REASON_INCOMPATIBLE,
        detail="OSError ENOEXEC",
    )

    raw = json.loads(path.read_text())

    assert raw["ct800"]["reason_code"] == REASON_INCOMPATIBLE
    assert raw["ct800"]["detail"] == "OSError ENOEXEC"
    assert "/opt/universalchess" not in path.read_text()
