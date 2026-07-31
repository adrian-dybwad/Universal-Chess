"""Tests for the per-engine documentation link shown in the engine managers.

Why these tests exist
---------------------
An engine card offers to install a binary on the device, but names it with two
short strings; a user who has not heard of "Koivisto" has nowhere to read about it
before installing. Each catalog entry therefore resolves to a page describing the
engine, which the web Engines tab renders as a "learn more" link.

The URL is derived from the entry the installer already clones from
(``repo_url``), so a new catalog engine gets a link without a second field to
maintain and the link cannot drift from the code that is actually installed. Only
engines with no repository (a system package, a downloaded net-backed engine)
carry an explicit ``info_url``.

A regression here shows up as a card with no link at all, or as a link built from
a clone URL that the browser cannot open (the ``.git`` form, or an ``ssh://``/local
path that is not a web page).
"""

import pytest

from universalchess.managers.engine_manager import (
    ENGINES,
    EngineDefinition,
    documentation_url,
)

# The bundled novelty engines (Stockfish-driven Python wrappers in
# services.derived_engines) have no upstream project of their own, so they have no
# documentation page to link to; their cards carry a full description instead.
BUNDLED_ENGINE_NAMES = frozenset({"worstfish", "drawfish"})


def _engine(**overrides) -> EngineDefinition:
    """Build an EngineDefinition carrying only the fields under test.

    Everything else is inert: the link resolution reads just ``info_url`` and
    ``repo_url``, so the remaining install metadata is filled to construct.
    """
    fields = {
        "name": "x",
        "display_name": "X Engine",
        "summary": "",
        "description": "",
        "repo_url": None,
        "build_commands": [],
        "binary_path": "",
        "is_system_package": False,
        "package_name": None,
        "extra_files": [],
        "dependencies": [],
    }
    fields.update(overrides)
    return EngineDefinition(**fields)


def test_documentation_url_strips_the_git_suffix_from_the_clone_url():
    # The catalog stores clone URLs ("....git"), which GitHub serves as a git
    # endpoint rather than a project page. The link must be the browsable form.
    # Regression: passing the clone URL through gives a user a download prompt or
    # an error page instead of the project's README.
    engine = _engine(repo_url="https://github.com/jhonnold/berserk.git")
    assert documentation_url(engine) == "https://github.com/jhonnold/berserk"


def test_documentation_url_keeps_a_repo_url_that_has_no_git_suffix():
    # Not every catalog entry spells the clone URL with the suffix; such a URL is
    # already browsable and must be left alone (a blind 4-character trim would
    # corrupt it).
    engine = _engine(repo_url="https://github.com/owner/project")
    assert documentation_url(engine) == "https://github.com/owner/project"


def test_explicit_info_url_wins_over_the_repository():
    # An engine whose project page is not its repository (Stockfish's site) pins the
    # link explicitly; the derived value must not override it.
    engine = _engine(
        repo_url="https://github.com/official-stockfish/Stockfish.git",
        info_url="https://stockfishchess.org",
    )
    assert documentation_url(engine) == "https://stockfishchess.org"


def test_engine_with_neither_repository_nor_info_url_has_no_link():
    # The bundled novelty engines have no upstream page. Returning None (rather
    # than an invented URL) is what lets the UI omit the link instead of rendering
    # a dead one.
    assert documentation_url(_engine()) is None


@pytest.mark.parametrize(
    "repo_url",
    [
        "git@github.com:owner/project.git",  # ssh clone form: not a web page
        "ssh://git@example.test/project.git",
        "/srv/local/project.git",  # local path used for a private mirror
        "http://example.test/project.git",  # plain http: no transport security
    ],
)
def test_non_https_repository_yields_no_link(repo_url):
    # The value becomes an anchor href in the browser, so only an https web URL
    # qualifies. Regression: emitting an ssh or filesystem URL renders a link the
    # browser cannot follow, and accepting arbitrary schemes turns catalog data into
    # whatever the href allows.
    assert documentation_url(_engine(repo_url=repo_url)) is None


@pytest.mark.parametrize(
    "name",
    sorted(set(ENGINES) - BUNDLED_ENGINE_NAMES),
)
def test_every_installable_catalog_engine_resolves_to_an_https_page(name):
    # The point of the feature: every engine a user can install offers somewhere to
    # read about it. Parametrized over the live catalog so a newly added engine
    # without a repository (or with only a non-https one) fails here instead of
    # shipping a card with no link.
    url = documentation_url(ENGINES[name])
    assert url is not None, f"{name} has no documentation link"
    assert url.startswith("https://"), url
    assert not url.endswith(".git"), url


@pytest.mark.parametrize("name", sorted(BUNDLED_ENGINE_NAMES))
def test_bundled_novelty_engines_declare_no_documentation_link(name):
    # Pins the deliberate exception: these two are implemented in this project (a
    # wrapper around the installed Stockfish), so there is no upstream page to send
    # a user to. Should one ever gain a documentation page, this test is the
    # reminder that the exception list above must shrink with it.
    assert documentation_url(ENGINES[name]) is None
