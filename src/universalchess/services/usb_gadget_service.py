"""USB Ethernet gadget mode: desired preference, live detection, boot prep.

Mirrors :mod:`universalchess.services.system_time_service`: the OS (and boot
config) are the live truth, a short preference is persisted in ``centaur.ini``,
and privileged changes go through the pinned ``uc-usb-gadget-admin`` helper via
``sudo -n``.

Modes match Raspberry Pi's USB Ethernet gadget stack:

- ``off`` -- gadget disabled
- ``auto`` -- the vendor's ``rpi-usb-gadget-ics.service`` decides between Client
  and Shared from what the host offers. The helper enables that unit and pins no
  profile, so ``live`` is whichever concrete mode the switcher currently holds.
- ``client`` -- Pi takes DHCP from the host. Applied via ``rpi-usb-gadget on -f``
  then pinning the Client NM profile and disabling the switcher (vendor ``on``
  alone brings Shared up and leaves the switcher enabled).
- ``shared`` -- Pi serves DHCP at ``10.12.194.1``. Current ``rpi-usb-gadget``
  packages have no ``shared`` verb; the helper enables the stack then pins the
  Shared NM profile and disables the switcher.

``auto`` therefore cannot be read back from usb0 -- the address only ever says
Client or Shared. Whether the board is in Auto is the switcher unit's enable
state (``auto_switching``), which is why Client/Shared are only in their expected
state while that unit is *not* enabled: a running switcher can move them.

``prepared`` means boot still loads ``dwc2`` / ``g_ether`` (overlay in
config.txt plus ``g_ether`` on the kernel cmdline *or* in
``/etc/modules-load.d/usb-gadget.conf``, which is what current
``rpi-usb-gadget on`` writes). When no preference is stored yet and the boot is
prepared, desired is seeded from the switcher: ``auto`` on a card prepared with
``enable_usb_gadget.py --auto``, otherwise ``client``, so the Connectivity select
does not read Off (or the wrong mode) on a card that script prepared.
"""

from __future__ import annotations

import configparser
import logging
import subprocess  # nosec B404 - only ever runs fixed argv lists, never shell=True
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

HELPER_PATH = "/opt/universalchess/scripts/uc-usb-gadget-admin"
MODES = frozenset({"off", "auto", "client", "shared"})
# Modes that leave the gadget stack loaded, so they share the boot/reboot rules.
ON_MODES = frozenset({"auto", "client", "shared"})
# Vendor unit that switches Client<->Shared from what the host offers.
ICS_UNIT = "rpi-usb-gadget-ics.service"
SETTINGS_SECTION = "system"
SETTINGS_KEY = "usb_gadget_mode"
SHARED_IPV4 = "10.12.194.1"
DWC2_OVERLAY = "dtoverlay=dwc2,dr_mode=peripheral"
DEFAULT_SETTINGS_PATH = Path("/opt/universalchess/config/centaur.ini")
DEFAULT_MODULES_LOAD_PATH = Path("/etc/modules-load.d/usb-gadget.conf")

_CONFIG_CANDIDATES = (
    Path("/boot/firmware/config.txt"),
    Path("/boot/config.txt"),
)
_CMDLINE_PROC = Path("/proc/cmdline")
_USB0_SYSFS = Path("/sys/class/net/usb0")

CLIENT_CONN_NAME = "USB Gadget (client)"
SHARED_CONN_NAME = "USB Gadget (shared)"
# Kernel UDC ``state`` values normalised for the API / UI.
ATTACHMENT_NONE = "none"
ATTACHMENT_ATTACHED = "attached"
ATTACHMENT_NOT_ATTACHED = "not_attached"
ATTACHMENT_UNKNOWN = "unknown"
ATTACHMENTS = frozenset(
    {
        ATTACHMENT_NONE,
        ATTACHMENT_ATTACHED,
        ATTACHMENT_NOT_ATTACHED,
        ATTACHMENT_UNKNOWN,
    }
)
_UDC_CLASS = Path("/sys/class/udc")
# Sentinels so get_status can tell "probe the host" from an explicit None, which
# for these two signals means "could not be determined" rather than "absent".
_UDC_UNSET: object = object()
_AUTO_UNSET: object = object()

