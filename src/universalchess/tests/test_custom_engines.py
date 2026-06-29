"""Unit tests for the custom-engine support modules.

Background / why these tests exist
----------------------------------
Two web features let an operator add their own UCI engine: uploading a binary
and downloading one from a custom URL. Both must not become a foothold for
arbitrary code or SSRF, and both must refuse a binary that cannot run on the
board. These tests pin the pure building blocks that enforce those guarantees:

* ``validate_engine_id`` / ``validate_display_name`` -- safe ids (no traversal,
  no catalog collision) and sane names.
* ``detect_elf_arch`` / ``validate_binary_arch`` -- accept only ARM ELF binaries
  matching the device architecture (the operator chose "reject mismatches").
* ``validate_download_url`` -- HTTPS only, and never a private/loopback/
  link-local address (SSRF guard).
* ``locate_engine_binary_in_dir`` / ``install_binary_payload`` -- place exactly
  one validated binary from a raw file or a .tar.gz, reusing the project's
  path-traversal-safe tar extractor.
* ``CustomEngineRegistry`` -- durable JSON record of which custom engines exist.

The functions are pure / filesystem-scoped so they are tested directly with
fabricated ELF headers and temp dirs, without touching /opt or the network.
"""

import gzip
import io
import os
import tarfile
from pathlib import Path

import pytest

from universalchess.services import custom_engines as ce
from universalchess.services.custom_engine_registry import (
    CustomEngine,
    CustomEngineRegistry,
)
from universalchess.managers.engine_manager import _safe_extract_tar


# Real ENGINES ids collide with these; a couple are referenced explicitly.
BUILTIN_IDS = {"stockfish", "berserk", "rodentIV"}


def _elf_header(arch: str = "arm64") -> bytes:
    """Return a minimal little-endian ELF header for the given ARM arch.

    Only the fields ``detect_elf_arch`` reads are meaningful: the magic, the
    EI_CLASS/EI_DATA bytes, and ``e_machine`` at offset 18. The rest is padding
    so the buffer is long enough to look like a real header.
    """
    machine = {"arm64": 183, "armhf": 40}[arch]
    ei_class = 2 if arch == "arm64" else 1
    buf = bytearray(64)
    buf[0:4] = b"\x7fELF"
    buf[4] = ei_class
    buf[5] = 1  # EI_DATA = little-endian
    buf[18:20] = machine.to_bytes(2, "little")
    return bytes(buf)


def _write_binary(path: Path, arch: str = "arm64") -> Path:
    path.write_bytes(_elf_header(arch) + b"\x00" * 32)
    return path


