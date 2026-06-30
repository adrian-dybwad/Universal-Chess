"""Import the original DGT Centaur software from an uploaded SD-card image.

The original Centaur app lives on an ext4 partition that macOS cannot mount, so
the user captures it with ``tools/centaur-import/make-centaur-image.sh`` into a
gzip image. This package loop-mounts that image read-only on the Pi, locates the
app inside it, validates the file set, and copies it into the managed
``CENTAUR_HOME`` with debug cruft stripped -- removing the prior dependence on
hand-placed ``/home/pi/centaur`` and ``/opt/DGTCentaurMods`` state.

Design: the pure functions (``detect_app_dir``, ``validate_app_dir``,
``ignore_cruft``) are directly testable; the privileged loop-mount/umount and the
gzip decompression are injected into ``install_from_image`` as the side-effect
boundary, so the orchestration is testable without root.
"""

from universalchess.paths import CENTAUR_HOME
from universalchess.services.centaur_import.detection import (
    REQUIRED_APP_ENTRIES,
    ValidationResult,
    detect_app_dir,
    ignore_cruft,
    validate_app_dir,
)
from universalchess.services.centaur_import.installer import (
    CentaurImportError,
    InstallResult,
    ensure_factory_marker,
    install_from_image,
)


def centaur_app_installed(app_dir=CENTAUR_HOME) -> bool:
    """Whether a *complete, launchable* Centaur install exists at ``app_dir``.

    A complete install has the ``centaur`` executable plus ``engines/`` and
    ``fonts/`` (the full ``validate_app_dir`` set), not merely the executable. A
    partial import -- e.g. a copy that failed after the top-level files but before
    ``engines``/``fonts`` -- must not count as installed: launching it shows the
    Centaur splash and then hangs because the engine and fonts are missing. This
    is the single source of truth for the on-board menu gate and the web
    availability flag so both refuse to offer an unlaunchable install.
    """
    return validate_app_dir(app_dir).ok


__all__ = [
    "REQUIRED_APP_ENTRIES",
    "ValidationResult",
    "detect_app_dir",
    "ignore_cruft",
    "validate_app_dir",
    "centaur_app_installed",
    "ensure_factory_marker",
    "CentaurImportError",
    "InstallResult",
    "install_from_image",
]
