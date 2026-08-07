"""Tests for the web AI-coach endpoints.

Root cause these guard
----------------------
The web coach endpoints were unauthenticated *and* able to trigger billed AI
generation. ``GET /api/coach/statement`` generated-on-miss and ``POST
/api/coach/tip`` generated on every call, so any device on the LAN could drain the
owner's AI provider quota by walking plies or posting positions -- no credentials
needed.

The web surface is now strictly a **reader**: it displays coach statements the
board produced and stored, and never initiates generation. The tip endpoint is
gone (it existed only to generate; the board's hint flow still uses
``coach_tips.get_tip_statement`` directly). ``/api/coach/models`` contacts the
configured provider to enumerate models, so it now requires authentication.

These tests pin that no request path can reach a generator, because the failure
mode is financial and silent: a regression would restore generation and only show
up on the owner's provider bill.
"""

import json

import pytest

from universalchess.tests.webapp_fixture import load_webapp, make_test_client

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")


webapp = load_webapp()

from universalchess.managers.game import coach_persistence  # noqa: E402
from universalchess.services import coach as coach_service  # noqa: E402
from universalchess.services.coach import CoachConfig  # noqa: E402

STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return make_test_client(webapp)


@pytest.fixture
def no_generation(monkeypatch):
    """Make any attempt to generate a coach statement fail the test loudly.

    Installed on every statement test rather than only the "absent" case: the
    whole point of the read-only contract is that NO path reaches a generator, so
    an accidental generation anywhere must surface as a failure, not as a passing
    test that quietly bills the provider.
    """

    def fail(*args, **kwargs):
        raise AssertionError("the web coach must never generate; it only reads stored text")

    monkeypatch.setattr(coach_service, "generate_coach_statement", fail)
    return fail


def _configured(monkeypatch):
    """Point the endpoint's config reader at a fully configured provider.

    Used to prove the endpoint still refuses to generate even when it *could*
    (a key is present) -- otherwise a test could pass merely because no provider
    was set up.
    """
    monkeypatch.setattr(
        webapp,
        "_read_coach_config",
        lambda: CoachConfig(provider="openai", api_key="k", model="gpt-4o-mini"),
    )


