# Path resolution tests
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Tests for paths.get_engine_path, the single name->path choke point that maps
# an (untrusted) engine id to an executable under a trusted directory.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import os

import pytest

from universalchess import paths


@pytest.fixture
def engines_dir(tmp_path, monkeypatch):
    """Point paths.ENGINES_DIR at a temp directory with one real engine file.

    The dev fallback directory (relative to paths.py) does not exist in the
    checkout, so os.listdir raises and get_engine_path falls through to "".
    """
    d = tmp_path / "engines"
    d.mkdir()
    (d / "stockfish").write_text("binary")
    monkeypatch.setattr(paths, "ENGINES_DIR", str(d))
    return d


def test_returns_path_for_present_engine(engines_dir):
    """A name that matches a real entry resolves to its path under the base.

    Why: this is the happy path every engine consumer depends on.
    Regression manifestation: if enumeration matched the wrong entry (or none),
    the return would be "" and every engine would report as missing.
    """
    assert paths.get_engine_path("stockfish") == str(engines_dir / "stockfish")


def test_returns_empty_for_absent_engine(engines_dir):
    """An unknown name resolves to "" (not a fabricated path).

    Why: callers treat "" as "not installed"; a non-empty path for a missing
    engine would make the registry try to launch a nonexistent binary.
    Regression manifestation: returning os.path.join(base, name) unconditionally
    would yield a path that does not exist.
    """
    assert paths.get_engine_path("does-not-exist") == ""


@pytest.mark.parametrize(
    "evil",
    ["../../etc/passwd", "../stockfish", "/etc/passwd", "sub/../../escape"],
)
def test_rejects_traversal_names(engines_dir, evil):
    """Traversal/absolute names never match a top-level entry, so return "".

    Why this test exists: engine_name is request-derived (a selected/custom id);
    it must not be usable to reach files outside the engines directory.
    Regression manifestation: if the untrusted name were joined into the path
    instead of matched against os.listdir entries, "../../etc/passwd" would
    resolve outside the base and get_engine_path would hand it to the registry.
    """
    assert paths.get_engine_path(evil) == ""


def test_resolves_leaf_symlink_pointing_outside_base(tmp_path, monkeypatch):
    """A system engine symlinked into the engines dir is resolved, not rejected.

    Why this test exists: engine_manager._install_system_package installs system
    engines as symlinks in the engines dir pointing to e.g. /usr/games/stockfish.
    Regression manifestation: a realpath-based containment guard would follow the
    link out of the base and report the engine as missing (return "").
    """
    base = tmp_path / "engines"
    base.mkdir()
    outside = tmp_path / "system" / "stockfish"
    outside.parent.mkdir()
    outside.write_text("binary")
    (base / "stockfish").symlink_to(outside)
    monkeypatch.setattr(paths, "ENGINES_DIR", str(base))

    # os.path.exists follows the link, so the in-base symlink path is returned.
    assert paths.get_engine_path("stockfish") == str(base / "stockfish")


@pytest.mark.parametrize("bad", [None, ""])
def test_empty_name_returns_empty(engines_dir, bad):
    """A missing/empty name returns "" rather than the base directory itself.

    Why: os.path.join(base, "") is the base dir; treating that as an engine
    would make the registry try to exec a directory.
    Regression manifestation: without the empty guard, an empty name could match
    nothing yet a naive join would surface the base dir.
    """
    assert paths.get_engine_path(bad) == ""


def test_custom_script_engine_resolves_binary_inside_subdirectory(tmp_path, monkeypatch):
    """Maia resolves to engines/maia/lc0, not the engines/maia directory.

    Why this test exists: Maia (a custom-script engine, repo_url=None) installs
    its executable inside a subdirectory named after the engine
    (engines/maia/lc0 + weights), unlike single-file engines. get_engine_path is
    the shared choke point every consumer uses -- profile probing, game launch,
    analysis, the centaur proxy -- so returning the directory instead of the
    binary makes all of them fail (the reported "Maia is not installed" in the
    profile editor is one symptom).

    How the regression manifests: the pre-fix code matches the top-level entry
    'maia' (a directory) and returns it; launching a directory as a UCI engine
    fails. The assertion below returns the directory path (missing the '/lc0'
    leaf) when that regression is present.
    """
    base = tmp_path / "engines"
    (base / "maia").mkdir(parents=True)
    (base / "maia" / "lc0").write_text("binary")
    monkeypatch.setattr(paths, "ENGINES_DIR", str(base))

    assert paths.get_engine_path("maia") == str(base / "maia" / "lc0")


def test_custom_script_engine_missing_binary_reports_not_installed(tmp_path, monkeypatch):
    """An engines/maia directory without lc0 inside resolves to "" (not installed).

    Why this test exists: matching the top-level directory is not proof the
    binary is present. An incomplete/interrupted install can leave the directory
    without the executable; callers treat "" as "not installed", so a partial
    install must not masquerade as a working engine.

    How the regression manifests: the pre-fix code returns the existing directory
    path (non-empty) even though no executable is inside, so the engine would be
    reported installed and then fail to launch. The assertion expects "".
    """
    base = tmp_path / "engines"
    (base / "maia").mkdir(parents=True)  # directory present, but no lc0 inside
    monkeypatch.setattr(paths, "ENGINES_DIR", str(base))

    assert paths.get_engine_path("maia") == ""