CommandRunner = Callable[[Sequence[str], float], "subprocess.CompletedProcess"]
_APPLY_TIMEOUT_SECONDS = 60
# Status probe, not an apply: it runs on every poll, so it must not be able to
# hold a status request open the way a mode change legitimately can.
_LEASE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class UsbGadgetStatus:
    """Desired preference vs live OS state for USB Ethernet gadget mode."""

    desired: str
    live: str
    prepared: bool
    in_expected_state: bool
    reboot_required: bool
    attachment: str
    ipv4: str | None = None
    dhcp_lease_count: int | None = None
    # True/False when the vendor switcher's enable state is known, None when it
    # cannot be determined -- see detect_auto_switching.
    auto_switching: bool | None = None


def invalidate_status_cache() -> None:
    """No-op cache hook (kept for API symmetry with system_time_service)."""
    return


def _default_runner(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603  # nosec B603 - fixed argv list (no shell)
        args, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _cmdline_has_g_ether(cmdline_txt: str) -> bool:
    """Return True when the kernel cmdline loads g_ether (legacy enable_usb_gadget.py)."""
    words = cmdline_txt.replace("\n", " ").split()
    if any(
        "g_ether" in word
        for word in words
        if word.startswith("modules-load=") or word == "g_ether"
    ):
        return True
    return "g_ether" in cmdline_txt and "modules-load=" in cmdline_txt


def _modules_load_has_g_ether(modules_load_txt: str) -> bool:
    """Return True when modules-load.d requests g_ether (current rpi-usb-gadget)."""
    for raw in modules_load_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line == "g_ether" or line.startswith("g_ether "):
            return True
    return False


def is_prepared(
    *,
    config_txt: str,
    cmdline_txt: str,
    modules_load_txt: str = "",
) -> bool:
    """Return True when boot config loads the USB Ethernet gadget stack.

    The dwc2 peripheral overlay is required, plus ``g_ether`` either on the
    kernel command line (older host-side prep) or in modules-load.d (what
    current ``rpi-usb-gadget on`` writes). Either modules source alone with the
    overlay is enough; overlay alone or modules alone is not.
    """
    has_overlay = DWC2_OVERLAY in config_txt
    has_g_ether = _cmdline_has_g_ether(cmdline_txt) or _modules_load_has_g_ether(
        modules_load_txt
    )
    return has_overlay and has_g_ether


def detect_live_mode(  # noqa: PLR0911 - ordered precedence cascade; see docstring
    *,
    usb0_exists: bool,
    usb0_ipv4: str | None,
    nm_active: str | None,
    nm_profile_names: frozenset[str] | None = None,
) -> str:
    """Infer live gadget mode from netdev / address / NetworkManager profile.

    Returns ``off``, ``client``, ``shared``, or ``unknown``.

    After ``rpi-usb-gadget off``, ``usb0`` can linger until reboot with no NM
    profiles -- that must read as ``off``. When Client/Shared profiles still
    exist and ``usb0`` is up but has no address yet (host not attached / no
    DHCP), that is configured Client (or Shared) waiting for a link -- not Off.
    """
    profiles = nm_profile_names if nm_profile_names is not None else frozenset()
    if nm_active:
        lowered = nm_active.lower()
        if "shared" in lowered:
            return "shared"
        if "client" in lowered:
            return "client"
    if not usb0_exists:
        return "off"
    if usb0_ipv4 == SHARED_IPV4:
        return "shared"
    if usb0_ipv4:
        return "client"
    has_client = CLIENT_CONN_NAME in profiles
    has_shared = SHARED_CONN_NAME in profiles
    if has_client or has_shared:
        # Both profiles normally exist after vendor ``on``; Shared gets a fixed
        # address when its profile is up, so an idle netdev with profiles is
        # Client-style bring-up (waiting for host DHCP / carrier).
        if has_shared and not has_client:
            return "shared"
        return "client"
    # Leftover usb0 after Off removed the profiles.
    return "off"


def detect_auto_switching(*, is_enabled: str | None) -> bool | None:
    """Map ``systemctl is-enabled`` on the vendor switcher to on / off / unknown.

    ``enabled`` and ``enabled-runtime`` are on; ``disabled`` and ``masked`` are
    off. Anything else -- including ``static`` (no ``[Install]`` section, so
    neither enable nor disable applies) and a probe that could not run -- is
    None. None must stay None: inventing False would show a mismatch on a
    working Auto board, and inventing True would hide a switcher that is not
    running.
    """
    if is_enabled is None:
        return None
    state = is_enabled.strip().lower()
    if state in ("enabled", "enabled-runtime"):
        return True
    if state in ("disabled", "masked"):
        return False
    return None


def detect_attachment(*, udc_state: str | None) -> str:
    """Map kernel UDC ``state`` to a closed attachment token for the UI.

    ``none`` -- no UDC (gadget stack not bound). ``attached`` / ``not_attached``
    -- host cable presence on the gadget controller. Anything else is
    ``unknown`` rather than inventing a status.
    """
    if udc_state is None:
        return ATTACHMENT_NONE
    normalised = udc_state.strip().lower()
    if normalised == "":
        return ATTACHMENT_NONE
    if normalised == "attached":
        return ATTACHMENT_ATTACHED
    if normalised == "not attached":
        return ATTACHMENT_NOT_ATTACHED
    return ATTACHMENT_UNKNOWN


def count_dhcp_leases(lease_txt: str) -> int:
    """Count dnsmasq lease records in a lease-file body.

    Empty / whitespace-only files are zero. Comment lines (``#``) are ignored.
    A non-empty lease line means Shared DHCP has handed an address to someone
    on usb0 -- the signal that was missing when the host sat on APIPA.
    """
    count = 0
    for raw in lease_txt.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        count += 1
    return count


def read_shared_lease_count(
    *,
    run: CommandRunner = _default_runner,
    helper_path: str = HELPER_PATH,
) -> int | None:
    """Count NetworkManager's Shared dnsmasq leases on usb0, or None if unknown.

    The lease file lives in NetworkManager's state directory, which is 0700 root,
    so the read goes through the same pinned helper the mode changes use. Reading
    it directly would require making that directory traversable by everyone on
    the machine, which is what releases up to 2.0.0 did.

    None means this board cannot tell (no grant, no helper, timeout) and is a
    different fact from zero, which means Shared is running and nothing has taken
    an address -- the APIPA case this count exists to expose.
    """
    args = ["sudo", "-n", helper_path, "read-shared-leases"]
    try:
        proc = run(args, _LEASE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("usb gadget: could not read shared leases (%s)", exc)
        return None
    if proc.returncode != 0:
        log.debug(
            "usb gadget: lease read exited %s: %s",
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return None
    return count_dhcp_leases(proc.stdout or "")


def format_epaper_status(*, attachment: str, ipv4: str | None) -> str:
    """Short Connected/Disconnected line (+ IP) for the selected e-paper radio.

    An address on ``usb0`` means the USB Ethernet session is up -- including
    Shared's fixed ``10.12.194.1`` and a Client DHCP lease. Never report
    Disconnected (or ``No host``) alongside an IP; that contradicts the only
    signal the user can verify while using the link.
    """
    address = (ipv4 or "").strip() or None
    if address:
        return f"Connected\n{address}"
    if attachment == ATTACHMENT_ATTACHED:
        return "Connected"
    return "Disconnected"


def in_expected_state(
    *,
    desired: str,
    live: str,
    auto_switching: bool | None = None,
) -> bool:
    """Return True when the board is running the mode the user selected.

    Off/Client/Shared are the concrete modes and must equal ``live``. ``auto``
    has no live mode of its own -- the switcher holds Client or Shared -- so both
    count, but only while that switcher is enabled; with it disabled the board is
    pinned and can no longer switch. Conversely a pinned mode with the switcher
    still enabled is not settled either, since the unit can move it at any
    moment. An unknown switcher state never flips a matching pin to False.
    """
    if live == "unknown" or desired not in MODES:
        return False
    if desired == "auto":
        if auto_switching is False:
            return False
        return live in ("client", "shared")
    if desired in ("client", "shared") and auto_switching is True:
        return False
    return desired == live


def _read_desired(settings_path: Path) -> str | None:
    """Return the stored preference, or None if unset / unreadable."""
    if not settings_path.is_file():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(settings_path)
    except configparser.Error as exc:
        log.debug("usb gadget: could not parse %s (%s)", settings_path, exc)
        return None
    if not parser.has_option(SETTINGS_SECTION, SETTINGS_KEY):
        return None
    value = parser.get(SETTINGS_SECTION, SETTINGS_KEY).strip().lower()
    if value in MODES:
        return value
    log.debug("usb gadget: ignoring invalid stored mode %r", value)
    return None


def _write_desired(settings_path: Path, mode: str) -> None:
    """Persist ``mode`` under ``[system] usb_gadget_mode``."""
    parser = configparser.ConfigParser()
    if settings_path.is_file():
        try:
            parser.read(settings_path)
        except configparser.Error:
            parser = configparser.ConfigParser()
    if not parser.has_section(SETTINGS_SECTION):
        parser.add_section(SETTINGS_SECTION)
    parser.set(SETTINGS_SECTION, SETTINGS_KEY, mode)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("usb gadget: could not read %s (%s)", path, exc)
        return ""


def _default_config_txt() -> str:
    for path in _CONFIG_CANDIDATES:
        if path.is_file():
            return _read_text(path)
    return ""


def _default_cmdline_txt() -> str:
    if _CMDLINE_PROC.is_file():
        return _read_text(_CMDLINE_PROC)
    return ""


def _default_modules_load_txt() -> str:
    return _read_text(DEFAULT_MODULES_LOAD_PATH)


def _default_usb0_exists() -> bool:
    return _USB0_SYSFS.exists()


def _default_usb0_ipv4() -> str | None:
    """Best-effort first IPv4 on usb0 via ``ip -4 -o addr show dev usb0``."""
    if not _default_usb0_exists():
        return None
    try:
        proc = _default_runner(
            ["ip", "-4", "-o", "addr", "show", "dev", "usb0"],
            5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    # Example: ``2: usb0    inet 192.168.2.3/24 ...``
    for token in proc.stdout.split():
        if "/" in token and token[0].isdigit():
            return token.split("/", 1)[0]
    return None


def _default_nm_active() -> str | None:
    """Active NetworkManager connection name on usb0, if nmcli is available."""
    if not _default_usb0_exists():
        return None
    try:
        proc = _default_runner(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", "usb0"],
            5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "GENERAL.CONNECTION":
            name = value.strip()
            return name or None
    return None


def _default_nm_profile_names() -> frozenset[str]:
    """Names of saved NetworkManager connections (for Client/Shared presence)."""
    try:
        proc = _default_runner(
            ["nmcli", "-t", "-f", "NAME", "connection", "show"],
            5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if proc.returncode != 0 or not proc.stdout:
        return frozenset()
    return frozenset(line.strip() for line in proc.stdout.splitlines() if line.strip())


def _default_auto_switching() -> bool | None:
    """Enable state of the vendor switcher unit, or None when it cannot be read."""
    try:
        proc = _default_runner(["systemctl", "is-enabled", ICS_UNIT], 5.0)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("usb gadget: could not query %s (%s)", ICS_UNIT, exc)
        return None
    # is-enabled exits non-zero for disabled and for a missing unit alike, so the
    # printed state -- not the exit status -- is what distinguishes them.
    return detect_auto_switching(is_enabled=proc.stdout)


def _default_udc_state() -> str | None:
    """Raw ``state`` from the first (or attached) USB device controller."""
    if not _UDC_CLASS.is_dir():
        return None
    try:
        children = sorted(p for p in _UDC_CLASS.iterdir() if p.is_dir())
    except OSError as exc:
        log.debug("usb gadget: could not list %s (%s)", _UDC_CLASS, exc)
        return None
    states: list[str] = []
    for child in children:
        raw = _read_text(child / "state").strip()
        if not raw:
            continue
        if raw.lower() == "attached":
            return raw
        states.append(raw)
    return states[0] if states else None


def _resolve_lease_count(
    *,
    live: str,
    injected_count: int | None,
    lease_run: CommandRunner | None,
    probing_host: bool,
) -> int | None:
    """Decide the Shared DHCP lease count for one status read.

    The count is the pool *this* board serves, so it means something only in
    Shared -- which is also the only mode the web card renders it for. Probing it
    in Client or Off would spend a privileged call on every status poll to
    produce a number nothing displays.
    """
    if injected_count is not None:
        return injected_count
    if live != "shared":
        return None
    if lease_run is not None:
        return read_shared_lease_count(run=lease_run)
    if not probing_host:
        # Injected (test) path: never reach for the machine running the tests.
        return None
    return read_shared_lease_count()


def get_status(  # noqa: PLR0913 - keyword-only probe seams, injected per test
    *,
    config_txt: str | None = None,
    cmdline_txt: str | None = None,
    modules_load_txt: str | None = None,
    usb0_exists: bool | None = None,
    usb0_ipv4: str | None = None,
    nm_active: str | None = None,
    nm_profile_names: frozenset[str] | None = None,
    udc_state: object = _UDC_UNSET,
    dhcp_lease_count: int | None = None,
    lease_run: CommandRunner | None = None,
    auto_switching: object = _AUTO_UNSET,
    settings_path: Path | None = None,
) -> UsbGadgetStatus:
    """Return desired / live / prepared / attachment / expected-state for the gadget."""
    path = Path(settings_path) if settings_path is not None else DEFAULT_SETTINGS_PATH
    cfg = _default_config_txt() if config_txt is None else config_txt
    cmdline = _default_cmdline_txt() if cmdline_txt is None else cmdline_txt
    modules = (
        _default_modules_load_txt() if modules_load_txt is None else modules_load_txt
    )
    prepared = is_prepared(
        config_txt=cfg, cmdline_txt=cmdline, modules_load_txt=modules
    )

    # Each live signal is probed only when the caller injected nothing at all.
    # Injecting usb0_exists while omitting a signal means "absent", never "go
    # probe the host" -- tests inject an explicit frozenset, including empty.
    exists = _default_usb0_exists() if usb0_exists is None else usb0_exists
    ipv4 = _default_usb0_ipv4() if usb0_ipv4 is None and usb0_exists is None else usb0_ipv4
    nm = _default_nm_active() if nm_active is None and usb0_exists is None else nm_active
    profiles = (
        _default_nm_profile_names()
        if nm_profile_names is None and usb0_exists is None
        else (nm_profile_names if nm_profile_names is not None else frozenset())
    )

    if udc_state is not _UDC_UNSET:
        udc_raw = udc_state if isinstance(udc_state, str) or udc_state is None else None
    elif usb0_exists is not None or config_txt is not None:
        # Test / injected path: do not read the local machine's UDC class.
        udc_raw = None
    else:
        udc_raw = _default_udc_state()

    if auto_switching is not _AUTO_UNSET:
        switching = auto_switching if isinstance(auto_switching, bool) else None
    elif usb0_exists is not None or config_txt is not None:
        switching = None
    else:
        switching = _default_auto_switching()

    live = detect_live_mode(
        usb0_exists=exists,
        usb0_ipv4=ipv4,
        nm_active=nm,
        nm_profile_names=profiles,
    )

    leases = _resolve_lease_count(
        live=live,
        injected_count=dhcp_lease_count,
        lease_run=lease_run,
        probing_host=usb0_exists is None and config_txt is None,
    )

    attachment = detect_attachment(udc_state=udc_raw)
    # An address from host DHCP (Client) means the USB session is up even when
    # UDC ``state`` lags. Shared's fixed 10.12.194.1 is always configured and
    # must not force ``attached`` -- that hid not-attached / no-carrier after
    # unplug.
    if ipv4 and ipv4 != SHARED_IPV4 and attachment != ATTACHMENT_ATTACHED:
        attachment = ATTACHMENT_ATTACHED

    desired = _read_desired(path)
    if desired is None:
        if prepared:
            # A prepared boot with the switcher still enabled is what
            # ``enable_usb_gadget.py --auto`` leaves; every other prepared card
            # pins a profile and disables it, and Client is the tool's default.
            desired = "auto" if switching else "client"
            try:
                _write_desired(path, desired)
            except OSError as exc:
                log.debug("usb gadget: could not seed preference (%s)", exc)
        else:
            desired = "off"

    expected = in_expected_state(
        desired=desired, live=live, auto_switching=switching
    )
    # Boot still loads the gadget while the user asked for off (or the reverse
    # for enabling without modules): a reboot finishes persistence. ``off`` also
    # needs a reboot when usb0/g_ether linger after the vendor tool cleared the
    # boot markers (prepared False but netdev still present). Any on-mode after
    # Off writes markers immediately (prepared True) while usb0 is still absent
    # until reboot -- that case must offer Reboot now too.
    reboot_required = (
        (desired == "off" and prepared)
        or (desired == "off" and exists)
        or (desired in ON_MODES and not prepared)
        or (desired in ON_MODES and prepared and not exists)
    )

    return UsbGadgetStatus(
        desired=desired,
        live=live,
        prepared=prepared,
        in_expected_state=expected,
        reboot_required=reboot_required,
        attachment=attachment,
        ipv4=ipv4,
        dhcp_lease_count=leases,
        auto_switching=switching,
    )


def reconcile_desired_mode(
    *,
    settings_path: Path | None = None,
    live: str | None = None,
    prepared: bool | None = None,
    auto_switching: bool | None = None,
    run: CommandRunner = _default_runner,
    helper_path: str = HELPER_PATH,
) -> bool:
    """Re-apply the stored preference when live mode does not match.

    Called on web/board startup so a reboot that left Shared up (vendor
    autoconnect / ICS watcher) is corrected without another UI click. Returns
    True only when ``set_mode`` ran and reported applied. Skips when already
    matched so a healthy Client link is not bounced on every service start --
    which for Auto means either concrete mode is left alone, and only a switcher
    that is no longer enabled brings Auto back.
    """
    path = Path(settings_path) if settings_path is not None else DEFAULT_SETTINGS_PATH
    if live is None or prepared is None:
        status = get_status(settings_path=path)
        if live is None:
            live = status.live
        if prepared is None:
            prepared = status.prepared
        if auto_switching is None:
            auto_switching = status.auto_switching
        desired = status.desired
    else:
        desired = _read_desired(path)
        if desired is None:
            desired = "client" if prepared else "off"

    if desired not in MODES:
        return False
    if live == "unknown":
        return False
    if in_expected_state(desired=desired, live=live, auto_switching=auto_switching):
        return False
    return set_mode(
        desired,
        run=run,
        helper_path=helper_path,
        settings_path=path,
        prepared=prepared,
    )


def set_mode(
    mode: str,
    *,
    run: CommandRunner = _default_runner,
    helper_path: str = HELPER_PATH,
    settings_path: Path | None = None,
    prepared: bool = False,  # noqa: ARG001 - reserved for prepare/unprepare apply path
) -> bool:
    """Persist ``mode`` and ask the helper to apply it. Returns whether apply succeeded."""
    normalised = (mode or "").strip().lower()
    if normalised not in MODES:
        message = f"invalid usb gadget mode: {mode!r}"
        raise ValueError(message)

    path = Path(settings_path) if settings_path is not None else DEFAULT_SETTINGS_PATH
    try:
        _write_desired(path, normalised)
    except OSError as exc:
        log.warning("usb gadget: could not persist preference (%s)", exc)

    args = ["sudo", "-n", helper_path, normalised]
    try:
        proc = run(args, _APPLY_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("usb gadget: helper invoke failed (%s)", exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "usb gadget: helper exited %s: %s",
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return False
    return True
