"""Shared loader for the Flask web app under test.

``universalchess.web.app`` does real work at import time: it binds a SQLAlchemy
engine (creating the schema) and loads sprite/resource images from disk. Importing
it in a test therefore needs the database redirected to memory and image loading
stubbed, or the import fails on a machine without the packaged install tree.

Every web test previously repeated that boilerplate. Centralising it here keeps
the setup identical across tests, so a change to the app's import-time
requirements is fixed in one place instead of in each test module.
"""

import importlib
import sys


def configure_for_testing(webapp):
    """Enable Flask testing mode on ``webapp``.

    Testing mode makes Flask propagate exceptions instead of converting them into
    500 responses, which is what lets a test see the real failure.

    Centralised so this rationale lives in one place, and so the accompanying
    semgrep suppression does too. ``avoid_hardcoded_config`` flags enabling TESTING
    because doing so in *production* config leaks tracebacks to clients; that does
    not apply to a pytest-only helper. Suppressing it here means the security rule
    stays active everywhere else instead of being disabled tree-wide, and a future
    test file inherits the suppression rather than tripping the CI gate.
    """
    webapp.app.config.update(TESTING=True)  # nosemgrep: python.flask.security.audit.hardcoded-config.avoid_hardcoded_config_TESTING


def make_test_client(webapp):
    """Return a Flask test client for ``webapp``, with testing mode enabled."""
    configure_for_testing(webapp)
    return webapp.app.test_client()


def load_webapp():
    """Import (or reload) ``universalchess.web.app`` with its import-time I/O stubbed.

    The module is reloaded when already present so a test module picks up a clean
    instance rather than one another test has monkeypatched.

    Returns:
        The imported ``universalchess.web.app`` module.
    """
    from PIL import Image

    import universalchess.db.uri as uri

    uri.get_database_uri = lambda: "sqlite:///:memory:"

    original_open = Image.open
    Image.open = lambda *args, **kwargs: Image.new("RGBA", (8, 8))
    try:
        if "universalchess.web.app" in sys.modules:
            return importlib.reload(sys.modules["universalchess.web.app"])
        import universalchess.web.app as webapp

        return webapp
    finally:
        Image.open = original_open
