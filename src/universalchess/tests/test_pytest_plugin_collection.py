"""Player-plugin tests must be collected with the app tests.

Why these tests exist
---------------------
A player plugin ships its tests in ``players/<name>/tests/``. Pytest, CI, tox,
and the release script used to pass only ``src/universalchess/tests/``, so those
files never ran. A green suite then meant the app tests passed, not that the
plugin tests passed.

How the regression manifests
----------------------------
``testpaths`` or a hardcoded pytest path omits ``src/universalchess/players``.
``pytest --collect-only`` drops ``players/lichess/tests``, and a plugin
regression is invisible.
"""

from pathlib import Path

import universalchess

PACKAGE_ROOT = Path(universalchess.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent


def test_pytest_testpaths_include_player_plugins():
    """Default collection must include the player-plugin tree.

    Failure: pyproject lists only ``src/universalchess/tests``, so ``./bin/pytest``
    with no path never sees ``players/*/tests``.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text()
    options = text.split("[tool.pytest.ini_options]", 1)
    assert len(options) == 2, (
        "pyproject.toml must declare [tool.pytest.ini_options] so pytest has a "
        "default collection that includes player plugins"
    )
    block = options[1].split("\n[", 1)[0]
    assert "src/universalchess/tests" in block
    assert "src/universalchess/players" in block


def test_ci_pytest_does_not_restrict_to_the_central_tree():
    """CI must use pytest's configured testpaths, not tests/ alone.

    Failure: the workflow still passes ``src/universalchess/tests/``, so GitHub
    Actions never runs player-plugin tests.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "pytest src/universalchess/tests" not in workflow, (
        "CI must not pass src/universalchess/tests as the pytest path; that "
        "overrides testpaths and skips players/*/tests"
    )
    assert "pytest" in workflow


def test_tox_default_does_not_restrict_to_the_central_tree():
    """tox with no posargs must use the same testpaths as CI.

    Failure: ``pytest {posargs:src/universalchess/tests}`` makes a bare tox run
    skip player-plugin tests.
    """
    tox = (REPO_ROOT / "tox.ini").read_text()
    assert "posargs:src/universalchess/tests" not in tox, (
        "tox's default posargs must not be src/universalchess/tests; leave "
        "posargs empty so pytest uses testpaths"
    )


def test_release_script_does_not_restrict_to_the_central_tree():
    """The release test step must use the same testpaths as CI.

    Failure: release.sh still passes ``src/universalchess/tests/`` to pytest,
    so a release can ship with unrun plugin tests.
    """
    script = (REPO_ROOT / "scripts" / "release.sh").read_text()
    assert "src/universalchess/tests/" not in script, (
        "release.sh must not pass src/universalchess/tests/ to pytest; that "
        "overrides testpaths and skips players/*/tests"
    )