def _make_targz(path: Path, members: dict) -> Path:
    """Write a .tar.gz at ``path`` containing {arcname: bytes}."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for arcname, data in members.items():
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(gzip.compress(raw.getvalue()))
    return path


# ---------------------------------------------------------------------------
# Engine id validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "engine_id",
    ["myengine", "my-engine", "my_engine", "e1", "a" * 32],
)
def test_validate_engine_id_accepts_safe_ids(engine_id):
    """Lowercase alnum/dash/underscore ids within length are accepted.

    Why: these ids become a filename under the engines dir; the allowed set is
    deliberately conservative. Manifestation if the regex tightened wrongly: a
    legitimate id would be rejected and uploads would fail.
    """
    assert ce.validate_engine_id(engine_id, builtin_ids=BUILTIN_IDS, existing_ids=set()) is None


@pytest.mark.parametrize(
    "engine_id",
    [
        "",                # empty
        "Upper",           # uppercase (filesystem case pitfalls)
        "../evil",         # path traversal
        "a/b",             # separator
        "a.b",             # dot (could mask extensions)
        "-leading",        # leading dash (could read as a CLI flag downstream)
        " spaced ",        # whitespace
        "a" * 33,          # too long
        "x\x00y",          # NUL injection
    ],
)
def test_validate_engine_id_rejects_unsafe_ids(engine_id):
    """Unsafe ids (traversal, separators, case, length, control chars) are rejected.

    Why: an id like ``../evil`` would escape the engines directory; uppercase or
    dotted ids invite filesystem/extension confusion. Manifestation if a guard
    is dropped: the offending id returns None (accepted) and a later file write
    lands outside the intended directory or collides unexpectedly.
    """
    assert ce.validate_engine_id(engine_id, builtin_ids=BUILTIN_IDS, existing_ids=set()) is not None


def test_validate_engine_id_rejects_builtin_collision():
    """An id matching a catalog engine is rejected (cannot shadow built-ins).

    Why: reusing 'stockfish' would let a custom upload masquerade as / clobber a
    catalog engine. Manifestation if dropped: the custom binary overwrites the
    catalog engine's slot.
    """
    assert ce.validate_engine_id("stockfish", builtin_ids=BUILTIN_IDS, existing_ids=set()) is not None


def test_validate_engine_id_rejects_existing_custom_collision():
    """An id already used by another custom engine is rejected.

    Why: two custom engines sharing an id would fight over the same file and
    registry slot. Manifestation if dropped: the second add silently replaces
    the first's binary.
    """
    assert ce.validate_engine_id("mine", builtin_ids=BUILTIN_IDS, existing_ids={"mine"}) is not None


# ---------------------------------------------------------------------------
# Display name validation
# ---------------------------------------------------------------------------


def test_validate_display_name_accepts_reasonable_name():
    """A normal human name is accepted.

    Manifestation if over-tightened: legitimate names like 'My Engine 1' fail.
    """
    assert ce.validate_display_name("My Engine 1") is None


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, "bad\nname", "ctrl\x07"])
def test_validate_display_name_rejects_bad_names(name):
    """Empty/whitespace-only/over-long/control-char names are rejected.

    Why: the name is rendered in the UI and stored; control characters and empty
    values are not meaningful labels. Manifestation if dropped: a blank or
    control-laden label is persisted and shown.
    """
    assert ce.validate_display_name(name) is not None


# ---------------------------------------------------------------------------
# ELF architecture detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arch", ["arm64", "armhf"])
def test_detect_elf_arch_reads_supported_arm_machines(arch):
    """A valid ARM ELF header reports its architecture.

    Why: the device-match check depends on reading e_machine correctly.
    Manifestation if offsets/endianness are wrong: a valid binary is read as the
    wrong arch (or None) and a runnable engine is rejected.
    """
    assert ce.detect_elf_arch(_elf_header(arch)) == arch


@pytest.mark.parametrize(
    "data",
    [
        b"",                       # empty
        b"not an elf file at all", # no magic
        b"\x7fELF" + b"\x00" * 4,  # magic only, too short to read e_machine
    ],
)
def test_detect_elf_arch_returns_none_for_non_elf(data):
    """Non-ELF or truncated input yields None (not a fabricated arch).

    Why: a raw text/HTML body (e.g. an error page mistaken for a binary) must not
    be classified as a runnable engine. Manifestation if dropped: garbage is
    accepted as a valid binary.
    """
    assert ce.detect_elf_arch(data) is None


def test_detect_elf_arch_returns_none_for_unrelated_machine():
    """An ELF for a non-ARM machine (x86-64) yields None.

    Why: the board is ARM-only; an x86 ELF must be refused. Manifestation if
    dropped: an x86 binary is accepted and fails to exec at runtime.
    """
    buf = bytearray(_elf_header("arm64"))
    buf[18:20] = (62).to_bytes(2, "little")  # EM_X86_64
    assert ce.detect_elf_arch(bytes(buf)) is None


def test_validate_binary_arch_accepts_match(tmp_path):
    """A binary whose arch matches the device passes validation (returns None)."""
    binary = _write_binary(tmp_path / "engine", "arm64")
    assert ce.validate_binary_arch(str(binary), "arm64") is None


def test_validate_binary_arch_rejects_mismatch(tmp_path):
    """A binary whose arch differs from the device is rejected with a message.

    Why: the operator chose to reject mismatches outright. Manifestation if
    dropped: an armhf binary is installed on an arm64 board (or vice versa) and
    crashes when launched.
    """
    binary = _write_binary(tmp_path / "engine", "armhf")
    err = ce.validate_binary_arch(str(binary), "arm64")
    assert err is not None
    assert "armhf" in err and "arm64" in err


def test_validate_binary_arch_rejects_non_elf(tmp_path):
    """A non-ELF file is rejected (e.g. a script or downloaded error page)."""
    bogus = tmp_path / "engine"
    bogus.write_bytes(b"#!/bin/sh\necho hi\n")
    assert ce.validate_binary_arch(str(bogus), "arm64") is not None


# ---------------------------------------------------------------------------
# Download URL validation (SSRF guard)
# ---------------------------------------------------------------------------


def _resolver_to(ip):
    """Fake getaddrinfo that resolves any host to a single fixed IP."""
    def _resolve(host, port, *args, **kwargs):
        family = 10 if ":" in ip else 2  # AF_INET6 / AF_INET
        return [(family, 1, 6, "", (ip, 0))]
    return _resolve


def test_validate_download_url_accepts_public_https():
    """A public HTTPS URL resolving to a public IP is accepted (returns None)."""
    assert ce.validate_download_url(
        "https://example.com/engine.tar.gz", resolver=_resolver_to("93.184.216.34")
    ) is None


@pytest.mark.parametrize("url", ["http://example.com/x", "ftp://example.com/x", "file:///etc/passwd", "https://"])
def test_validate_download_url_rejects_non_https_or_hostless(url):
    """Non-HTTPS schemes and host-less URLs are rejected.

    Why: only HTTPS is permitted (operator choice), and a URL with no host has
    nothing safe to fetch. Manifestation if dropped: a plain-http or file:// URL
    is fetched, exposing the board to MITM or local-file reads.
    """
    assert ce.validate_download_url(url, resolver=_resolver_to("93.184.216.34")) is not None


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",     # loopback
        "10.0.0.5",      # private
        "192.168.1.10",  # private
        "172.16.0.1",    # private
        "169.254.0.1",   # link-local
        "::1",           # loopback v6
        "fd00::1",       # unique-local v6
    ],
)
def test_validate_download_url_rejects_private_targets(ip):
    """An HTTPS URL resolving to a private/loopback/link-local IP is rejected.

    Why: this is the core SSRF guard -- a user-supplied URL must not be coerced
    into reaching the board's own services or the LAN. Manifestation if dropped:
    'https://anything' pointing (via DNS) at 127.0.0.1/10.x/192.168.x is fetched.
    """
    err = ce.validate_download_url("https://attacker.example/x", resolver=_resolver_to(ip))
    assert err is not None


# ---------------------------------------------------------------------------
# Locating / installing the binary payload
# ---------------------------------------------------------------------------


def test_locate_engine_binary_in_dir_finds_single(tmp_path):
    """Exactly one ARM ELF of the right arch is located among other files.

    Why: archives often carry a README/license beside the binary; the locator
    must pick the one runnable binary. Manifestation if it counted non-ELF files
    as candidates: it would report 'multiple' and fail a valid archive.
    """
    _write_binary(tmp_path / "engine", "arm64")
    (tmp_path / "README.txt").write_text("hello")
    found, err = ce.locate_engine_binary_in_dir(str(tmp_path), "arm64")
    assert err is None
    assert found is not None and Path(found).name == "engine"


def test_locate_engine_binary_in_dir_rejects_multiple(tmp_path):
    """Two ARM ELF binaries of the right arch are ambiguous and rejected.

    Why: with two engines we cannot know which to install; silently picking one
    would be wrong. Manifestation if dropped: an arbitrary binary is chosen.
    """
    _write_binary(tmp_path / "engineA", "arm64")
    _write_binary(tmp_path / "engineB", "arm64")
    found, err = ce.locate_engine_binary_in_dir(str(tmp_path), "arm64")
    assert found is None
    assert err is not None


def test_locate_engine_binary_in_dir_rejects_none(tmp_path):
    """An archive with no matching-arch ELF is rejected.

    Why: nothing runnable for this device. Manifestation if dropped: an empty or
    wrong-arch archive yields a 'success' with no usable binary.
    """
    _write_binary(tmp_path / "engine", "armhf")  # wrong arch for an arm64 device
    found, err = ce.locate_engine_binary_in_dir(str(tmp_path), "arm64")
    assert found is None
    assert err is not None


def test_install_binary_payload_raw(tmp_path):
    """A raw binary of the right arch is placed at dest and made executable.

    Asserts the full success shape: file exists at dest, is executable (0o755),
    and the bytes match the source. Manifestation if chmod is dropped: the
    engine is installed but not launchable.
    """
    src = _write_binary(tmp_path / "upload.bin", "arm64")
    dest = tmp_path / "engines" / "mine"
    dest.parent.mkdir()
    err = ce.install_binary_payload(
        source_path=str(src), is_archive=False, dest_path=str(dest),
        expected_arch="arm64", safe_extract=_safe_extract_tar,
    )
    assert err is None
    assert dest.exists()
    assert os.access(dest, os.X_OK)
    assert dest.read_bytes() == src.read_bytes()


def test_install_binary_payload_raw_wrong_arch_rejected(tmp_path):
    """A raw binary of the wrong arch is rejected and nothing is written.

    Manifestation if dropped: a non-runnable binary is installed at dest.
    """
    src = _write_binary(tmp_path / "upload.bin", "armhf")
    dest = tmp_path / "engines" / "mine"
    dest.parent.mkdir()
    err = ce.install_binary_payload(
        source_path=str(src), is_archive=False, dest_path=str(dest),
        expected_arch="arm64", safe_extract=_safe_extract_tar,
    )
    assert err is not None
    assert not dest.exists()


def test_install_binary_payload_targz_single_binary(tmp_path):
    """A .tar.gz containing one matching binary installs it at dest, executable.

    Exercises the archive path end-to-end using the project's safe extractor.
    Manifestation if extraction/locate regressed: dest is missing or not exec.
    """
    archive = _make_targz(
        tmp_path / "engine.tar.gz",
        {"engine": _elf_header("arm64") + b"\x00" * 16, "README": b"docs"},
    )
    dest = tmp_path / "engines" / "mine"
    dest.parent.mkdir()
    err = ce.install_binary_payload(
        source_path=str(archive), is_archive=True, dest_path=str(dest),
        expected_arch="arm64", safe_extract=_safe_extract_tar,
    )
    assert err is None
    assert dest.exists() and os.access(dest, os.X_OK)


def test_install_binary_payload_targz_multiple_binaries_rejected(tmp_path):
    """A .tar.gz with two matching binaries is ambiguous and rejected.

    Manifestation if dropped: an arbitrary one of the two is installed.
    """
    archive = _make_targz(
        tmp_path / "engine.tar.gz",
        {"a": _elf_header("arm64"), "b": _elf_header("arm64")},
    )
    dest = tmp_path / "engines" / "mine"
    dest.parent.mkdir()
    err = ce.install_binary_payload(
        source_path=str(archive), is_archive=True, dest_path=str(dest),
        expected_arch="arm64", safe_extract=_safe_extract_tar,
    )
    assert err is not None
    assert not dest.exists()


# ---------------------------------------------------------------------------
# Custom engine registry persistence
# ---------------------------------------------------------------------------


def test_registry_add_get_list_roundtrip(tmp_path):
    """An added engine is retrievable, listed, and survives a reload from disk.

    Why: the registry is the source of truth for which custom engines exist; it
    must persist across process restarts. Manifestation if the write/read
    regressed: a reloaded registry would not contain the engine.
    """
    path = tmp_path / "custom_engines.json"
    reg = CustomEngineRegistry(path)
    reg.add(CustomEngine(id="mine", display_name="Mine", source="upload"))

    assert reg.exists("mine")
    got = reg.get("mine")
    assert got is not None and got.display_name == "Mine" and got.source == "upload"
    assert [e.id for e in reg.list()] == ["mine"]

    # A fresh instance over the same file sees the persisted entry.
    assert CustomEngineRegistry(path).get("mine").display_name == "Mine"


def test_registry_add_replaces_same_id(tmp_path):
    """Re-adding the same id replaces the entry rather than duplicating it.

    Manifestation if dropped: list() would return two entries with the same id.
    """
    reg = CustomEngineRegistry(tmp_path / "custom_engines.json")
    reg.add(CustomEngine(id="mine", display_name="Old", source="upload"))
    reg.add(CustomEngine(id="mine", display_name="New", source="url", url="https://x/y"))
    assert [e.id for e in reg.list()] == ["mine"]
    assert reg.get("mine").display_name == "New"
    assert reg.get("mine").url == "https://x/y"


def test_registry_remove(tmp_path):
    """Remove deletes the entry and reports whether something was removed.

    Manifestation if dropped: a removed engine lingers in list()/exists().
    """
    reg = CustomEngineRegistry(tmp_path / "custom_engines.json")
    reg.add(CustomEngine(id="mine", display_name="Mine", source="upload"))
    assert reg.remove("mine") is True
    assert not reg.exists("mine")
    # Removing a non-existent id reports False rather than raising.
    assert reg.remove("nope") is False


def test_registry_tolerates_missing_and_corrupt_file(tmp_path):
    """A missing or corrupt JSON file loads as an empty registry, not a crash.

    Why: a partially written or hand-edited file must not break engine listing.
    Manifestation if dropped: constructing the registry raises and the engines
    page fails to load.
    """
    missing = CustomEngineRegistry(tmp_path / "does_not_exist.json")
    assert missing.list() == []

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json")
    assert CustomEngineRegistry(corrupt).list() == []
