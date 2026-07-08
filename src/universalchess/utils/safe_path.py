"""Filesystem path containment guard shared across the app.

Single defense against path traversal (CWE-22, "Uncontrolled data used in a path
expression"). Any filesystem path built from untrusted input (HTTP request data,
route parameters, uploaded filenames, resource names) must be resolved through
``safe_under_base`` before it reaches ``open``/``os.*``/``send_file``.

Implementation notes:
  - Normalization uses ``os.path.realpath`` (follows symlinks and removes ``..``)
    rather than ``pathlib.Path.resolve()``. The os.path form is the
    normalization that static analysis (CodeQL ``py/path-injection``) recognizes
    as part of a valid containment guard; ``Path.resolve()`` is itself reported
    as a path access and its result is not treated as sanitized.
  - Containment is enforced with an explicit ``startswith(base + os.sep)`` check
    on the normalized strings, the documented and tool-recognized pattern, in
    place of ``Path.relative_to`` (which static analysis does not model as a
    barrier).
  - Every leading separator is stripped from each part before joining, because
    ``os.path.join(base, "/abs")`` discards ``base`` and returns ``/abs``; the
    strip is what prevents an absolute-path payload from escaping the base.
"""

import os

__all__ = ["safe_under_base", "safe_leaf_under_base"]


def safe_under_base(base, *user_parts):
    """Resolve untrusted ``user_parts`` under ``base``, enforcing containment.

    Returns the contained absolute path as a ``str`` so callers can pass it
    straight to ``open``/``os.*``/``send_file``, or ``None`` when a part is
    missing/empty or the resolved path escapes ``base``.

    Args:
        base: Trusted base directory the result must stay within.
        *user_parts: One or more untrusted path segments to join under ``base``.

    Returns:
        The normalized absolute path string inside ``base``, or ``None`` if the
        input is empty or would escape ``base``.
    """
    if not user_parts or any(part is None or str(part) == "" for part in user_parts):
        return None

    base_real = os.path.realpath(base)
    stripped = [str(part).lstrip("/").lstrip("\\") for part in user_parts]
    candidate = os.path.realpath(os.path.join(base_real, *stripped))

    # Containment check on normalized paths: candidate must be base itself or a
    # descendant of base. The os.sep suffix prevents a sibling like
    # "/home/pi-evil" from matching base "/home/pi".
    if candidate != base_real and not candidate.startswith(base_real + os.sep):
        return None
    return candidate


def safe_leaf_under_base(base, name):
    """Contain untrusted ``name`` under ``base`` without resolving a leaf symlink.

    Same containment guarantee as :func:`safe_under_base` -- the untrusted
    ``name`` cannot escape ``base`` via ``..`` or an absolute path -- but the
    resolved path is normalized with ``os.path.normpath`` instead of
    ``os.path.realpath`` so that a *legitimate* symlink at the leaf is preserved
    rather than followed. This is required for engine binaries: the installer
    creates ``<engines>/stockfish`` as a symlink to a system location such as
    ``/usr/games/stockfish`` (see ``engine_manager._install_system_package``), and
    a ``realpath``-based guard would resolve that link to a path outside the
    engines directory and wrongly reject the engine.

    Only the leaf link is spared: ``normpath`` still collapses ``..`` textually,
    so a traversing ``name`` is rejected before it can escape. ``base`` itself is
    resolved with ``realpath`` so the containment prefix is stable even when the
    base directory is reached through a symlink.

    Both ``os.path.normpath`` and the explicit ``startswith(base + os.sep)`` check
    are the containment pattern static analysis (CodeQL ``py/path-injection``)
    recognizes as a sanitizer -- it is the "GOOD" example in that query's own
    documentation -- so callers may pass the result straight to ``os.*``.

    Args:
        base: Trusted base directory the result must stay within.
        name: A single untrusted path segment (may contain ``/`` subdirectories).

    Returns:
        The normalized absolute path string inside ``base`` (whose leaf may be a
        symlink pointing elsewhere), or ``None`` if ``name`` is empty or would
        escape ``base``.
    """
    if name is None or str(name) == "":
        return None

    base_real = os.path.realpath(base)
    stripped = str(name).lstrip("/").lstrip("\\")
    candidate = os.path.normpath(os.path.join(base_real, stripped))

    # Containment check on the textually normalized path: the leaf may be a
    # symlink out of base, but the name must not traverse out of it. The os.sep
    # suffix stops a sibling like "/opt/engines-evil" from matching "/opt/engines".
    if candidate != base_real and not candidate.startswith(base_real + os.sep):
        return None
    return candidate
