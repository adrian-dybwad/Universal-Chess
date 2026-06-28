"""Tests for safe tar extraction in the engine manager.

Engine archives are downloaded from the project's GitHub releases and extracted
on the board. A tampered or malicious archive could otherwise carry members
with ``..`` segments, absolute paths, or escaping symlinks that write outside
the intended directory (path-traversal / "tarbomb", ruff S202). These tests
guard the helpers that enforce extraction stays inside the target directory.
"""

import io
import tarfile
from pathlib import Path

import pytest

from universalchess.managers.engine_manager import (
    _assert_tar_members_safe,
    _is_within_directory,
    _safe_extract_tar,
)


def _write_tar(path: Path, members: list[tuple[str, bytes]]) -> None:
    """Write a gzip tar at ``path`` from ``(name, content)`` regular-file pairs."""
    with tarfile.open(path, "w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))


def _write_symlink_tar(path: Path, link_name: str, link_target: str) -> None:
    """Write a gzip tar at ``path`` containing a single symlink member."""
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(link_name)
        info.type = tarfile.SYMTYPE
        info.linkname = link_target
        tar.addfile(info)


@pytest.mark.parametrize(
    ("rel_target", "expected_inside"),
    [
        # Regression: a normal nested path must be accepted, otherwise valid
        # archives would be rejected and engine installs would break.
        ("arch/engine", True),
        # The destination itself is "inside" (boundary case): rejecting it would
        # make extracting the root entry fail.
        (".", True),
        # Path-traversal: a `..` segment escaping the root must be rejected;
        # if accepted, a member could overwrite files outside the target dir.
        ("../escape.txt", False),
        ("arch/../../escape.txt", False),
    ],
)
def test_is_within_directory(tmp_path: Path, rel_target: str, expected_inside: bool):
    # Why: _is_within_directory is the containment primitive the manual fallback
    # relies on; if it mis-classifies a `..` escape as inside, traversal guards
    # silently fail. Manifests here as a wrong boolean for the escape cases.
    dest = tmp_path / "dest"
    dest.mkdir()
    target = (dest / rel_target).resolve()
    assert _is_within_directory(dest, target) is expected_inside


def test_is_within_directory_rejects_absolute_outside(tmp_path: Path):
    # Why: an absolute member name (e.g. "/etc/passwd") resolves outside the
    # destination; this must be classified as not-inside or such a member could
    # clobber arbitrary absolute paths. Fails (returns True) if absolute paths
    # are not handled.
    dest = tmp_path / "dest"
    dest.mkdir()
    assert _is_within_directory(dest, Path("/etc/passwd")) is False


def test_assert_members_safe_accepts_benign_archive(tmp_path: Path):
    # Why: guards must not reject legitimate archives. If this raises, every
    # prebuilt engine install would fail. Manifests as an unexpected TarError.
    archive = tmp_path / "benign.tar.gz"
    _write_tar(archive, [("arch/engine", b"binary"), ("arch/readme.txt", b"hi")])
    with tarfile.open(archive, "r:gz") as tar:
        _assert_tar_members_safe(tar, tmp_path / "dest")  # must not raise


@pytest.mark.parametrize("evil_name", ["../escape.txt", "arch/../../escape.txt"])
def test_assert_members_safe_rejects_traversal(tmp_path: Path, evil_name: str):
    # Why: this is the core path-traversal regression guard for the manual
    # (old-Python) fallback. If member validation regresses, this raises nothing
    # and the escaping member would later be written outside dest.
    archive = tmp_path / "evil.tar.gz"
    _write_tar(archive, [(evil_name, b"pwned")])
    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(tarfile.TarError):
            _assert_tar_members_safe(tar, tmp_path / "dest")


def test_assert_members_safe_rejects_escaping_symlink(tmp_path: Path):
    # Why: the 'data' filter also blocks symlinks whose target escapes the tree;
    # the manual fallback must match. A regression here would allow a link that
    # later redirects writes outside dest. Manifests as no TarError raised.
    archive = tmp_path / "link.tar.gz"
    _write_symlink_tar(archive, "arch/link", "../../../../etc/passwd")
    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(tarfile.TarError):
            _assert_tar_members_safe(tar, tmp_path / "dest")


def test_safe_extract_benign_archive_lands_files(tmp_path: Path):
    # Why: end-to-end happy path -- a normal archive must extract its files at
    # the expected relative locations. Fails if extraction is broken or files
    # land in the wrong place (count/content assertions catch both).
    archive = tmp_path / "benign.tar.gz"
    _write_tar(archive, [("arch/engine", b"ENGINE"), ("arch/data/net.nnue", b"NET")])
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        _safe_extract_tar(tar, dest)

    assert (dest / "arch" / "engine").read_bytes() == b"ENGINE"
    assert (dest / "arch" / "data" / "net.nnue").read_bytes() == b"NET"
    # No stray files escaped into the parent.
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_traversal_archive_raises_and_writes_nothing_outside(tmp_path: Path):
    # Why: the headline S202 guard. A malicious `..` member must (1) raise
    # TarError and (2) never create a file outside dest. If traversal protection
    # regresses, escape.txt appears in tmp_path (outside dest) -- the assertion
    # that it does NOT exist is what fails.
    archive = tmp_path / "evil.tar.gz"
    _write_tar(archive, [("../escape.txt", b"pwned")])
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(tarfile.TarError):
            _safe_extract_tar(tar, dest)

    assert not (tmp_path / "escape.txt").exists()
