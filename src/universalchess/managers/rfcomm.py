# RFCOMM Manager
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""RFCOMM manager for Classic Bluetooth discovery and pairing.

This module manages RFCOMM (Classic Bluetooth) connections, which require
pairing before data can be exchanged. BLE GATT connections are handled
separately by ble_manager.py.

Features:
- Enable/disable Bluetooth adapter
- Device discovery for Classic Bluetooth
- PIN-based pairing for RFCOMM connections
- Device management (list, remove paired devices)
- Discoverability control

Usage:
    # Instance-based usage (recommended)
    manager = RfcommManager(device_name="My Device")
    manager.enable_bluetooth()
    manager.start_pairing(timeout=60)
    
    # Context manager usage
    with RfcommManager() as manager:
        manager.start_pairing_thread()
        # Bluetooth automatically enabled and cleaned up
"""
import time
import select
import subprocess
import threading
import re
import shutil
from typing import Optional, Callable, List, Dict
import pathlib
from universalchess.board.logging import log

try:
    import psutil as _psutil  # type: ignore
except ImportError:  # pragma: no cover (platform/environment dependent)
    _psutil = None


def _process_iter(attrs: List[str]):
    """Iterate processes if psutil is available; otherwise return an empty iterator.
    
    This keeps RFCOMM utilities usable in minimal environments (unit tests, non-Linux dev)
    without requiring psutil.
    """
    if _psutil is None:
        return []
    return _psutil.process_iter(attrs=attrs)


def _is_psutil_exception(exc: Exception) -> bool:
    if _psutil is None:
        return False
    return isinstance(exc, (_psutil.NoSuchProcess, _psutil.AccessDenied))


class RfcommManager:
    """Manager for RFCOMM (Classic Bluetooth) discovery and pairing.
    
    This manager handles Classic Bluetooth connections that require pairing:
    - Enable/disable Bluetooth adapter
    - Device discovery for Classic Bluetooth
    - PIN-based pairing for RFCOMM connections
    - Device management (list, remove paired devices)
    - Discoverability control for extended pairing windows
    
    Protocol Support:
        - RFCOMM (Classic Bluetooth): For reliable serial-like communication
        
    Cross-Platform Compatibility:
        - Android: Full support for both RFCOMM and BLE
        - iOS/iPhone: Full support for both RFCOMM and BLE  
        - Linux: BlueZ stack (primary target platform)
        - Windows: Limited (requires BlueZ-compatible stack)
        - macOS: Limited (requires BlueZ-compatible stack)
    
    Industry Standards Compliance:
        - Uses BlueZ D-Bus API patterns (via bluetoothctl)
        - Follows Bluetooth Core Specification for pairing
        - Implements security best practices (no shell injection)
        - Validates all inputs (MAC addresses, commands)
        - Proper resource management (subprocess cleanup)
    """
    
    PIN_CONF_PATHS = [
        "/etc/bluetooth/pin.conf",
        str(pathlib.Path(__file__).parent.parent.parent / "etc/bluetooth/pin.conf")
    ]
    
    # MAC address validation regex (XX:XX:XX:XX:XX:XX format)
    MAC_ADDRESS_REGEX = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')
    
    @staticmethod
    def _validate_mac_address(address: str) -> bool:
        """
        Validate MAC address format.
        
        Args:
            address: MAC address string to validate
            
        Returns:
            True if valid format, False otherwise
        """
        return RfcommManager.MAC_ADDRESS_REGEX.match(address) is not None
    
    @staticmethod
    def _find_bluetoothctl_path() -> str:
        """
        Find bluetoothctl executable path.
        
        Returns:
            Path to bluetoothctl executable
        """
        path = shutil.which('bluetoothctl') or '/usr/bin/bluetoothctl'
        return path
    
    def __init__(self, device_name: str = "Chess Link", use_external_agent: bool = False):
        """
        Initialize Bluetooth controller.
        
        Args:
            device_name: Name to use for the Bluetooth device (max 248 chars per Bluetooth spec)
            use_external_agent: If True, this manager does NOT spawn ``bt-agent``;
                pairing requests are serviced by an externally-registered BlueZ
                agent (the application's KeyboardDisplay D-Bus agent). The pairing
                thread then only maintains discoverability. This is required so
                Bluetooth keyboards can complete passkey pairing: ``bt-agent``
                registers itself as the *default* agent and lacks the
                ``KeyboardDisplay`` capability, so leaving it running would
                prevent the host from displaying a passkey.
            
        Raises:
            ValueError: If device_name is invalid
        """
        # Validate device name at initialization
        if not device_name or len(device_name) > 248:
            raise ValueError(f"Invalid device name: {device_name} (must be 1-248 characters)")
        
        shell_metachars = [';', '&', '|', '`', '$', '(', ')', '<', '>', '\n', '\r']
        if any(char in device_name for char in shell_metachars):
            raise ValueError(f"Device name contains invalid characters: {device_name}")
        
        self.device_name = device_name
        self._use_external_agent = use_external_agent
        self._discovery_thread: Optional[threading.Thread] = None
        self._pairing_thread: Optional[threading.Thread] = None
        self._discovery_running = False
        self._stop_event = threading.Event()  # Interruptible stop signal for pairing thread
    
    def __enter__(self):
        """Context manager entry - enables Bluetooth automatically"""
        self.enable_bluetooth()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - stops threads and cleans up"""
        self.stop_pairing_thread()
        return False  # Don't suppress exceptions
    
    @classmethod
    def _create_bluetoothctl_process(cls) -> subprocess.Popen:
        """
        Create a bluetoothctl subprocess with standard configuration.
        
        Returns:
            Configured subprocess.Popen object
            
        Raises:
            FileNotFoundError: If bluetoothctl is not found
            OSError: If process creation fails
        """
        bluetoothctl_path = cls._find_bluetoothctl_path()
        try:
            return subprocess.Popen(
                [bluetoothctl_path],
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                shell=False  # Security: avoid shell injection
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"bluetoothctl not found at {bluetoothctl_path}")
    
    @staticmethod
    def _send_bluetoothctl_commands(process: subprocess.Popen, commands: List[str], wait_time: float = 2.0):
        """
        Send commands to bluetoothctl process and wait.
        
        Args:
            process: bluetoothctl subprocess
            commands: List of command strings to send
            wait_time: Seconds to wait after sending commands (default 2.0s ensures
                      commands complete on slower hardware like Pi Zero)
            
        Raises:
            ValueError: If commands contain invalid characters
            BrokenPipeError: If process pipe is closed
        """
        # Security: Validate commands don't contain shell metacharacters
        shell_metachars = [';', '&', '|', '`', '$', '(', ')', '<', '>', '\n', '\r']
        for cmd in commands:
            if any(char in cmd for char in shell_metachars):
                raise ValueError(f"Invalid characters in command: {cmd}")
            process.stdin.write(f"{cmd}\n")
            process.stdin.flush()
        time.sleep(wait_time)
    
    @staticmethod
    def _read_bluetoothctl_output(process: subprocess.Popen, timeout: float, 
                                  line_processor: Optional[Callable[[str], bool]] = None) -> List[str]:
        """
        Read output from bluetoothctl process using polling.
        
        Args:
            process: bluetoothctl subprocess
            timeout: Maximum time to read (seconds)
            line_processor: Optional function to process each line. Returns True to continue, False to stop.
            
        Returns:
            List of lines read
        """
        lines: List[str] = []

        # Prefer poll-based reading for real subprocess pipes, but fall back to a simple
        # readline loop for test doubles / non-file-descriptor streams.
        poll_obj: Optional[select.poll] = None
        try:
            poll_obj = select.poll()
            poll_obj.register(process.stdout, select.POLLIN)
        except Exception:
            poll_obj = None

        start_time = time.time()
        while time.time() - start_time < timeout:
            if poll_obj is not None:
                poll_result = poll_obj.poll(100)  # 100ms timeout
                if not poll_result:
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue

            line = process.stdout.readline()
            if not line:
                break

            # In universal_newlines mode, subprocess returns str; tests may feed bytes.
            if isinstance(line, bytes):
                try:
                    line = line.decode("utf-8", errors="ignore")
                except Exception:
                    line = ""

            lines.append(line)

            if line_processor:
                if not line_processor(line):
                    break
            elif line.strip().startswith("[") and "]#" in line:
                # Reached prompt, done reading
                break

            if process.poll() is not None:
                break

        return lines
    
    # Strips the ANSI color codes that interactive bluetoothctl wraps around
    # its [NEW]/[CHG]/[DEL] event tags.
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
    _MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
    _BTMGMT_FOUND_RE = re.compile(
        r"^hci\d+\s+dev_found:\s+"
        r"(?P<address>(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+"
        r"type\s+(?P<type>\S+)"
    )
    _HCI_INQUIRY_CLASS_RE = re.compile(
        r"(?P<address>(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}).*"
        r"class:\s+(?P<class>0x[0-9A-Fa-f]+|\d+)"
    )

    @staticmethod
    def _parse_hex_int(value: str) -> Optional[int]:
        """Parse the leading integer token of a bluetoothctl property value.

        Values look like ``0x000540`` (Class) or ``0x03c1 (961)`` (Appearance);
        only the first token is significant. Returns None if it is not an
        integer so callers can simply skip unparseable values.
        """
        token = value.strip().split(" ", 1)[0] if value else ""
        if not token:
            return None
        try:
            return int(token, 16) if token.lower().startswith("0x") else int(token)
        except ValueError:
            return None

    @staticmethod
    def _parse_device_fields(line: str) -> Optional[Dict[str, object]]:
        """Extract the device address and any single field from a bluetoothctl line.

        Interactive bluetoothctl emits, with ANSI-colored ``[NEW]``/``[CHG]``/
        ``[DEL]`` event tags:
          * ``Device <mac> <name>``            (a NEW device's friendly name)
          * ``Device <mac> <Key>: <value>``    (a property update)
        and bare ``Device <mac> <name>`` lines for ``devices Paired`` listings.

        The tag and ANSI codes are stripped, the MAC is validated, and the line
        is reduced to ``{'address': ...}`` plus at most one recognised field:
        ``name`` (from a friendly-name line or a ``Name:`` update), ``icon``,
        ``cod`` (Class of Device, int), or ``appearance`` (int). Unrecognised
        property updates (``RSSI:``, ``AdvertisingFlags:``, ...) yield only the
        address, which lets discovery track type without treating them as names.

        Returns None if the line does not describe a ``Device`` with a valid MAC.
        """
        clean = RfcommManager._ANSI_RE.sub("", line).strip()

        # Drop a leading event tag like "[NEW]"/"[CHG]"/"[DEL]" if present.
        if clean.startswith("["):
            end = clean.find("]")
            if end == -1:
                return None
            clean = clean[end + 1:].strip()

        if not clean.startswith("Device "):
            return None

        parts = clean.split(" ", 2)
        if len(parts) < 2:
            return None

        device_addr = parts[1]
        if not RfcommManager._MAC_RE.match(device_addr):
            return None

        fields: Dict[str, object] = {'address': device_addr}
        remainder = parts[2].strip() if len(parts) > 2 else ""
        if not remainder:
            return fields

        # A property-update line's first token ends with ':' (e.g. "Name:",
        # "Icon:", "Class:", "Appearance:", "RSSI:"). A bare friendly name does
        # not. Recognise only the fields that identify a device or its type.
        first_token = remainder.split(" ", 1)[0]
        if first_token.endswith(":"):
            key = first_token[:-1]
            value = remainder[len(first_token):].strip()
            if key == "Name":
                fields['name'] = value or "Unknown"
            elif key == "Icon":
                if value:
                    fields['icon'] = value
            elif key == "Class":
                cod = RfcommManager._parse_hex_int(value)
                if cod is not None:
                    fields['cod'] = cod
            elif key == "Appearance":
                appearance = RfcommManager._parse_hex_int(value)
                if appearance is not None:
                    fields['appearance'] = appearance
            # Other property updates contribute only the address.
        else:
            fields['name'] = remainder

        return fields

    @staticmethod
    def _parse_device_line(line: str) -> Optional[Dict[str, str]]:
        """Parse a bluetoothctl line into ``{'address', 'name'}`` or None.

        Thin wrapper over :meth:`_parse_device_fields` for callers that only
        care about address+name (e.g. paired-device listings). Lines that name a
        device but carry no usable name (RSSI/flags/Class/etc. updates) return
        None so they are not mistaken for a named device.

        Documented case (regression guard): a parser that only matched bare
        ``Device`` lines listed zero devices during a scan, because every scan
        line is prefixed with an ANSI-colored ``[NEW]``/``[CHG]`` tag.
        """
        fields = RfcommManager._parse_device_fields(line)
        if not fields or 'name' not in fields:
            return None
        return {'address': fields['address'], 'name': str(fields['name'])}

    @staticmethod
    def _parse_btmgmt_find_output(output: str) -> List[Dict[str, object]]:
        """Parse ``btmgmt find -b`` output into discovered BR/EDR devices.

        The board can see some Classic HID devices (notably WiFi Key) via the
        BlueZ management scan even when ``bluetoothctl scan on`` never emits a
        matching D-Bus discovery event. ``btmgmt`` reports the address on a
        ``dev_found`` line and the friendly name on a following ``name`` line,
        so parsing must keep only the most recent valid BR/EDR device pending
        until its name arrives.
        """
        devices: List[Dict[str, object]] = []
        by_address: Dict[str, Dict[str, object]] = {}
        pending_address: Optional[str] = None

        for raw_line in output.splitlines():
            line = RfcommManager._ANSI_RE.sub("", raw_line).strip()
            if not line:
                continue

            match = RfcommManager._BTMGMT_FOUND_RE.match(line)
            if match:
                if match.group("type") == "BR/EDR":
                    pending_address = match.group("address").upper()
                else:
                    pending_address = None
                continue

            if line.startswith("name ") and pending_address:
                name = line[len("name "):].strip()
                if not name:
                    pending_address = None
                    continue
                record = by_address.get(pending_address)
                if record is None:
                    record = {"address": pending_address, "name": name}
                    by_address[pending_address] = record
                    devices.append(record)
                elif RfcommManager._is_placeholder_name(
                        str(record.get("name") or ""), pending_address):
                    record["name"] = name
                pending_address = None

        return devices

    @staticmethod
    def _parse_hci_inquiry_classes(output: str) -> Dict[str, int]:
        """Parse Class-of-Device values from controller inquiry output.

        ``btmgmt find -b`` provides the device name/address but not the remote
        Class-of-Device. The keyboard-only menu needs that class for Classic HID
        devices, so a controller inquiry is used only to enrich records with the
        type signal that the filter already understands.
        """
        classes: Dict[str, int] = {}
        for raw_line in output.splitlines():
            line = RfcommManager._ANSI_RE.sub("", raw_line).strip()
            match = RfcommManager._HCI_INQUIRY_CLASS_RE.search(line)
            if not match:
                continue
            cod = RfcommManager._parse_hex_int(match.group("class"))
            if cod is not None:
                classes[match.group("address").upper()] = cod
        return classes

    @staticmethod
    def _merge_discovery_records(
            primary: List[Dict[str, object]],
            secondary: List[Dict[str, object]],
            cod_by_address: Dict[str, int]) -> List[Dict[str, object]]:
        """Merge discovery records by address and enrich them with CoD data."""
        merged: List[Dict[str, object]] = []
        by_address: Dict[str, Dict[str, object]] = {}

        def merge_one(device: Dict[str, object]) -> None:
            address = str(device.get("address") or "").upper()
            if not RfcommManager._MAC_RE.match(address):
                return

            record = by_address.get(address)
            if record is None:
                record = {"address": address}
                by_address[address] = record
                merged.append(record)

            name = str(device.get("name") or "").strip()
            if name and (
                    "name" not in record
                    or RfcommManager._is_placeholder_name(
                        str(record.get("name") or ""), address)
                    and not RfcommManager._is_placeholder_name(name, address)):
                record["name"] = name

            for type_key in ("icon", "appearance", "cod"):
                if type_key in device:
                    record[type_key] = device[type_key]

            if address in cod_by_address and "cod" not in record:
                record["cod"] = cod_by_address[address]

        for device in primary:
            merge_one(device)
        for device in secondary:
            merge_one(device)

        return [record for record in merged if "name" in record]

    @staticmethod
    def is_keyboard_device(device: Dict[str, object]) -> bool:
        """Classify a discovered device as a keyboard from its BlueZ type fields.

        Uses, in order of authority:
          * ``icon`` == ``input-keyboard`` (BlueZ derives this from
            class/appearance, so it is the most reliable signal);
          * BLE ``appearance``: HID category (bits 6-15 == 0x00F) with the
            keyboard sub-type (bits 0-5 == 0x01), i.e. 0x03C1;
          * Classic ``cod`` (Class of Device): Peripheral major class
            (bits 8-12 == 0x05) with the keyboard bit set (0x40).

        A device with none of these type signals is NOT classified as a keyboard
        (the "Pair Keyboard" list intentionally shows only keyboards). A pointing
        device (cod bit 0x80, appearance sub-type 0x02, icon ``input-mouse``)
        therefore returns False.
        """
        icon = str(device.get("icon") or "").strip().lower()
        if icon:
            return icon == "input-keyboard"

        appearance = device.get("appearance")
        if isinstance(appearance, int):
            return (appearance >> 6) == 0x00F and (appearance & 0x3F) == 0x01

        cod = device.get("cod")
        if isinstance(cod, int):
            major_device_class = (cod >> 8) & 0x1F
            return major_device_class == 0x05 and bool(cod & 0x40)

        return False
    
    @staticmethod
    def _is_placeholder_name(name: str, address: str) -> bool:
        """Return True when ``name`` is a stand-in, not a real friendly name.

        bluetoothctl substitutes a MAC-derived placeholder when a device has no
        advertised name: either the address itself (``64:db:a0:..``) or the
        address with colons replaced by dashes (``64-DB-A0-..``). "Unknown" is
        also treated as a placeholder. Used to decide whether a later ``Name:``
        update should overwrite a previously stored name.
        """
        if not name or name == "Unknown":
            return True
        normalized = name.upper()
        addr_upper = address.upper()
        return normalized == addr_upper or normalized == addr_upper.replace(":", "-")

    @staticmethod
    def _safe_terminate(p: Optional[subprocess.Popen]):
        """
        Safely terminate a subprocess by closing pipes before termination.
        Follows industry best practices for subprocess resource management.
        
        Args:
            p: subprocess.Popen object to terminate (None is handled gracefully)
        """
        if p is None:
            return
        
        try:
            # Close stdin if it exists and isn't already closed
            if p.stdin and not p.stdin.closed:
                try:
                    p.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            
            # Close stdout if it exists and isn't already closed
            if p.stdout and not p.stdout.closed:
                try:
                    p.stdout.close()
                except (BrokenPipeError, OSError):
                    pass
            
            # Close stderr if it exists and isn't already closed
            if p.stderr and not p.stderr.closed:
                try:
                    p.stderr.close()
                except (BrokenPipeError, OSError):
                    pass
            
            # Terminate the process gracefully
            try:
                p.terminate()
            except ProcessLookupError:
                # Process already terminated
                return
            
            # Wait for process to exit (with timeout)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Force kill if it doesn't terminate gracefully
                try:
                    p.kill()
                    p.wait(timeout=1)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    pass
        except (ProcessLookupError, ValueError):
            # Process already terminated or invalid
            pass

    @staticmethod
    def _run_root_capable_command(
            command: List[str], timeout: int) -> subprocess.CompletedProcess:
        """Run a Bluetooth diagnostic command without ever prompting for sudo.

        The chess service commonly runs with enough privilege to call BlueZ
        management commands directly. Developer shells often do not, so a
        passwordless ``sudo -n`` retry is allowed. ``-n`` is intentional: a
        pairing scan must fail fast instead of hanging behind an invisible
        password prompt on the board.
        """
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout.decode("utf-8", errors="ignore") \
                if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode("utf-8", errors="ignore") \
                if isinstance(e.stderr, bytes) else (e.stderr or "")
            result = subprocess.CompletedProcess(
                command, 124, stdout=stdout, stderr=stderr)
        except PermissionError:
            result = None

        if result is not None and result.returncode == 0:
            return result

        sudo = shutil.which("sudo")
        if sudo is None:
            if result is not None:
                return result
            raise PermissionError(f"Permission denied running {command[0]}")
        sudo_command = [sudo, "-n", *command]
        try:
            return subprocess.run(
                sudo_command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout.decode("utf-8", errors="ignore") \
                if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode("utf-8", errors="ignore") \
                if isinstance(e.stderr, bytes) else (e.stderr or "")
            return subprocess.CompletedProcess(
                sudo_command, 124, stdout=stdout, stderr=stderr)

    def _discover_bredr_with_btmgmt(self, timeout: int) -> List[Dict[str, object]]:
        """Discover Classic BR/EDR devices through BlueZ management."""
        btmgmt = shutil.which("btmgmt")
        if btmgmt is None:
            return []

        try:
            result = self._run_root_capable_command(
                [btmgmt, "find", "-b"],
                timeout=timeout + 3,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.debug(f"btmgmt BR/EDR discovery unavailable: {e}")
            return []

        if result.returncode not in (0, 124):
            error = (result.stderr or result.stdout or "").strip()
            log.debug(f"btmgmt BR/EDR discovery failed: {error}")
            return []

        return self._parse_btmgmt_find_output(result.stdout)

    def _read_controller_inquiry_classes(self, timeout: int) -> Dict[str, int]:
        """Read Classic Class-of-Device values from a controller inquiry."""
        hcitool = shutil.which("hcitool")
        if hcitool is None:
            return {}

        try:
            result = self._run_root_capable_command(
                [hcitool, "inq"],
                timeout=timeout + 3,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.debug(f"HCI inquiry unavailable for CoD enrichment: {e}")
            return {}

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            log.debug(f"HCI inquiry failed for CoD enrichment: {error}")
            return {}

        return self._parse_hci_inquiry_classes(result.stdout)

    def _resolve_classic_device_name(self, address: str) -> Optional[str]:
        """Resolve a Classic Bluetooth friendly name by address."""
        hcitool = shutil.which("hcitool")
        if hcitool is None:
            return None

        try:
            result = subprocess.run(
                [hcitool, "name", address],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.debug(f"HCI name lookup failed for {address}: {e}")
            return None

        if result.returncode != 0:
            return None
        name = result.stdout.strip()
        return name or None

    def _discover_keyboards_from_inquiry_classes(
            self, cod_by_address: Dict[str, int]) -> List[Dict[str, object]]:
        """Build keyboard records from inquiry CoD when btmgmt is unavailable.

        This is deliberately limited to devices whose Class-of-Device already
        classifies as a keyboard. The name lookup only makes the known keyboard
        selectable in the UI; it is not used as the type signal.
        """
        devices: List[Dict[str, object]] = []
        for address, cod in cod_by_address.items():
            candidate = {"address": address, "cod": cod}
            if not self.is_keyboard_device(candidate):
                continue
            name = self._resolve_classic_device_name(address)
            if not name:
                continue
            devices.append({"address": address, "name": name, "cod": cod})
        return devices

    def discover_keyboards(self, timeout: int = 8) -> List[Dict[str, object]]:
        """Discover Bluetooth keyboards using the fastest reliable path first.

        Keyboard pairing does not need the full list of nearby Bluetooth
        devices. Classic keyboard Class-of-Device is enough to identify the
        target, so try controller inquiry + name lookup first and return as soon
        as it finds a named keyboard. Fall back to the broad scan only when the
        keyboard-specific path finds nothing.
        """
        cod_by_address = self._read_controller_inquiry_classes(timeout)
        keyboards = self._discover_keyboards_from_inquiry_classes(cod_by_address)
        if keyboards:
            log.info(
                f"Discovered {len(keyboards)} keyboard(s) "
                "via inquiry-keyboard fast path"
            )
            return keyboards

        devices = self._discover_with_bluetoothctl(timeout=4)
        keyboards = [d for d in devices if self.is_keyboard_device(d)]
        log.info(
            f"Discovered {len(keyboards)} keyboard(s) "
            f"via broad fallback ({len(devices)} device(s))"
        )
        return keyboards

    def discover_keyboards_broad_fallback(self, timeout: int = 4) -> List[Dict[str, object]]:
        """Discover keyboards from bluetoothctl's broad scan stream.

        Used as a background supplement after the fast keyboard inquiry has
        already produced the initial menu. This may find keyboards that BlueZ
        reports by icon/appearance rather than Classic Class-of-Device.
        """
        devices = self._discover_with_bluetoothctl(timeout=timeout)
        keyboards = [d for d in devices if self.is_keyboard_device(d)]
        log.info(
            f"Discovered {len(keyboards)} keyboard(s) "
            f"via broad supplemental scan ({len(devices)} device(s))"
        )
        return keyboards
    
    @staticmethod
    def kill_bt_agent():
        """Kill any running bt-agent processes"""
        killed_count = 0
        for p in _process_iter(attrs=['pid', 'name']):
            try:
                if "bt-agent" in p.info["name"]:
                    log.info(f"[RfcommManager] Killing bt-agent process {p.info['pid']}")
                    p.kill()
                    killed_count += 1
                    time.sleep(1)
            except Exception as e:
                if _is_psutil_exception(e):
                    log.warning(f"[RfcommManager] Error killing bt-agent: {e}")
                else:
                    raise
        if killed_count > 0:
            log.info(f"[RfcommManager] Killed {killed_count} bt-agent process(es)")
        else:
            log.info("[RfcommManager] No bt-agent processes found")
    
    def enable_bluetooth(self):
        """
        Enable Bluetooth and make device discoverable and pairable.
        
        Raises:
            subprocess.SubprocessError: If bluetoothctl command fails
            OSError: If process creation fails
        """
        try:
            p = self._create_bluetoothctl_process()
            self._send_bluetoothctl_commands(p, ["power on", "discoverable on", "pairable on"])
            RfcommManager._safe_terminate(p)
            log.info("Bluetooth enabled and made discoverable")
        except (subprocess.SubprocessError, OSError) as e:
            log.error(f"Error enabling Bluetooth: {e}")
            raise
    
    def disable_bluetooth(self):
        """
        Disable Bluetooth.
        
        Raises:
            subprocess.SubprocessError: If bluetoothctl command fails
            OSError: If process creation fails
        """
        try:
            p = self._create_bluetoothctl_process()
            self._send_bluetoothctl_commands(p, ["power off"], wait_time=1.0)
            RfcommManager._safe_terminate(p)
            log.info("Bluetooth disabled")
        except (subprocess.SubprocessError, OSError) as e:
            log.error(f"Error disabling Bluetooth: {e}")
            raise
    
    def set_device_name(self, name: str):
        """
        Set the Bluetooth device name.
        
        Args:
            name: Name to set for the Bluetooth device
            
        Raises:
            ValueError: If name contains invalid characters
            subprocess.SubprocessError: If bluetoothctl command fails
            OSError: If process creation fails
        """
        # Validate device name (prevent command injection)
        if not name or len(name) > 248:  # Bluetooth name limit
            raise ValueError(f"Invalid device name: {name}")
        
        # Check for shell metacharacters
        shell_metachars = [';', '&', '|', '`', '$', '(', ')', '<', '>', '\n', '\r']
        if any(char in name for char in shell_metachars):
            raise ValueError(f"Device name contains invalid characters: {name}")
        
        try:
            p = self._create_bluetoothctl_process()
            self._send_bluetoothctl_commands(p, ["power on", f"system-alias {name}"], wait_time=1.0)
            RfcommManager._safe_terminate(p)
            self.device_name = name
            log.info(f"Bluetooth device name set to: {name}")
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log.error(f"Error setting device name: {e}")
            raise
    
    def keep_discoverable(self, device_name: Optional[str] = None):
        """
        Keep Bluetooth device discoverable and set device name.
        This is critical for iPhone compatibility as iPhones need the device
        to be discoverable during the entire pairing window.
        
        Args:
            device_name: Name to set for the Bluetooth device (uses instance default if None)
            
        Raises:
            ValueError: If device_name contains invalid characters
            subprocess.SubprocessError: If bluetoothctl command fails
            OSError: If process creation fails
        """
        name = device_name or self.device_name
        
        # Validate device name
        if name and len(name) > 248:
            raise ValueError(f"Device name too long: {name}")
        
        try:
            p = self._create_bluetoothctl_process()
            # Use discoverable on with no timeout for indefinite discoverability
            # This ensures iPhone can discover the device throughout the pairing window
            # Also ensures Android devices can discover during scanning
            self._send_bluetoothctl_commands(
                p, 
                ["power on", f"system-alias {name}", "discoverable on", "pairable on"]
            )
            RfcommManager._safe_terminate(p)
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log.debug(f"Error keeping Bluetooth discoverable: {e}")
            # Don't raise - this is a maintenance function that may fail occasionally
    
    @staticmethod
    def get_pin_conf_path() -> Optional[str]:
        """Find pin.conf file in standard locations"""
        for path in RfcommManager.PIN_CONF_PATHS:
            if pathlib.Path(path).exists():
                return path
        return None
    
    def _discover_with_bluetoothctl(
            self,
            timeout: int,
            on_device_found: Optional[Callable[[str, str], None]] = None,
    ) -> List[Dict[str, object]]:
        """Discover devices from bluetoothctl's live scan stream only."""
        discovered_devices: List[Dict[str, object]] = []
        device_map: Dict[str, Dict[str, object]] = {}

        p = self._create_bluetoothctl_process()
        p.stdin.write("scan on\n")
        p.stdin.flush()

        def process_discovery_line(line: str) -> bool:
            """Process a line from discovery output.

            Device identity and type arrive across several lines: a ``[NEW]``
            line (often with a MAC-derived placeholder name), then ``[CHG]``
            lines carrying ``Name:``, ``Icon:``, ``Class:`` or
            ``Appearance:``. Each address gets one record that is enriched in
            place as more lines arrive: a real name replaces a placeholder,
            and type fields (icon/cod/appearance) are merged so the device
            can later be classified (e.g. as a keyboard). Duplicates are not
            appended.
            """
            fields = self._parse_device_fields(line)
            if not fields:
                return True  # Continue reading

            device_addr = fields['address']
            record = device_map.get(device_addr)
            if record is None:
                # A device enters the list on its first name-bearing line
                # (the [NEW] line, which always carries at least a
                # MAC-placeholder name). Type-only updates (RSSI/Class/etc.)
                # for an as-yet-unseen address are ignored to avoid phantom
                # "Unknown" entries from stray [CHG] lines.
                if 'name' not in fields:
                    return True
                record = {'address': device_addr, 'name': str(fields['name'])}
                device_map[device_addr] = record
                discovered_devices.append(record)
                if on_device_found:
                    try:
                        on_device_found(device_addr, record['name'])
                    except Exception as e:
                        log.error(f"Error in on_device_found callback: {e}")

            # Refine a placeholder name once a real one is learned.
            new_name = fields.get('name')
            if new_name and self._is_placeholder_name(record['name'], device_addr) \
                    and not self._is_placeholder_name(str(new_name), device_addr):
                record['name'] = str(new_name)

            # Merge type signals used for keyboard classification.
            for type_key in ('icon', 'cod', 'appearance'):
                if type_key in fields:
                    record[type_key] = fields[type_key]
            return True  # Continue reading

        self._read_bluetoothctl_output(p, timeout, process_discovery_line)

        p.stdin.write("scan off\n")
        p.stdin.flush()
        time.sleep(0.5)
        RfcommManager._safe_terminate(p)
        return discovered_devices

    def start_discovery(self, timeout: int = 30, on_device_found: Optional[Callable[[str, str], None]] = None) -> List[Dict[str, object]]:
        """
        Start discovering nearby Bluetooth devices.
        
        Args:
            timeout: Seconds to scan for devices (default: 30)
            on_device_found: Optional callback(device_addr, device_name) when device found
            
        Returns:
            List of discovered devices with 'address' and 'name' keys
        """
        discovered_devices: List[Dict[str, object]] = []
        try:
            # Some Classic HID devices (observed with WiFi Key) answer BlueZ
            # management/controller inquiry but are not surfaced by
            # bluetoothctl's D-Bus scan path. Run the controller-level pass
            # first so it observes the adapter before bluetoothctl discovery
            # changes scan state.
            btmgmt_devices = self._discover_bredr_with_btmgmt(timeout)
            cod_by_address = self._read_controller_inquiry_classes(timeout)
            inquiry_keyboards = self._discover_keyboards_from_inquiry_classes(
                cod_by_address)

            discovered_devices = self._discover_with_bluetoothctl(
                timeout, on_device_found)
            
            merged_devices = self._merge_discovery_records(
                discovered_devices,
                btmgmt_devices + inquiry_keyboards,
                cod_by_address)

            log.info(
                f"Discovered {len(merged_devices)} devices "
                f"({len(discovered_devices)} bluetoothctl, "
                f"{len(btmgmt_devices)} btmgmt, "
                f"{len(inquiry_keyboards)} inquiry-keyboards)"
            )
            return merged_devices
            
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log.error(f"Error during device discovery: {e}")
            return discovered_devices
    
    def start_pairing(self, timeout: int = 60, on_device_detected: Optional[Callable[[], None]] = None) -> bool:
        """
        Start Bluetooth pairing mode.
        Compatible with both Android and iPhone devices.
        
        For Android:
            - Maintains discoverability throughout pairing
            - Uses NoInputNoOutput capability for seamless pairing
        
        For iPhone:
            - Keeps device discoverable indefinitely
            - Handles iPhone's pairing requirements
        
        Args:
            timeout: Seconds to wait for pairing (0 = infinite)
            on_device_detected: Optional callback when pairing device is detected
            
        Returns:
            True if device paired successfully, False if timeout
        """
        self.kill_bt_agent()
        self.enable_bluetooth()
        
        # Keep device discoverable from the start (critical for iPhone)
        self.keep_discoverable()
        
        pin_conf = self.get_pin_conf_path()
        if not pin_conf:
            log.warning("Warning: pin.conf not found, using NoInputNoOutput")
            cmd = ['/usr/bin/bt-agent', '--capability=NoInputNoOutput']
        else:
            cmd = ['/usr/bin/bt-agent', '--capability=NoInputNoOutput', '-p', pin_conf]
        
        try:
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
                shell=False  # Security: avoid shell injection
            )
            poll_obj = select.poll()
            poll_obj.register(p.stdout, select.POLLIN)
            
            start_time = time.time()
            running = True
            spamyes = False
            spamtime = 0
            
            while running:
                # Check timeout
                if timeout > 0 and time.time() - start_time > timeout:
                    RfcommManager._safe_terminate(p)
                    log.info("Pairing timeout")
                    return False
                
                poll_result = poll_obj.poll(0)
                
                if spamyes:
                    if time.time() - spamtime < 3:
                        p.stdin.write(b'yes\n')
                        p.stdin.flush()
                        time.sleep(1)
                    else:
                        # Pairing succeeded - don't terminate bt-agent, let it keep running
                        # The RFCOMM connection needs bt-agent to stay active
                        log.info("Pairing completed successfully, bt-agent will remain running")
                        # Keep device discoverable after pairing (for applications like Hiarcs)
                        self.keep_discoverable()
                        return True
                
                if poll_result and not spamyes:
                    line = p.stdout.readline()
                    if b'Device:' in line:
                        log.info("Device detected, pairing...")
                        p.stdin.write(b'yes\n')
                        p.stdin.flush()
                        if on_device_detected:
                            try:
                                on_device_detected()
                            except Exception as e:
                                log.error(f"Error in on_device_detected callback: {e}")
                        spamyes = True
                        spamtime = time.time()
                
                r = p.poll()
                if r is not None:
                    running = False
                
                time.sleep(0.1)
            
            return False
            
        except (subprocess.SubprocessError, OSError) as e:
            log.error(f"Error during pairing: {e}")
            return False
    
    def _check_bt_agent_running(self) -> bool:
        """
        Check if bt-agent process is currently running.
        
        Returns:
            True if bt-agent is running, False otherwise
        """
        for p in _process_iter(attrs=['pid', 'name']):
            if "bt-agent" in p.info["name"]:
                return True
        return False
    
    def _maintain_discoverability(self, last_check: float, interval: float = 30.0) -> float:
        """
        Maintain device discoverability at regular intervals.
        
        Args:
            last_check: Timestamp of last discoverability check
            interval: Seconds between discoverability updates
            
        Returns:
            Updated timestamp of last check
        """
        current_time = time.time()
        if current_time - last_check > interval:
            self.keep_discoverable()
            return current_time
        return last_check
    
    def start_pairing_thread(self, timeout: int = 0):
        """
        Run pairing in background thread (for continuous pairing support).
        Useful for eboard/millennium modes where pairing should be available continuously.
        
        Args:
            timeout: Seconds to wait for each pairing attempt (0 = infinite)
            
        Returns:
            Thread object for the pairing thread
        """
        self._stop_event.clear()
        
        def discoverability_loop():
            """Maintain discoverability when an external agent services pairing.

            Used in ``use_external_agent`` mode: bt-agent is NOT started (the
            application's KeyboardDisplay D-Bus agent handles pairing). Any
            straggler bt-agent from a previous run is killed first so it cannot
            override the default agent and suppress passkey display.
            """
            self.kill_bt_agent()
            if self._stop_event.wait(2.5):
                return
            self.keep_discoverable()
            last_check = time.time()
            while not self._stop_event.is_set():
                if self._stop_event.wait(10):
                    return
                last_check = self._maintain_discoverability(last_check)

        if self._use_external_agent:
            thread = threading.Thread(target=discoverability_loop, daemon=True)
            thread.start()
            self._pairing_thread = thread
            return thread

        def pair_loop():
            # Small delay to ensure bt-agent has started from start_pairing() before
            # we call keep_discoverable(). This prevents bluetoothctl commands from
            # interfering with bt-agent's initial pairing setup
            # Use Event.wait() for interruptible sleep
            if self._stop_event.wait(2.5):
                return  # Stop requested during initial delay
            
            # Keep device discoverable from the start, not just after pairing
            # This ensures Android devices can discover the service during scanning
            # and iPhone devices can discover throughout the pairing window
            self.keep_discoverable()
            last_discoverable_check = time.time()
            
            while not self._stop_event.is_set():
                paired = self.start_pairing(timeout=timeout)  # Run indefinitely if timeout=0
                if paired:
                    # Pairing succeeded - bt-agent is still running
                    # Check periodically if it's still running, only restart if it exits
                    # Also keep device discoverable so applications like Hiarcs can find it
                    log.info("Pairing succeeded, monitoring bt-agent status and keeping discoverable")
                    # Set discoverable immediately after pairing
                    self.keep_discoverable()
                    last_discoverable_check = time.time()
                    while not self._stop_event.is_set():
                        # Use Event.wait() for interruptible 10-second sleep
                        if self._stop_event.wait(10):
                            return  # Stop requested
                        # Keep device discoverable every 30 seconds
                        last_discoverable_check = self._maintain_discoverability(last_discoverable_check)
                        
                        if not self._check_bt_agent_running():
                            log.info("bt-agent exited, restarting pairing")
                            break
                else:
                    # Pairing failed or timed out - restart quickly
                    # Keep device discoverable during retry
                    last_discoverable_check = self._maintain_discoverability(last_discoverable_check)
                # Use Event.wait() for interruptible short sleep
                if self._stop_event.wait(0.1):
                    return  # Stop requested
        
        thread = threading.Thread(target=pair_loop, daemon=True)
        thread.start()
        self._pairing_thread = thread
        return thread
    
    def stop_pairing_thread(self):
        """Stop the pairing thread and kill bt-agent process.
        
        Uses Event to immediately interrupt any sleep and signal the thread to exit.
        Also kills any running bt-agent processes to ensure clean shutdown.
        """
        log.info("[RfcommManager] Stopping pairing thread...")
        self._stop_event.set()
        if self._pairing_thread and self._pairing_thread.is_alive():
            log.info("[RfcommManager] Waiting for pairing thread to exit...")
            self._pairing_thread.join(timeout=0.5)  # Brief wait, thread should exit immediately
            if self._pairing_thread.is_alive():
                log.warning("[RfcommManager] Pairing thread did not exit within timeout")
            else:
                log.info("[RfcommManager] Pairing thread exited")
        else:
            log.info("[RfcommManager] Pairing thread was not running")
        
        # Kill bt-agent process to ensure clean shutdown
        log.info("[RfcommManager] Killing bt-agent processes...")
        self.kill_bt_agent()
        log.info("[RfcommManager] Stop complete")
    
    def get_paired_devices(self) -> List[Dict[str, str]]:
        """
        Get list of currently paired devices.
        
        Returns:
            List of paired devices with 'address' and 'name' keys
        """
        paired_devices = []
        
        try:
            p = self._create_bluetoothctl_process()
            p.stdin.write("paired-devices\n")
            p.stdin.flush()
            
            def process_paired_line(line: str) -> bool:
                """Process a line from paired-devices output"""
                device_info = self._parse_device_line(line)
                if device_info:
                    paired_devices.append(device_info)
                return True  # Continue reading
            
            self._read_bluetoothctl_output(p, timeout=5.0, line_processor=process_paired_line)
            RfcommManager._safe_terminate(p)
            
            log.debug(f"Found {len(paired_devices)} paired devices")
            return paired_devices
            
        except (subprocess.SubprocessError, OSError) as e:
            log.error(f"Error getting paired devices: {e}")
            return paired_devices
    
    def get_known_devices(self) -> List[Dict[str, str]]:
        """
        Get list of all known devices (paired and previously seen).
        
        Returns:
            List of known devices with 'address' and 'name' keys
        """
        known_devices = []
        
        try:
            p = self._create_bluetoothctl_process()
            p.stdin.write("devices\n")
            p.stdin.flush()
            
            def process_device_line(line: str) -> bool:
                """Process a line from devices output"""
                device_info = self._parse_device_line(line)
                if device_info:
                    known_devices.append(device_info)
                return True  # Continue reading
            
            self._read_bluetoothctl_output(p, timeout=5.0, line_processor=process_device_line)
            RfcommManager._safe_terminate(p)
            
            log.debug(f"Found {len(known_devices)} known devices")
            return known_devices
            
        except (subprocess.SubprocessError, OSError) as e:
            log.error(f"Error getting known devices: {e}")
            return known_devices
    
    def find_device_by_name(self, name: str) -> Optional[str]:
        """
        Find a device address by name (case-insensitive partial match).
        
        Args:
            name: Device name to search for (partial match supported)
            
        Returns:
            Device address if found, None otherwise
        """
        name_upper = name.upper()
        
        # First check known devices
        for device in self.get_known_devices():
            if device['name'] and name_upper in device['name'].upper():
                log.info(f"Found {name} in known devices: {device['address']}")
                return device['address']
        
        return None
    
    def remove_device(self, device_address: str) -> bool:
        """
        Remove a paired device.
        
        Args:
            device_address: Bluetooth address of device to remove (e.g., "AA:BB:CC:DD:EE:FF")
            
        Returns:
            True if device was removed, False otherwise
            
        Raises:
            ValueError: If device_address format is invalid
        """
        # Validate MAC address format
        if not self._validate_mac_address(device_address):
            raise ValueError(f"Invalid MAC address format: {device_address}")
        
        try:
            p = self._create_bluetoothctl_process()
            self._send_bluetoothctl_commands(p, [f"remove {device_address}"], wait_time=2.0)
            RfcommManager._safe_terminate(p)
            
            log.info(f"Removed device: {device_address}")
            return True
            
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log.error(f"Error removing device {device_address}: {e}")
            return False
    
    def pair_device(self, device_address: str, timeout: float = 30.0) -> bool:
        """Pair with a device by address (host-initiated pairing).

        Drives ``bluetoothctl`` to scan briefly (so the target is in range),
        then ``pair``. Passkey/PIN prompts are handled by the registered BlueZ
        agent (which displays the passkey on the board), not here.

        Args:
            device_address: Target MAC address (XX:XX:XX:XX:XX:XX).
            timeout: Maximum seconds to wait for pairing to report a result.

        Returns:
            True if pairing succeeded, False otherwise.

        Raises:
            ValueError: If device_address format is invalid.
        """
        if not self._validate_mac_address(device_address):
            raise ValueError(f"Invalid MAC address format: {device_address}")
        return self._run_pairing_command(f"pair {device_address}", timeout,
                                         success_markers=("Pairing successful",),
                                         failure_markers=("Failed to pair", "org.bluez.Error"))

    def trust_device(self, device_address: str) -> bool:
        """Mark a device as trusted so it may reconnect without re-pairing.

        Args:
            device_address: Target MAC address.

        Returns:
            True if the trust command reported success.

        Raises:
            ValueError: If device_address format is invalid.
        """
        if not self._validate_mac_address(device_address):
            raise ValueError(f"Invalid MAC address format: {device_address}")
        return self._run_pairing_command(f"trust {device_address}", timeout=10.0,
                                         success_markers=("trust succeeded", "Changing", "trusted: yes"),
                                         failure_markers=("Failed to trust",))

    def connect_device(self, device_address: str, timeout: float = 20.0) -> bool:
        """Connect to a paired device (e.g. bind a keyboard's input profile).

        Args:
            device_address: Target MAC address.
            timeout: Maximum seconds to wait for the connection result.

        Returns:
            True if the connection succeeded.

        Raises:
            ValueError: If device_address format is invalid.
        """
        if not self._validate_mac_address(device_address):
            raise ValueError(f"Invalid MAC address format: {device_address}")
        return self._run_pairing_command(f"connect {device_address}", timeout,
                                         success_markers=("Connection successful", "Connected: yes"),
                                         failure_markers=("Failed to connect", "org.bluez.Error"))

    def _run_pairing_command(self, command: str, timeout: float,
                             success_markers, failure_markers) -> bool:
        """Run a single bluetoothctl command and detect success/failure markers.

        Args:
            command: bluetoothctl command (e.g. ``"pair AA:BB:..."``).
            timeout: Maximum seconds to read output before giving up.
            success_markers: Substrings whose presence indicates success.
            failure_markers: Substrings whose presence indicates failure.

        Returns:
            True if a success marker was seen before a failure marker or timeout.
        """
        result = {"done": False, "ok": False}

        def process_line(line: str) -> bool:
            for marker in success_markers:
                if marker in line:
                    result["ok"] = True
                    result["done"] = True
                    return False
            for marker in failure_markers:
                if marker in line:
                    result["ok"] = False
                    result["done"] = True
                    return False
            return True

        try:
            p = self._create_bluetoothctl_process()
            self._send_bluetoothctl_commands(p, ["power on", "agent on", "scan on"], wait_time=1.0)
            p.stdin.write(f"{command}\n")
            p.stdin.flush()
            self._read_bluetoothctl_output(p, timeout=timeout, line_processor=process_line)
            try:
                p.stdin.write("scan off\n")
                p.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            RfcommManager._safe_terminate(p)
            log.info(f"[RfcommManager] Command '{command}' -> ok={result['ok']}")
            return result["ok"]
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log.error(f"[RfcommManager] Error running '{command}': {e}")
            return False

    def get_bluetooth_status(self) -> Dict[str, bool]:
        """
        Get current Bluetooth status.
        
        Returns:
            Dictionary with 'powered', 'discoverable', and 'pairable' status
        """
        status = {
            'powered': False,
            'discoverable': False,
            'pairable': False
        }
        
        try:
            p = self._create_bluetoothctl_process()
            p.stdin.write("show\n")
            p.stdin.flush()
            
            def process_status_line(line: str) -> bool:
                """Process a line from show output"""
                line_lower = line.lower()
                if 'powered: yes' in line_lower:
                    status['powered'] = True
                elif 'powered: no' in line_lower:
                    status['powered'] = False
                elif 'discoverable: yes' in line_lower:
                    status['discoverable'] = True
                elif 'discoverable: no' in line_lower:
                    status['discoverable'] = False
                elif 'pairable: yes' in line_lower:
                    status['pairable'] = True
                elif 'pairable: no' in line_lower:
                    status['pairable'] = False
                return True  # Continue reading
            
            self._read_bluetoothctl_output(p, timeout=3.0, line_processor=process_status_line)
            RfcommManager._safe_terminate(p)
            
            return status
            
        except (subprocess.SubprocessError, OSError) as e:
            log.error(f"Error getting Bluetooth status: {e}")
            return status

