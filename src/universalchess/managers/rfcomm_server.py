"""RFCOMM Server Manager.

Manages Bluetooth RFCOMM server socket, client connections, and data routing.
Encapsulates socket lifecycle with clean start/stop semantics.
"""

import os
import threading
import time
from typing import Callable, Optional

from universalchess.board.logging import log


class RfcommServer:
    """RFCOMM Bluetooth server for classic Bluetooth connections.
    
    Handles:
    - Server socket creation and service advertisement
    - Client connection acceptance
    - Client data reading in background thread
    - Clean shutdown of sockets and threads
    
    Usage:
        server = RfcommServer(
            device_name="DGT PEGASUS",
            on_connected=lambda: print("Client connected"),
            on_disconnected=lambda: print("Client disconnected"),
            on_data_received=lambda data: print(f"Received: {data}"),
        )
        server.start()
        # ... later ...
        server.stop()
    """
    
    def __init__(
        self,
        device_name: str,
        on_connected: Callable[[], None],
        on_disconnected: Callable[[], None],
        on_data_received: Callable[[bytes], None],
        port: Optional[int] = None,
        rfcomm_manager=None,
        adapter_alias: Optional[str] = None,
    ):
        """Initialize the RFCOMM server.
        
        Args:
            device_name: Bluetooth SDP service name advertised for the SPP
                service (what Classic apps match on by name); kept distinct from
                the adapter's friendly Alias.
            on_connected: Callback when a client connects
            on_disconnected: Callback when a client disconnects
            on_data_received: Callback for received data bytes
            port: RFCOMM port number (None = auto-assign)
            rfcomm_manager: Optional RfcommManager for pairing support
            adapter_alias: Friendly adapter Alias to set for discoverability
                (the branded name a phone shows). When None, falls back to
                ``device_name`` so behaviour is unchanged for callers that do
                not brand the alias.
        """
        self._device_name = device_name
        self._adapter_alias = adapter_alias or device_name
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_data_received = on_data_received
        self._port = port
        self._rfcomm_manager = rfcomm_manager
        
        self._server_sock = None
        self._client_sock = None
        self._client_connected = False
        self._running = False
        self._accept_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None
        
        self._uuid = "94f39d29-7d6d-437d-973b-fba39e49d4ee"
    
    @property
    def connected(self) -> bool:
        """Return True if a client is currently connected."""
        return self._client_connected
    
    @property
    def client_socket(self):
        """Return the current client socket, or None."""
        return self._client_sock
    
    def start(self, startup_splash=None) -> bool:
        """Start the RFCOMM server in a background thread.
        
        Args:
            startup_splash: Optional splash screen for status updates
            
        Returns:
            True if started successfully
        """
        if self._running:
            log.warning("[RfcommServer] Already running")
            return True
        
        self._running = True
        
        def accept_loop():
            self._setup_and_accept(startup_splash)
        
        self._accept_thread = threading.Thread(target=accept_loop, daemon=True)
        self._accept_thread.start()
        log.info("[RfcommServer] Background thread started")
        return True
    
    def _setup_and_accept(self, startup_splash=None):
        """Initialize RFCOMM and accept connections.
        
        Runs in background thread. Handles:
        - Killing existing rfcomm processes
        - Setting up RfcommManager for pairing
        - Creating and binding server socket
        - Advertising service
        - Accept loop for incoming connections
        """
        try:
            import bluetooth
        except ImportError:
            log.error("[RfcommServer] bluetooth module not available")
            return
        
        try:
            import psutil
        except ImportError:
            psutil = None
        
        if startup_splash:
            startup_splash.set_message("RFCOMM...")
        log.info("[RfcommServer] Starting initialization...")
        
        # Kill any existing rfcomm processes
        os.system('sudo service rfcomm stop 2>/dev/null')  # noqa: S605, S607 # nosec B605 B607 - fixed literal command, no untrusted input
        time.sleep(0.5)
        
        if psutil:
            for p in psutil.process_iter(attrs=['pid', 'name']):
                if str(p.info["name"]) == "rfcomm":
                    try:
                        p.kill()
                    except Exception:  # noqa: BLE001, S110 # nosec B110 - best-effort cleanup of a stale process; failure is non-fatal
                        pass
        
        time.sleep(0.3)
        
        # Setup pairing manager if provided
        if self._rfcomm_manager is not None:
            self._rfcomm_manager.enable_bluetooth()
            # Set the adapter Alias (friendly name), not the SDP service name:
            # the alias is the branded name a phone shows, while the SDP service
            # keeps device_name so Classic apps still match the SPP service.
            self._rfcomm_manager.set_device_name(self._adapter_alias)
            self._rfcomm_manager.start_pairing_thread()
            time.sleep(0.5)
        
        # Create server socket
        log.info("[RfcommServer] Setting up server socket...")
        try:
            self._server_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            bind_port = self._port if self._port else bluetooth.PORT_ANY
            self._server_sock.bind(("", bind_port))
            self._server_sock.settimeout(0.5)
            self._server_sock.listen(1)
            actual_port = self._server_sock.getsockname()[1]
        except Exception as e:
            log.error(f"[RfcommServer] Failed to create server socket: {e}")
            return
        
        # Advertise service
        try:
            bluetooth.advertise_service(
                self._server_sock,
                self._device_name,
                service_id=self._uuid,
                service_classes=[self._uuid, bluetooth.SERIAL_PORT_CLASS],
                profiles=[bluetooth.SERIAL_PORT_PROFILE]
            )
            log.info(f"[RfcommServer] Service '{self._device_name}' advertised on channel {actual_port}")
        except Exception as e:
            log.error(f"[RfcommServer] Failed to advertise service: {e}")
        
        log.info("[RfcommServer] Initialization complete, accepting connections...")
        
        # Accept loop
        while self._running:
            try:
                sock, client_info = self._server_sock.accept()
                self._client_sock = sock
                self._client_connected = True
                
                log.info("=" * 60)
                log.info("RFCOMM CLIENT CONNECTED")
                log.info("=" * 60)
                log.info(f"Client address: {client_info}")
                
                # Notify connection
                try:
                    self._on_connected()
                except Exception as e:
                    log.error(f"[RfcommServer] Error in on_connected callback: {e}")
                
                # Start reader thread
                self._reader_thread = threading.Thread(
                    target=self._client_reader,
                    daemon=True
                )
                self._reader_thread.start()
                
                # Wait for disconnect before accepting new connection
                while self._client_connected and self._running:
                    time.sleep(0.5)
                
            except bluetooth.BluetoothError:
                time.sleep(0.1)
            except Exception as e:
                if self._running:
                    log.error(f"[RfcommServer] Error accepting connection: {e}")
                time.sleep(0.1)
    
    def _client_reader(self):
        """Read data from connected client.
        
        Runs in background thread while client is connected.
        """
        try:
            import bluetooth
        except ImportError:
            return
        
        log.info("[RfcommServer] Client reader thread started")
        try:
            while self._running and self._client_connected:
                try:
                    if self._client_sock is None:
                        time.sleep(0.1)
                        continue
                    
                    data = self._client_sock.recv(1024)
                    if len(data) == 0:
                        log.info("[RfcommServer] Client disconnected (zero-length read)")
                        self._client_connected = False
                        break
                    
                    # Forward to callback
                    try:
                        self._on_data_received(bytes(data))
                    except Exception as e:
                        log.error(f"[RfcommServer] Error in on_data_received callback: {e}")
                    
                except bluetooth.BluetoothError as e:
                    if self._running:
                        log.error(f"[RfcommServer] Bluetooth error: {e}")
                    self._client_connected = False
                    break
                except Exception as e:
                    if self._running:
                        log.error(f"[RfcommServer] Error reading: {e}")
                    break
        finally:
            log.info("[RfcommServer] Client reader thread stopped")
            was_connected = self._client_connected
            self._client_connected = False
            
            # Notify disconnection
            if was_connected:
                try:
                    self._on_disconnected()
                except Exception as e:
                    log.error(f"[RfcommServer] Error in on_disconnected callback: {e}")
    
    def send(self, data: bytes) -> bool:
        """Send data to connected client.
        
        Args:
            data: Bytes to send
            
        Returns:
            True if sent successfully, False otherwise
        """
        if not self._client_connected or self._client_sock is None:
            return False
        
        try:
            self._client_sock.send(data)
            return True
        except Exception as e:
            log.error(f"[RfcommServer] Error sending: {e}")
            return False
    
    def stop(self):
        """Stop the RFCOMM server and close all sockets."""
        log.info("[RfcommServer] Stopping...")
        self._running = False
        self._client_connected = False
        
        # Close client socket
        if self._client_sock is not None:
            try:
                self._client_sock.close()
                log.info("[RfcommServer] Client socket closed")
            except Exception as e:
                log.error(f"[RfcommServer] Error closing client socket: {e}")
            self._client_sock = None
        
        # Close server socket
        if self._server_sock is not None:
            try:
                self._server_sock.close()
                log.info("[RfcommServer] Server socket closed")
            except Exception as e:
                log.error(f"[RfcommServer] Error closing server socket: {e}")
            self._server_sock = None
        
        # Stop pairing manager
        if self._rfcomm_manager is not None:
            try:
                self._rfcomm_manager.stop_pairing_thread()
                log.info("[RfcommServer] Pairing manager stopped")
            except Exception as e:
                log.error(f"[RfcommServer] Error stopping pairing manager: {e}")
        
        log.info("[RfcommServer] Stopped")

