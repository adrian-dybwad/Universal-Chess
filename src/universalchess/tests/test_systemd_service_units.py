"""Tests for the systemd units and log handling the package ships.

Root cause these guard: making the install tree root-owned (see
``test_postinst_privilege_boundary``) left ``universal-chess.service`` running
with ``WorkingDirectory=/opt/universalchess``. lgpio creates its notification
FIFO ``.lgd-nfy<n>`` in the process working directory the moment it is imported,
and the service user can no longer create files there. Importing ``RPi.GPIO`` --
reached from the epaper driver on the startup path -- therefore died with::

    xCreatePipe: Can't set permissions (436) for /opt/universalchess/.lgd-nfy0,
    Operation not permitted
    FileNotFoundError: [Errno 2] No such file or directory: '.lgd-nfy-3'

(the EPERM makes lgpio return the error handle ``-3``, which it then formats
straight back into a filename). That happens at import time, before the board
controller is constructed, so the board was never detected and the unit
crash-looped every thirteen seconds on a board that had just taken an update.

The second failure these guard is diagnostic rather than functional. Both units
redirected their output with ``StandardOutput=file:``, which systemd opens at
offset zero *without* truncating. Each restart overwrote the head of the log
while stale content stayed behind it, so the file carried a current mtime and
historic contents: ``tail`` showed entries from fourteen hours earlier and the
live crash was only visible via ``head``. That cost most of the time spent
finding the fault above.
"""

from pathlib import Path

import pytest

from universalchess import paths
from universalchess.services.game_broadcast import SOCKET_DIR

# Repo layout: .../src/universalchess/paths.py -> repo root is three parents up.
DEB_ROOT = Path(paths.__file__).resolve().parent.parent.parent / "packaging" / "deb-root"
UNIT_DIR = DEB_ROOT / "etc" / "systemd" / "system"
TMPFILES_CONF = DEB_ROOT / "usr" / "lib" / "tmpfiles.d" / "universal-chess.conf"
LOGROTATE_CONF = DEB_ROOT / "etc" / "logrotate.d" / "universal-chess"
POSTINST = DEB_ROOT / "DEBIAN" / "postinst"

BOARD_UNIT = "universal-chess.service"
WEB_UNIT = "universal-chess-web.service"

# The units that redirect their output at a plain file rather than the journal.
UNITS_WITH_FILE_LOGS = (BOARD_UNIT, WEB_UNIT)
LOG_REDIRECT_DIRECTIVES = ("StandardOutput", "StandardError")

# The owner the shipped tmpfiles entry names. The postinst rewrites it to the
# detected primary user at install time; ``pi`` is the placeholder it matches on.
TMPFILES_PLACEHOLDER_OWNER = "pi"


def _service_directives(unit_name: str) -> dict:
    """Return the ``[Service]`` directives of a shipped unit as name -> values.

    Values are collected in a list because systemd permits a directive to be
    repeated, and a duplicate is itself worth asserting on: a second
    ``WorkingDirectory`` silently wins over the first.
    """
    unit_path = UNIT_DIR / unit_name
    assert unit_path.exists(), f"unit missing: {unit_path}"

    directives: dict = {}
    section = None
    for raw_line in unit_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Service" or "=" not in line:
            continue
        name, _, value = line.partition("=")
        directives.setdefault(name.strip(), []).append(value.strip())
    return directives


def _sole_working_directory(unit_name: str) -> str:
    """Return the single WorkingDirectory of a unit, without systemd's ``-`` prefix."""
    declared = _service_directives(unit_name).get("WorkingDirectory", [])
    assert len(declared) == 1, (
        f"{unit_name} declares {len(declared)} WorkingDirectory values; the last "
        f"would silently win: {declared}"
    )
    return declared[0].lstrip("-")


def _redirected_log_paths() -> set:
    """Every file path the shipped units send their output to."""
    paths_found = set()
    for unit_name in UNITS_WITH_FILE_LOGS:
        directives = _service_directives(unit_name)
        for directive in LOG_REDIRECT_DIRECTIVES:
            for value in directives.get(directive, []):
                _, separator, target = value.partition(":")
                if separator:
                    paths_found.add(target)
    return paths_found


