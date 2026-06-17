# VM Setup for Running Centaur Software on Mac

This setup allows you to run the original DGT Centaur software in a Debian Bullseye VM on your Mac, while forwarding serial communication and epaper display updates to/from the Raspberry Pi.

## Architecture

```
Raspberry Pi                    Mac VM (Bullseye)
┌─────────────────┐            ┌──────────────────┐
│                 │            │                  │
│ Serial Relay    │◄──TCP──►   │ Serial Relay    │
│ Server          │            │ Client           │
│                 │            │                  │
│ Epaper Proxy    │◄──TCP──►   │ Epaper Proxy    │
│ Server          │            │ Client           │
│                 │            │                  │
│ /dev/ttyS0      │            │ /dev/ttyS0       │
│ (real hardware) │            │ (virtual)        │
│                 │            │                  │
│ Epaper Driver   │            │ Centaur Software│
│ (real hardware) │            │ (runs in VM)     │
└─────────────────┘            └──────────────────┘
```

## Prerequisites

### On Mac:
- VirtualBox, VMware Fusion, or UTM (for ARM Macs)
- Network connectivity between Mac and Pi

### On Raspberry Pi:
- Python 3
- pyserial: `pip3 install pyserial`
- PIL/Pillow: `pip3 install Pillow`

## Setup Steps

### 1. Create Debian Bullseye VM

#### Using VirtualBox (Intel Mac):
```bash
# Download Debian 11 (Bullseye) ISO
# Create new VM with:
# - 2GB RAM minimum
# - 20GB disk
# - Network: Bridged or NAT with port forwarding
```

#### Using UTM (Apple Silicon Mac):
```bash
# Download Debian 11 (Bullseye) ARM64 ISO
# Create new VM in UTM
# - 2GB RAM minimum
# - 20GB disk
# - Network: Shared or Bridged
```

### 2. Install Centaur Software in VM

```bash
# In VM, copy centaur directory from Pi or install from original source
# Ensure ~/centaur/centaur exists and is executable
```

### 3. Setup Serial Relay and Epaper Proxy

#### On Raspberry Pi (Server):
```bash
cd ~/Universal-Chess/scripts/vm-setup
# Start both servers at once:
./start_pi_servers.sh

# Or start individually:
python3 serial_relay_server.py &
python3 epaper_proxy_server.py &
```

#### On Mac VM (Client):
```bash
cd ~/Universal-Chess/scripts/vm-setup
# Start both clients at once:
./start_vm_relays.sh <PI_IP_ADDRESS>

# Or start individually:
python3 serial_relay_client.py --server-ip <PI_IP_ADDRESS> &
python3 epaper_proxy_client.py --server-ip <PI_IP_ADDRESS> &
```

### 5. Run Centaur in VM

**Option 1: Using the wrapper (recommended, no code modification):**
```bash
cd ~/Universal-Chess/scripts/vm-setup
# Ensure serial relay client is running first
python3 serial_relay_client.py --server-ip <PI_IP> &

# Run centaur with epaper proxy wrapper
python3 epaper_proxy_wrapper.py --server-ip <PI_IP> --centaur-path ~/centaur/centaur
```

**Option 2: Manual setup (if wrapper doesn't work):**
```bash
# In VM, ensure serial relay and epaper proxy clients are running
cd ~/centaur
sudo ./centaur
# Note: Epaper proxying may not work without the wrapper
```

## Network Configuration

The scripts use TCP sockets. Default ports:
- Serial relay: 8888
- Epaper proxy: 8889

Ensure firewall allows these ports if needed.

## Important Notes

### Epaper Proxy Integration (No Code Modification Required)
Since we cannot modify the centaur software, we use a wrapper script that injects the proxy driver using Python's import system:

**For Python-based centaur:**
```bash
# Use the wrapper script (automatically handles import injection)
python3 epaper_proxy_wrapper.py --server-ip <PI_IP> --centaur-path ~/centaur/centaur
```

**For binary centaur:**
If centaur is a compiled binary, epaper proxying may not work without kernel-level interception. The serial relay will still function. You can:
- Run centaur normally (serial will work, epaper won't be proxied)
- Or use the wrapper which will attempt to inject (may not work for binaries)

### Serial Port Permissions
On the VM, you may need to add your user to the `dialout` group or use `sudo` for serial port access:
```bash
sudo usermod -a -G dialout $USER
# Then log out and back in
```

## Troubleshooting

1. **Serial connection fails**: 
   - Check network connectivity: `ping <PI_IP>`
   - Verify firewall allows ports 8888 and 8889
   - Check serial port permissions on VM

2. **Epaper not updating**: 
   - Verify epaper proxy server is running on Pi
   - Check that centaur software is using the proxy driver
   - Look for connection errors in proxy client logs

3. **Centaur crashes**: 
   - Check VM has enough resources (2GB+ RAM recommended)
   - Verify Bullseye compatibility
   - Check serial relay is connected before starting centaur

4. **Virtual serial port not found**:
   - Ensure `socat` is installed: `sudo apt-get install socat`
   - Check `/tmp/vm_serial` exists after starting relay client

