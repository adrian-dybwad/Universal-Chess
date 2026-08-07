"""Auth policy for the engine API: every state-changing endpoint needs a password.

Several engine endpoints mutated durable state with no authentication at all --
saving, deleting and resetting engine profiles, rewriting profile option case,
dismissing a recorded engine failure, and clearing persisted install state. Any
unauthenticated caller on the network could rewrite or destroy engine
configuration. Install/uninstall/upload were gated; these had simply been missed,
which is exactly the kind of gap a per-endpoint test never catches because the
test is only written for endpoints someone remembered to protect.

So this enumerates the routes rather than listing them: every POST under
``/api/engines`` must reject an unauthenticated request, and a new endpoint is
covered the moment it is added. Exceptions must be named in
``UNAUTHENTICATED_BY_DESIGN`` with a reason, which makes each one a deliberate,
reviewable decision instead of an oversight.
"""

import importlib
import re
import sys

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

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


# Endpoints that deliberately answer an unauthenticated POST, with the reason.
# Keep this as small as it can be: each entry is a hole in the policy above.
#
# Currently empty, and that is the goal state. The one entry it held,
# /api/engines/resume, was excused because the engine name came from the persisted
# install state rather than the request, so a caller could not choose what got
# built. Resume is now engine-scoped -- several installs can be paused at once, so
# the request has to say which one -- and that argument no longer holds, so the
# exemption was removed with it rather than reworded.
UNAUTHENTICATED_BY_DESIGN = set()


def _post_rules() -> list:
    """Every POST route under ``/api/engines``, as (rule string, concrete url)."""
    rules = []
    for rule in webapp.app.url_map.iter_rules():
        if "POST" not in (rule.methods or set()):
            continue
        if not str(rule.rule).startswith("/api/engines"):
            continue
        # Substitute any <converter:name> placeholder; the auth decorator runs
        # before the view, so the value never has to resolve to a real engine.
        rules.append((str(rule.rule), re.sub(r"<[^>]+>", "x", str(rule.rule))))
    return sorted(rules)


@pytest.fixture
def unauthenticated_client(monkeypatch):
    """A test client whose every request fails authentication."""
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    return webapp.app.test_client()


def test_there_are_engine_post_routes_to_check():
    """The enumeration must actually find routes.

    Why this test exists: the policy test below iterates a discovered list. If the
    prefix or method filter ever stops matching (a blueprint refactor, a URL
    prefix change), that test would iterate nothing and pass while enforcing
    nothing -- the worst kind of green.

    How a regression manifests: the route scan returns empty and this fails,
    rather than the policy silently ceasing to apply.
    """
    assert len(_post_rules()) >= 5, f"engine POST route scan looks wrong: {_post_rules()}"


def test_every_engine_post_endpoint_requires_authentication(unauthenticated_client):
    """Unauthenticated POSTs to engine endpoints must be rejected with 401.

    Why this test exists: this is the actual policy -- nothing that changes state
    through the web UI may be reachable without a password. It was violated by
    six endpoints (profile save/delete/reset, reconcile-case, failure dismiss,
    install cancel) that let an anonymous caller rewrite or delete engine
    configuration.

    How a regression manifests: adding an endpoint without ``@requires_auth``, or
    removing the decorator from an existing one, makes that route answer an
    anonymous POST; this fails naming the exact rule.
    """
    unprotected = []
    for rule, url in _post_rules():
        if rule in UNAUTHENTICATED_BY_DESIGN:
            continue
        response = unauthenticated_client.post(url)
        if response.status_code != 401:
            unprotected.append(f"{rule} -> {response.status_code}")

    assert not unprotected, (
        "engine endpoints answered an unauthenticated POST: " + ", ".join(unprotected)
    )


def test_documented_auth_exceptions_still_exist():
    """Every exception must name a real route.

    Why this test exists: an exception left behind after its endpoint is renamed
    or removed is a standing permission to skip auth on a path nobody reviews. It
    would also silently start excusing a *different* endpoint if a future route
    reused the path.

    How a regression manifests: adding an exemption for a route that does not
    exist, or leaving one behind after renaming its endpoint, fails here naming
    the stale path. Vacuously true while the exemption set is empty, which is the
    state to keep it in.
    """
    known = {rule for rule, _ in _post_rules()}
    stale = UNAUTHENTICATED_BY_DESIGN - known
    assert not stale, f"auth exceptions name routes that no longer exist: {sorted(stale)}"
