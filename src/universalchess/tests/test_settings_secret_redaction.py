"""Tests that GET /api/settings never returns a stored credential.

Root cause these guard
----------------------
``GET /api/settings`` is deliberately unauthenticated (the React app reads it to
render the Settings page before any login). ``get_all_settings`` builds its reply
by copying *every* section of centaur.ini verbatim, so any section holding a
secret leaks unless it is explicitly redacted. Coach API keys and the newer
``account:<type>:<id>`` sections were redacted; the legacy ``[lichess]`` section
was not, so a board whose token still lived there served it in cleartext to any
LAN client (``curl -k https://board/api/settings | jq .lichess.api_token``).

The legacy section is still live: ``board/centaur.py`` reads/writes it, the web
save path writes it directly, and ``account_store.migrate_legacy_lichess`` is a
documented no-op when the Lichess identity cannot be resolved (offline board, no
cached username) -- so the token can persist there indefinitely.

Redaction alone would introduce a worse bug: the Settings page loads
``lichess.api_token`` into its form and posts it back on every save, so a blanked
GET plus an unguarded save would *erase* the user's token. These tests therefore
pin both halves -- the secret never leaves, and a blank value on save means
"leave unchanged" -- because either half alone is broken.
"""

import configparser

import pytest

from universalchess.tests.webapp_fixture import load_webapp

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")


webapp = load_webapp()

# A token shaped like a real Lichess personal access token, distinctive enough
# that an assertion against the whole serialized response cannot pass by accident.
TOKEN = "lip_R3alL00kingT0ken123"


@pytest.fixture
def config_files(tmp_path, monkeypatch):
    """Point Settings at a writable temp centaur.ini plus an empty defaults file.

    get_all_settings merges the packaged defaults over the live config, so the
    defaults file must exist (and stay empty) for the assertions to describe only
    what the test seeded.
    """
    from universalchess.board.settings import Settings

    cfg = tmp_path / "centaur.ini"
    defcfg = tmp_path / "defaults.ini"
    defcfg.write_text("")
    monkeypatch.setattr(Settings, "configfile", str(cfg))
    monkeypatch.setattr(Settings, "defconfigfile", str(defcfg))
    return cfg


def _seed(cfg, section, values):
    parser = configparser.ConfigParser()
    parser.read(str(cfg))
    if not parser.has_section(section):
        parser.add_section(section)
    for key, value in values.items():
        parser.set(section, key, value)
    with open(cfg, "w", encoding="utf-8") as handle:
        parser.write(handle)


def _read(cfg, section, key):
    parser = configparser.ConfigParser()
    parser.read(str(cfg))
    return parser.get(section, key, fallback=None)


