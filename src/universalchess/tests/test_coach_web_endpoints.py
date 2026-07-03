"""Tests for the web AI-coach endpoints.

The live-board and analysis views read GET /api/coach/statement/<gameid>/<ply> to
show (or generate) a played move's coach statement, and POST /api/coach/tip to
coach a hinted move. These tests pin: stored statements are returned without a
second AI call, absent statements are generated + persisted, the not-configured
and out-of-range guards, and the tip request/response shape. A regression would
re-bill the AI for an already-coached move, 500 on unknown plies, or leak that
the coach is misconfigured as a generic error.
"""

import importlib
import json
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

from PIL import Image

import universalchess.db.uri as _uri  # noqa: E402

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp  # noqa: E402
finally:
    Image.open = _orig_image_open

from universalchess.managers.game import coach_persistence, coach_tips  # noqa: E402
from universalchess.services import coach as coach_service  # noqa: E402
from universalchess.services.coach import CoachConfig, CoachError  # noqa: E402

STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@pytest.fixture
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


def _configured(monkeypatch):
    """Point the endpoint's config reader at a configured provider.

    Also pins the notation reader to a deterministic value so tests that let the
    real request builder run don't depend on the on-disk centaur.ini.
    """
    monkeypatch.setattr(
        webapp,
        "_read_coach_config",
        lambda: CoachConfig(provider="openai", api_key="k", model="gpt-4o-mini"),
    )
    monkeypatch.setattr(webapp, "_read_notation", lambda: "san")


def test_statement_returns_stored_without_generating(client, monkeypatch):
    # A stored statement must be returned as cached and must NOT trigger a second
    # (billed) AI call -- the "fetch once, reuse forever" contract for the web.
    monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: "Stored.")

    def fail_generate(*a, **k):
        raise AssertionError("generate must not run when a statement is stored")

    monkeypatch.setattr(coach_service, "generate_coach_statement", fail_generate)

    resp = client.get("/api/coach/statement/7/3")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {"statement": "Stored.", "cached": True, "error": None}


def test_statement_generates_persists_when_absent(client, monkeypatch):
    # With no stored statement, the endpoint must reconstruct the move context,
    # generate, persist, and return it (cached=false). Regression: skipping the
    # save would re-generate (re-bill) on every view of that move.
    saved = {}
    _configured(monkeypatch)
    monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: None)
    monkeypatch.setattr(coach_persistence, "get_move_context", lambda g, p: (STARTPOS, "e2e4"))
    monkeypatch.setattr(coach_persistence, "get_move_evals", lambda g, p: (20, 35))
    monkeypatch.setattr(
        coach_persistence,
        "save_coach_statement_if_absent",
        lambda g, p, s: saved.update({"g": g, "p": p, "s": s}) or s,
    )

    captured = {}

    def fake_generate(config, req):
        captured["san"] = req.move_text
        captured["eval_before"] = req.eval_before_cp
        captured["eval_after"] = req.eval_after_cp
        return "Grabs the center."

    monkeypatch.setattr(coach_service, "generate_coach_statement", fake_generate)

    resp = client.get("/api/coach/statement/7/1")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "statement": "Grabs the center.",
        "cached": False,
        "error": None,
    }
    # Persisted against the requested game/ply with the generated text.
    assert saved == {"g": 7, "p": 1, "s": "Grabs the center."}
    # The reconstructed prompt used the real SAN and the eval swing from the DB.
    assert captured == {"san": "e4", "eval_before": 20, "eval_after": 35}


def test_statement_returns_canonical_when_another_writer_won(client, monkeypatch):
    # Race: the initial read misses, we generate, but by save time another writer
    # (e.g. the board) has committed a different statement. save-if-absent returns
    # that canonical text and the endpoint must return IT, not our generation -- so
    # web and board converge. Regression: returning our own text would show the same
    # move coached differently on the two surfaces.
    _configured(monkeypatch)
    monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: None)
    monkeypatch.setattr(coach_persistence, "get_move_context", lambda g, p: (STARTPOS, "e2e4"))
    monkeypatch.setattr(coach_persistence, "get_move_evals", lambda g, p: (None, None))
    monkeypatch.setattr(
        coach_service, "generate_coach_statement", lambda c, r: "Our late generation."
    )
    monkeypatch.setattr(
        coach_persistence,
        "save_coach_statement_if_absent",
        lambda g, p, s: "Board committed first.",
    )

    resp = client.get("/api/coach/statement/7/1")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "statement": "Board committed first.",
        "cached": False,
        "error": None,
    }


