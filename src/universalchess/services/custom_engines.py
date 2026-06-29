"""Validation and binary-placement helpers for custom (operator-added) engines.

Shared by the web upload and install-from-URL flows. These helpers enforce the
safety properties those features require:

* ``validate_engine_id`` / ``validate_display_name`` -- a filesystem-safe id that
  cannot traverse out of the engines directory or collide with the catalog, and
  a sane display label.
* ``detect_elf_arch`` / ``validate_binary_arch`` -- the board is ARM-only, so a
  binary must be an ARM ELF whose architecture matches the device; anything else
  is refused rather than installed and left to crash at launch.
* ``validate_download_url`` -- HTTPS only, and never a host that resolves to a
  private/loopback/link-local address (SSRF guard).
* ``locate_engine_binary_in_dir`` / ``install_binary_payload`` -- place exactly
  one validated binary from a raw file or a ``.tar.gz``.

The module is intentionally free of Flask and of ``engine_manager`` (the
path-traversal-safe tar extractor is injected) so every rule is unit-testable in
isolation.

Known limitation: ``validate_download_url`` resolves the host and rejects private
targets, but a subsequent download re-resolves DNS, leaving a narrow TOCTOU
rebinding window. This is acceptable for a LAN tool and is documented so a future
hardening (pinning the resolved IP for the fetch) is a deliberate follow-up.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Engine id: lowercase, starts with an alphanumeric, then alphanumerics / '_' /
# '-', at most 32 chars. It becomes a filename directly under the engines dir, so
# the set excludes separators, dots, uppercase, whitespace and control chars.
_ENGINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_MAX_DISPLAY_NAME = 60

# ELF ``e_machine`` values for the two ARM targets the board runs.
_EM_ARM = 40         # 0x28  -> 32-bit ARM (armhf)
_EM_AARCH64 = 183    # 0xB7  -> 64-bit ARM (arm64)
_ELF_ARCH_BY_MACHINE = {_EM_AARCH64: "arm64", _EM_ARM: "armhf"}

# Bytes read to classify a file: the ELF header fields used live in the first 20.
_ELF_HEADER_READ = 64


def validate_engine_id(engine_id, *, builtin_ids, existing_ids) -> Optional[str]:
    """Return an error string if ``engine_id`` is unsafe/colliding, else None.

    Rejects empty/non-string ids, anything outside the conservative id charset
    (which blocks ``..``, separators, dots, case and control characters), and
    collisions with a catalog engine or an already-registered custom engine.
    """
    if not engine_id or not isinstance(engine_id, str):
        return "Engine id is required."
    if not _ENGINE_ID_RE.match(engine_id):
        return (
            "Engine id must be 1-32 characters using lowercase letters, digits, "
            "'-' or '_', and start with a letter or digit."
        )
    if engine_id in builtin_ids:
        return f"'{engine_id}' is a built-in engine id; choose another."
    if engine_id in existing_ids:
        return f"A custom engine with id '{engine_id}' already exists."
    return None


def validate_display_name(name) -> Optional[str]:
    """Return an error string if ``name`` is empty/over-long/control-laden, else None."""
    if not name or not isinstance(name, str) or not name.strip():
        return "Display name is required."
    if len(name) > _MAX_DISPLAY_NAME:
        return f"Display name must be at most {_MAX_DISPLAY_NAME} characters."
    if any(ord(c) < 32 for c in name):
        return "Display name contains invalid control characters."
    return None


def detect_elf_arch(header: bytes) -> Optional[str]:
    """Classify an ELF header as 'arm64' / 'armhf', or None if not a known ARM ELF.

    Returns None for non-ELF, truncated, or non-ARM (e.g. x86) inputs so a
    downloaded error page or an x86 binary is never treated as runnable.
    """
    if not header or len(header) < 20 or header[:4] != b"\x7fELF":
        return None
    ei_data = header[5]
    if ei_data == 1:
        endian = "little"
    elif ei_data == 2:
        endian = "big"
    else:
        return None
    e_machine = int.from_bytes(header[18:20], endian)
    return _ELF_ARCH_BY_MACHINE.get(e_machine)


def validate_binary_arch(path, expected_arch) -> Optional[str]:
    """Return an error string if the file at ``path`` is not an ARM ELF for ``expected_arch``."""
    try:
        with open(path, "rb") as f:
            header = f.read(_ELF_HEADER_READ)
    except OSError as e:
        # The raw OSError can carry a filesystem path; keep it in the server log
        # and return a generic message so it is not exposed to the HTTP client.
        log.warning("Could not read engine binary at %s: %s", path, e)
        return "Could not read the engine binary."
    arch = detect_elf_arch(header)
    if arch is None:
        return "File is not a recognized ARM ELF executable."
    if arch != expected_arch:
        return (
            f"Engine binary architecture ({arch}) does not match this device "
            f"({expected_arch})."
        )
    return None


def _is_blocked_ip(ip_text: str) -> bool:
    """Whether an IP must not be a download target (SSRF guard).

    Unparseable input is treated as blocked: failing closed is correct when the
    address cannot be reasoned about.
    """
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_download_url(url, *, resolver: Callable = socket.getaddrinfo) -> Optional[str]:
    """Return an error string if ``url`` is not a safe HTTPS download target, else None.

    Enforces HTTPS and that the host resolves only to public addresses. The
    resolver is injected so the rule is unit-testable offline.
    """
    if not url or not isinstance(url, str):
        return "URL is required."
    parts = urlsplit(url)
    if parts.scheme != "https":
        return "Only https:// URLs are allowed."
    host = parts.hostname
    if not host:
        return "URL has no host."
    try:
        infos = resolver(host, None)
    except socket.gaierror:
        return f"Could not resolve host: {host}"
    addresses = [info[4][0] for info in infos] if infos else []
    if not addresses:
        return f"Could not resolve host: {host}"
    for address in addresses:
        if _is_blocked_ip(address):
            return "Refusing to download from a private, loopback, or link-local address."
    return None


def locate_engine_binary_in_dir(root, expected_arch) -> Tuple[Optional[str], Optional[str]]:
    """Find exactly one ``expected_arch`` ELF binary under ``root``.

    Returns ``(path, None)`` when exactly one matching binary exists, else
    ``(None, error)``. Non-ELF and wrong-arch files (READMEs, x86 builds) are
    ignored as candidates; zero or multiple matches are ambiguous and rejected.
    Symlinks are skipped so an archive cannot point the binary outside ``root``.
    """
    matches = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            try:
                with open(full, "rb") as f:
                    header = f.read(_ELF_HEADER_READ)
            except OSError:  # noqa: S112 - an unreadable archive member is simply not a candidate binary; nothing to log
                continue
            if detect_elf_arch(header) == expected_arch:
                matches.append(full)
    if not matches:
        return None, f"Archive contains no {expected_arch} engine binary."
    if len(matches) > 1:
        return None, "Archive contains multiple engine binaries; expected exactly one."
    return matches[0], None


def install_binary_payload(
    *, source_path, is_archive, dest_path, expected_arch, safe_extract
) -> Optional[str]:
    """Place a single validated UCI binary at ``dest_path``; return an error or None.

    For a raw binary, the file's architecture is validated and the file is copied
    into place. For a ``.tar.gz``, it is extracted with the injected
    path-traversal-safe extractor, the single matching-arch binary is located,
    and that file is copied into place. The installed file is made executable
    (0o755) so the engine can be launched.
    """
    if is_archive:
        return _install_archive(source_path, dest_path, expected_arch, safe_extract)
    err = validate_binary_arch(source_path, expected_arch)
    if err:
        return err
    return _place_binary(source_path, dest_path)


def _install_archive(source_path, dest_path, expected_arch, safe_extract) -> Optional[str]:
    tmpdir = tempfile.mkdtemp(prefix="custom_engine_")
    try:
        try:
            with tarfile.open(source_path, "r:gz") as tar:
                # Path-traversal-safe extraction is the caller's responsibility,
                # injected so this module need not import engine_manager. The
                # extractor expects a Path for its pre-3.11.4 fallback branch.
                safe_extract(tar, Path(tmpdir))
        except tarfile.TarError as e:
            # TarError detail can include member paths; log it and return a
            # generic message rather than leaking internals to the client.
            log.warning("Could not extract custom engine archive: %s", e)
            return "Could not extract the archive; it may be corrupt or not a valid .tar.gz."
        located, err = locate_engine_binary_in_dir(tmpdir, expected_arch)
        if err:
            return err
        return _place_binary(located, dest_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _place_binary(source_path, dest_path) -> Optional[str]:
    try:
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        shutil.copyfile(source_path, dest_path)
        # A UCI engine must be executable to be launched; 0o755 (no group/other
        # write) is the least-permissive mode that allows exec for the service.
        os.chmod(dest_path, 0o755)  # noqa: S103  # nosec B103 - engine binary must be executable
    except OSError as e:
        # OSError can carry filesystem paths; log the detail and return a generic
        # message so it is not exposed to the HTTP client.
        log.warning("Could not install engine binary to %s: %s", dest_path, e)
        return "Could not install the engine binary."
    return None