class TestLegacyLichessRedaction:
    """The legacy [lichess] api_token must never appear in a settings read."""

    def test_token_is_not_returned(self, config_files):
        """The stored token must be replaced by an empty value.

        How the regression manifests: reverting the redaction puts the cleartext
        token back in the ``lichess`` section of the response, so this assertion
        finds the token string and fails. This is the exact LAN disclosure the
        audit reproduced.
        """
        _seed(config_files, "lichess", {"api_token": TOKEN, "username": "alice"})

        result = webapp.get_all_settings()

        assert result["lichess"]["api_token"] == ""

    def test_token_is_absent_from_the_entire_payload(self, config_files):
        """No other key may carry the secret.

        Asserting only on ``lichess.api_token`` would miss a copy surfacing
        elsewhere (a merged default, a mirrored account section, a debug field).
        Serializing the whole reply catches a leak through any path.
        """
        import json

        _seed(config_files, "lichess", {"api_token": TOKEN, "username": "alice"})

        blob = json.dumps(webapp.get_all_settings())

        assert TOKEN not in blob

    def test_set_companion_reports_presence(self, config_files):
        """A boolean companion must tell the UI a token is stored.

        Without it the Settings page cannot distinguish "no token" from "token
        hidden" and would show an empty field as if Lichess were unconfigured.
        Mirrors the coach-key ``<key>_set`` convention.
        """
        _seed(config_files, "lichess", {"api_token": TOKEN})

        assert webapp.get_all_settings()["lichess"]["api_token_set"] is True

    def test_set_companion_is_false_when_no_token_stored(self, config_files):
        """The empty case must report False, not merely omit the flag.

        How the regression manifests: a truthy-by-default companion would make a
        fresh board claim a token is configured, hiding the field the user needs.
        """
        _seed(config_files, "lichess", {"api_token": "", "range": "0-3000"})

        result = webapp.get_all_settings()

        assert result["lichess"]["api_token_set"] is False
        assert result["lichess"]["api_token"] == ""

    def test_non_secret_lichess_keys_still_pass_through(self, config_files):
        """Redaction must be surgical: only the token is hidden.

        How the regression manifests: over-broad redaction (blanking the whole
        section) would wipe the rating range and cached username from the UI,
        silently resetting the user's Lichess pairing preferences.
        """
        _seed(
            config_files,
            "lichess",
            {"api_token": TOKEN, "range": "1200-1800", "username": "alice"},
        )

        result = webapp.get_all_settings()

        assert result["lichess"]["range"] == "1200-1800"
        assert result["lichess"]["username"] == "alice"


class TestLichessSaveKeepsStoredToken:
    """A blank api_token on save means "leave unchanged", never "erase"."""

    def test_blank_token_does_not_erase_stored_token(self, config_files):
        """Saving the redacted (blank) field must preserve the stored token.

        This is the direct consequence of redaction: Settings.tsx loads
        ``lichess.api_token`` into its form and posts it back on every save, so
        after redaction it posts "". How the regression manifests: without this
        guard the first unrelated settings save (changing the rating range, a
        sound toggle) wipes the user's Lichess token and online play stops
        working -- a worse bug than the leak.
        """
        _seed(config_files, "lichess", {"api_token": TOKEN})

        webapp.save_all_settings({"lichess": {"api_token": "", "range": "0-2000"}},
                                 broadcast=False)

        assert _read(config_files, "lichess", "api_token") == TOKEN
        assert _read(config_files, "lichess", "range") == "0-2000"

    def test_new_token_replaces_stored_token(self, config_files):
        """A non-empty token must still be written.

        How the regression manifests: an over-eager "blank means unchanged" rule
        that dropped every token write would make the field read-only, so a user
        could never set or rotate their token.
        """
        _seed(config_files, "lichess", {"api_token": TOKEN})

        webapp.save_all_settings({"lichess": {"api_token": "lip_brandNewToken"}},
                                 broadcast=False)

        assert _read(config_files, "lichess", "api_token") == "lip_brandNewToken"

    def test_set_companion_is_never_persisted(self, config_files):
        """The UI-only ``api_token_set`` flag must not be written to the ini.

        How the regression manifests: echoing the GET payload back would persist
        ``api_token_set = True`` as a settings key, which then merges into future
        reads as real config and confuses the token-presence logic.
        """
        _seed(config_files, "lichess", {"api_token": TOKEN})

        webapp.save_all_settings(
            {"lichess": {"api_token": "", "api_token_set": True, "range": "0-1000"}},
            broadcast=False,
        )

        assert _read(config_files, "lichess", "api_token_set") is None

    def test_coach_key_redaction_still_works(self, config_files):
        """The pre-existing coach-key redaction must not regress.

        Included because this change edits the same function; a refactor that
        reorders or replaces the redaction block could drop the coach case while
        adding the lichess one. Guards the control that already worked.
        """
        import json

        _seed(config_files, "game", {"coach_api_key": "sk-secret-coach-key"})

        result = webapp.get_all_settings()

        assert result["game"]["coach_api_key"] == ""
        assert result["game"]["coach_api_key_set"] is True
        assert "sk-secret-coach-key" not in json.dumps(result)