def test_statement_not_configured_is_reported(client, monkeypatch):
    # When no provider/key is set the endpoint must say so explicitly (so the UI
    # hides the panel) rather than attempting a generation or 500-ing.
    monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: None)
    monkeypatch.setattr(webapp, "_read_coach_config", lambda: CoachConfig(provider="none"))

    resp = client.get("/api/coach/statement/7/1")
    assert resp.status_code == 200
    assert json.loads(resp.data)["error"] == "not_configured"


def test_statement_out_of_range_returns_404(client, monkeypatch):
    # An unknown ply (no move context) must be a clean 404 out_of_range, not a
    # crash from indexing past the move list.
    _configured(monkeypatch)
    monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: None)
    monkeypatch.setattr(coach_persistence, "get_move_context", lambda g, p: None)

    resp = client.get("/api/coach/statement/7/99")
    assert resp.status_code == 404
    assert json.loads(resp.data)["error"] == "out_of_range"


def test_statement_generation_failure_returns_502(client, monkeypatch):
    # A provider failure must surface as 502 with a FIXED error token (UI shows
    # retry) and must NOT leak the underlying exception text (paths, URLs, internals)
    # to the client. It must also not persist anything so a later retry can succeed.
    _configured(monkeypatch)
    monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: None)
    monkeypatch.setattr(coach_persistence, "get_move_context", lambda g, p: (STARTPOS, "e2e4"))
    monkeypatch.setattr(coach_persistence, "get_move_evals", lambda g, p: (None, None))

    def boom(*a, **k):
        raise CoachError("provider down: https://secret.internal/path leaked")

    monkeypatch.setattr(coach_service, "generate_coach_statement", boom)

    def fail_save(*a, **k):
        raise AssertionError("must not persist a failed generation")

    monkeypatch.setattr(coach_persistence, "save_coach_statement_if_absent", fail_save)

    resp = client.get("/api/coach/statement/7/1")
    assert resp.status_code == 502
    body = json.loads(resp.data)
    assert body["error"] == "unavailable"
    # Regression: the raw exception text must never appear in the response.
    assert "secret.internal" not in resp.get_data(as_text=True)


def test_tip_returns_statement(client, monkeypatch):
    # The tip endpoint must pass the posted fen/move to the cached generator and
    # return its statement, so a hint gets an accompanying coaching remark.
    _configured(monkeypatch)
    seen = {}

    def fake_tip(config, fen, move, *, notation=None):
        seen["fen"] = fen
        seen["move"] = move
        seen["notation"] = notation
        return "Develops toward the center."

    monkeypatch.setattr(coach_tips, "get_tip_statement", fake_tip)

    resp = client.post(
        "/api/coach/tip",
        json={"fen": STARTPOS, "move": "e2e4"},
    )
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "statement": "Develops toward the center.",
        "error": None,
    }
    assert seen == {"fen": STARTPOS, "move": "e2e4", "notation": "san"}


def test_tip_not_configured(client, monkeypatch):
    # With no provider/key the tip must report not_configured up front, without
    # calling the generator.
    monkeypatch.setattr(webapp, "_read_coach_config", lambda: CoachConfig(provider="none"))

    def fail_tip(*a, **k):
        raise AssertionError("generator must not run when unconfigured")

    monkeypatch.setattr(coach_tips, "get_tip_statement", fail_tip)

    resp = client.post("/api/coach/tip", json={"fen": STARTPOS, "move": "e2e4"})
    assert resp.status_code == 200
    assert json.loads(resp.data)["error"] == "not_configured"


def test_tip_missing_body_is_bad_request(client):
    # A tip with no fen/move is a client error (400), guarding the generator
    # against empty input.
    resp = client.post("/api/coach/tip", json={"fen": "", "move": ""})
    assert resp.status_code == 400
    assert json.loads(resp.data)["error"] == "missing_fen_or_move"