class TestStatementIsReadOnly:
    """GET /api/coach/statement returns stored text and never generates."""

    def test_returns_stored_statement(self, client, monkeypatch, no_generation):
        """A statement the board stored must be returned and marked cached.

        How the regression manifests: if the read path breaks, the panel shows
        "pending" forever even for moves the board already coached.
        """
        monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: "Stored.")

        resp = client.get("/api/coach/statement/7/3")

        assert resp.status_code == 200
        assert json.loads(resp.data) == {"statement": "Stored.", "cached": True, "error": None}

    def test_absent_statement_does_not_generate(self, client, monkeypatch, no_generation):
        """A missing statement must report not_generated, not produce one.

        This is the core quota-drain fix. How the regression manifests: restoring
        generate-on-miss makes the ``no_generation`` fixture raise, because the
        endpoint reached the provider for a move the board never coached.
        """
        _configured(monkeypatch)
        monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: None)

        resp = client.get("/api/coach/statement/7/1")

        assert resp.status_code == 200
        assert json.loads(resp.data) == {
            "statement": None,
            "cached": False,
            "error": "not_generated",
        }

    def test_absent_statement_does_not_persist_anything(self, client, monkeypatch, no_generation):
        """A read miss must not write to the database.

        How the regression manifests: a reader that still calls the save helper
        would write empty/placeholder rows, poisoning the stored-statement cache
        so the board's later real statement is never adopted.
        """
        _configured(monkeypatch)
        monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: None)

        def fail_save(*args, **kwargs):
            raise AssertionError("a read-only endpoint must not persist")

        monkeypatch.setattr(coach_persistence, "save_coach_statement_if_absent", fail_save)

        assert client.get("/api/coach/statement/7/1").status_code == 200

    def test_unknown_ply_is_not_an_error(self, client, monkeypatch, no_generation):
        """An out-of-range ply reads as simply "not generated".

        A pure reader cannot tell an unplayed ply from an uncoached one, and the UI
        renders both the same ("pending"). How the regression manifests: 500ing or
        404ing here would break the live board, which polls the newest ply before
        the board has coached it.
        """
        _configured(monkeypatch)
        monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: None)

        resp = client.get("/api/coach/statement/7/9999")

        assert resp.status_code == 200
        assert json.loads(resp.data)["error"] == "not_generated"

    def test_not_configured_is_reported(self, client, monkeypatch, no_generation):
        """With no provider set the endpoint must say not_configured.

        The panel hides itself on this token, so a board with no coach never nags.
        How the regression manifests: returning not_generated instead would leave
        an permanently-empty coach panel visible on every board without a coach.
        """
        monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: None)
        monkeypatch.setattr(webapp, "_read_coach_config", lambda: CoachConfig(provider="none"))

        resp = client.get("/api/coach/statement/7/1")

        assert resp.status_code == 200
        assert json.loads(resp.data)["error"] == "not_configured"

    def test_stored_statement_shown_even_when_unconfigured(self, client, monkeypatch, no_generation):
        """Text the board already stored must still display after the key is removed.

        Statements are historical records, not live provider calls. How the
        regression manifests: checking configuration before the stored read would
        blank the coach commentary on every past game once the user rotates or
        clears their API key.
        """
        monkeypatch.setattr(coach_persistence, "get_coach_statement", lambda g, p: "Old remark.")
        monkeypatch.setattr(webapp, "_read_coach_config", lambda: CoachConfig(provider="none"))

        resp = client.get("/api/coach/statement/7/3")

        assert json.loads(resp.data) == {
            "statement": "Old remark.",
            "cached": True,
            "error": None,
        }


class TestTipEndpointRemoved:
    """POST /api/coach/tip must no longer exist."""

    def test_tip_endpoint_is_gone(self, client):
        """The generating tip endpoint must not be routable.

        It was the only unauthenticated state-changing route in the app and its
        sole purpose was billed generation; the web UI never called it. How the
        regression manifests: reinstating it returns 200/400 instead of
        404/405, restoring an anonymous quota-drain vector.
        """
        resp = client.post("/api/coach/tip", json={"fen": STARTPOS, "move": "e2e4"})

        assert resp.status_code in (404, 405)

    def test_no_route_mentions_coach_tip(self):
        """No URL rule may expose a coach tip path under any method.

        Guards against the endpoint returning under a different method or alias,
        which a status-code-only check could miss.
        """
        rules = [str(rule) for rule in webapp.app.url_map.iter_rules()]

        assert not [rule for rule in rules if "coach/tip" in rule]


class TestCoachModelsRequiresAuth:
    """GET /api/coach/models contacts the provider, so it must be authenticated."""

    def test_requires_authentication(self, monkeypatch):
        """An unauthenticated model listing must be rejected.

        The endpoint calls the configured AI provider with the owner's key. How the
        regression manifests: dropping the decorator lets any LAN client probe the
        provider (and confirm a valid key) without credentials.
        """
        monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
        unauthed = make_test_client(webapp)

        assert unauthed.get("/api/coach/models").status_code == 401


class TestCoachRoster:
    """GET /api/coaches stays open: it lists local personas and calls no provider."""

    def test_lists_roster_and_resolved(self, client):
        """The coach card needs the roster, the selection, and what Auto resolved to.

        Kept unauthenticated because it reads only local catalog data. A regression
        would leave the selector empty or hide which coach Auto picked.
        """
        resp = client.get("/api/coaches")

        assert resp.status_code == 200
        body = json.loads(resp.data)
        ids = [c["id"] for c in body["coaches"]]
        assert ids == ["dave", "myron", "sofia", "viktor"]  # weakest-first
        assert body["selected"] == "auto"
        assert body["resolved"] is not None
        assert body["resolved"]["id"] in ids