def _tmpfiles_entries() -> list:
    """The shipped tmpfiles.d lines, split into fields."""
    assert TMPFILES_CONF.exists(), f"tmpfiles config missing: {TMPFILES_CONF}"
    return [
        line.split()
        for line in TMPFILES_CONF.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_board_service_does_not_work_from_the_root_owned_install_tree():
    """The board service must not run with its CWD inside ${DGTCM_PATH}.

    Why this test exists: this is the regression that stopped the board being
    detected. lgpio unconditionally creates ``.lgd-nfy<n>`` in the process
    working directory at import, and the install tree is root-owned so that the
    service user cannot rewrite the sudo-granted helpers in ``scripts/``. Those
    two requirements are incompatible, and the working directory is the side
    that must move -- widening the tree's permissions would restore the
    privilege escalation.

    How a regression manifests: the service exits 1 roughly five seconds into
    every start with ``FileNotFoundError: '.lgd-nfy-3'`` raised from
    ``import RPi.GPIO``, and restarts forever. Nothing reaches board discovery,
    so the symptom reported is "the board is not detected" rather than anything
    naming permissions or the working directory.
    """
    working_directory = _sole_working_directory(BOARD_UNIT)
    assert not working_directory.startswith(paths.BASE_DIR), (
        f"{BOARD_UNIT} runs from {working_directory}, inside the root-owned install "
        f"tree {paths.BASE_DIR}. lgpio creates its .lgd-nfy FIFO in the working "
        "directory at import time, so the service user cannot start the process."
    )


def test_board_service_works_from_the_runtime_directory_the_package_creates():
    """The board service CWD must be the tmpfiles-managed runtime directory.

    Why this test exists: any writable directory would fix the crash, but only
    one is guaranteed to exist, be owned by the service user, and be emptied on
    every boot. Pinning it to the constant the IPC sockets already use keeps a
    single runtime directory rather than inventing a second one, and the
    boot-time recreation is what stops a stale FIFO surviving a reboot.

    Note this must not become ``RuntimeDirectory=`` on this unit: systemd
    deletes a RuntimeDirectory when the unit stops, which would take
    ``game.sock`` and ``settings.sock`` with it on every restart and break the
    web process's link to the board.

    How a regression manifests: pointing this at a path nothing creates makes
    systemd refuse to spawn the service at all, which reads as a unit
    misconfiguration rather than as a missing directory.
    """
    working_directory = _sole_working_directory(BOARD_UNIT)
    assert working_directory == str(SOCKET_DIR), (
        f"{BOARD_UNIT} should work from the runtime directory {SOCKET_DIR}, which "
        f"tmpfiles creates and owns for the service user; got {working_directory}"
    )


def test_the_board_service_runtime_directory_is_created_for_the_service_user():
    """tmpfiles must create the working directory owned by the service user.

    Why this test exists: the previous test only pins the unit's side of the
    contract. If tmpfiles stopped creating that directory, or created it owned
    by root, the service would be back to a working directory it cannot write --
    the identical crash, reached by a different route.

    How a regression manifests: dropping the entry makes systemd fail to spawn
    the unit; changing the owner to root reproduces the original
    ``.lgd-nfy-3`` failure exactly, with no hint that the cause moved.
    """
    matching = [
        fields
        for fields in _tmpfiles_entries()
        if fields[0] == "d" and fields[1] == str(SOCKET_DIR)
    ]
    assert matching, (
        f"tmpfiles must create {SOCKET_DIR}; it is the working directory of "
        f"{BOARD_UNIT} and the home of the IPC sockets"
    )

    _, _, _, owner, group = matching[0][:5]
    assert owner == group == TMPFILES_PLACEHOLDER_OWNER, (
        f"{SOCKET_DIR} must be owned by the service user placeholder "
        f"'{TMPFILES_PLACEHOLDER_OWNER}', which the postinst rewrites to the "
        f"detected user; got {owner}:{group}"
    )
    assert f"s/ {TMPFILES_PLACEHOLDER_OWNER} {TMPFILES_PLACEHOLDER_OWNER} /" in (
        POSTINST.read_text()
    ), (
        "the postinst must rewrite the tmpfiles owner placeholder to the detected "
        "primary user, or the runtime directory is unwritable on any board whose "
        f"user is not '{TMPFILES_PLACEHOLDER_OWNER}'"
    )


@pytest.mark.parametrize("unit_name", UNITS_WITH_FILE_LOGS)
@pytest.mark.parametrize("directive", LOG_REDIRECT_DIRECTIVES)
def test_service_logs_are_appended_rather_than_overwritten(unit_name, directive):
    """File redirections must use ``append:``, never ``file:``.

    Why this test exists: systemd opens a ``file:`` target at offset zero
    without truncating it. A restarting service therefore overwrites the head of
    its own log while older, longer content survives underneath -- producing a
    file whose mtime is current and whose tail is hours stale. During the
    crash-loop this guards against, ``tail`` returned entries from fourteen
    hours earlier and the actual traceback was only reachable with ``head``.

    How a regression manifests: no failure and no error. Logs simply stop being
    trustworthy, and the cost is paid later by whoever is reading them during an
    incident.
    """
    values = _service_directives(unit_name).get(directive, [])
    assert values, f"{unit_name} declares no {directive}"
    for value in values:
        assert not value.startswith("file:"), (
            f"{unit_name} {directive}={value} overwrites the log from offset zero "
            "on every restart, interleaving new output with stale content; use "
            "append: instead"
        )
        assert value.startswith("append:"), (
            f"{unit_name} {directive}={value} is not an append redirection"
        )


@pytest.mark.parametrize("log_path", sorted(_redirected_log_paths()))
def test_every_service_log_is_rotated(log_path):
    """Each file a unit appends to must have a logrotate entry.

    Why this test exists: ``append:`` is correct but unbounded, whereas the
    ``file:`` behaviour it replaces capped a log at the high-water mark of its
    longest single run. Without rotation the change trades corrupt logs for a
    full SD card on a board that runs for weeks, which is a worse failure.

    How a regression manifests: nothing for months, then a board that has filled
    /var and fails in unrelated ways -- writes to the database, staged updates --
    with no obvious link to logging.
    """
    assert LOGROTATE_CONF.exists(), f"logrotate config missing: {LOGROTATE_CONF}"
    assert log_path in LOGROTATE_CONF.read_text(), (
        f"{log_path} is appended to by a service unit but is not rotated; it will "
        "grow without bound"
    )


def test_log_rotation_truncates_in_place():
    """Rotation must use ``copytruncate``.

    Why this test exists: systemd opens the redirect target once and holds that
    descriptor for the lifetime of the service. The default rotation strategy
    renames the file and creates a new one, which leaves the running service
    writing into the renamed file forever while the fresh one stays empty --
    logging silently stops until the next restart. ``copytruncate`` keeps the
    inode, and because the descriptor is opened ``O_APPEND`` the next write
    resumes at the start of the truncated file.

    How a regression manifests: logs appear normal for one rotation period and
    then stop growing, with the real output hidden in ``*.log.1``.
    """
    assert LOGROTATE_CONF.exists(), f"logrotate config missing: {LOGROTATE_CONF}"
    assert "copytruncate" in LOGROTATE_CONF.read_text(), (
        "rotation must truncate in place; systemd holds the log descriptor open, "
        "so a rename leaves the service writing to the rotated file"
    )


def test_postinst_removes_stale_lgpio_notify_pipes_from_the_install_tree():
    """The install must clear ``.lgd-nfy*`` left in ${DGTCM_PATH}.

    Why this test exists: boards that ran an earlier version created those FIFOs
    in the install root as the service user, and the ownership reset then
    chowned them to root -- freezing a runtime artifact into a tree that is meant
    to contain only packaged files. They are inert once the working directory
    moves, but they are also the exact object whose presence and ownership
    produced the original crash, so leaving them behind keeps a loaded gun in
    the tree for any future change that moves the working directory back.

    How a regression manifests: no immediate failure. The risk is deferred: the
    stale root-owned FIFO is indistinguishable from packaged content on
    inspection, and it re-breaks lgpio the moment anything runs from that
    directory as the service user again.
    """
    text = POSTINST.read_text()
    removal_lines = [
        line.strip()
        for line in text.splitlines()
        if ".lgd-nfy" in line and not line.strip().startswith("#")
    ]
    assert removal_lines, (
        "postinst must delete stale .lgd-nfy* FIFOs from the install tree; they "
        "are runtime artifacts the ownership reset otherwise freezes into it"
    )
