<table>
<tr>
<td width="200" valign="top" align="center">
<img src="src/universalchess/web-app/public/icons/logo-full.png" alt="Universal-Chess" width="170" />
</td>
<td valign="top">
<h2>Universal-Chess</h2>
<p><em>Look a gift horse in the mouth</em></p>
<p>Universal-Chess is an extensible software platform for electronic chess boards. It currently targets the <strong>DGT Centaur</strong> hardware (as the first supported board), and includes:</p>
<ul>
<li>Local play against engines</li>
<li>AI coaching (experimental &mdash; still rough, can hallucinate, use with a grain of salt)</li>
<li>Game recording/export (PGN)</li>
<li>Online play (Lichess integration)</li>
<li>Board emulation modes (Millennium / Pegasus / Chessnut) for compatible apps</li>
<li>The ability to run the original Centaur software after uploading your original SD image to the new board</li>
</ul>
</td>
</tr>
</table>

## Acknowledgments

<img src="src/universalchess/web-app/public/images/board-and-web.jpg" alt="Universal-Chess running on a DGT Centaur board alongside the web interface" align="right" width="360" />

Universal Chess exists thanks to the people and projects below.

- **[Adrian Dybwad](https://github.com/adrian-dybwad)** — Creator and lead developer.
- **[Cursor](https://cursor.com)** — AI pair programming used throughout development.
- **Bartolomé Acosta Urrea** — A special thank you for motivation, extensive testing and hands-on help refining the application (**bartok1981** on discord).
- **[DGTCentaur Mods](https://github.com/EdNekebno/DGTCentaur)** — The project Universal Chess is built on, originally created by Ed Nekebno and community contributors.

Special thanks to all the open source chess engine authors whose work makes this project possible.

## Hardware

On DGT Centaur hardware, the stock Raspberry Pi can be replaced with a Raspberry Pi Zero 2 W (or Raspberry Pi Zero W) to enable Wi‑Fi/Bluetooth features and run this software stack. Performance is hugely improved on the Zero 2 W and we highly recommend using it as the Zero W is quite slow although it does work. This software supports the latest version of PI OS (Trixie) and also 32 or 64 bit. 64 bit supports more engines than 32, so we recommend you use that.

**Warranty notice:** Hardware modification may void device warranty. Proceed at your own risk.

## Architecture

The codebase is built on two foundational layers that enable everything else:

**Serial Communication Layer (`sync_centaur.py`)** - Handles all low-level DGT board protocol: packet parsing, command encoding, and an async callback queue for event processing. This provides a clean, reliable interface to the hardware - piece lift/place events, key presses, LED control, and sound.

**E-Paper Display Framework (`epaper/`)** - A composable widget system with a `Manager`, `Scheduler`, and widget hierarchy (`ChessBoardWidget`, `IconMenuWidget`, `GameAnalysisWidget`, etc.). Handles partial refresh scheduling, framebuffer management, and modal widget support. E-paper displays have unique constraints (slow refresh, ghosting, partial update limitations) and this framework abstracts those complexities away.

These foundations enable the higher-level components:
- `GameManager` receives clean piece events and manages chess game logic
- `DisplayManager` composes widgets without worrying about refresh mechanics  
- The menu system, game resume, position loading - all orchestration on top of solid primitives

The project is moving toward a plugin-friendly architecture where board support, emulators, players, and assistants can be extended without rewriting core orchestration.

## Project Status and a Word on Forks and Derivatives and other builds

Forks and derivatives are welcome. Follow the license, clearly label modifications, and ensure end users understand the state of the code.

A number of binaries are included in this repository and are not covered under the general GPL license terms. The GPL license covers the bulk of the Python code. Derivative projects should verify licensing for any bundled binaries.

This project is in beta. Bugs may exist. Issues and reports are welcome.


## Current Features

### Standalone Play
- **Play Engines** - Play against CT800, Zahak, RodentIV, Maia, or Stockfish directly from the board. Supports takebacks, move overrides, and configurable ELO levels. The engine shows its move via LEDs and you execute it on the board.
- **Game Resume** - If the board is shut down mid-game, it automatically resumes where you left off on next startup.
- **Predefined Positions** - Load test positions (en passant, castling, promotion) or puzzles/endgames from the Settings menu. Physical board correction mode guides you to set up the position correctly.

### Board Emulation (Universal)
Universal-Chess can advertise as multiple e-board types and auto-detect which protocol a connected app uses:
- **DGT Revelation II / Millennium** - Use the Centaur as a Bluetooth DGT e-board with apps, Rabbit plugin, Livechess, etc. Works with Chess for Android and Chess.com app (experimental).
- **DGT Pegasus** - Emulate a DGT Pegasus. Works with the DGT Chess app.
- **Chessnut** - Emulate a Chessnut board for compatible apps.
- **Engine Install** - Install your choice of UCI engines via the web interface.
- **AI Coach** - Use your choice of AI engine to coach you - on your level - or to analyse games after the fact.

### Online Play
- **Lichess** - Set your Lichess API token, then play online games directly from the board.

### Web Interface
- **Live Board View** - See the current board position at http://IP_ADDRESS or your board's hostname.
- **PGN Download** - Download all played games as PGN files.
- **Game Analysis** - Playback and analyze played games with takeback support.
- **Video Streaming** - Live MJPEG stream at /video for OBS or other streaming setups.

### Connectivity
- **WiFi** - Join WiFi networks from the board (WPS/WPA2).
- **Bluetooth** - Pair with apps via BLE or Bluetooth Classic.
- **Chromecast** - Stream live board view to Chromecast.
- **Network Drive** - Access files via authenticated WebDAV. The last 100 PGNs are accessible as files.

### Settings
- WiFi configuration, Bluetooth pairing, sound control, Lichess API token, engine selection, and predefined position loading.

## Install procedure

Start clean on a new SD card using the Raspberry Pi Imager. Do not reuse the
original card from the Centaur -- keep it safe, you may want it later.

1. In the Pi Imager, choose Raspberry Pi OS (64-bit) **Trixie**, with no apps or
   desktop (Lite). The 32-bit image also works, but the 64-bit image supports
   more engines.
2. In the Imager's advanced settings, configure the hostname, a user and password
   (any username works, you are not limited to `pi` -- just remember the
   password), Wi-Fi credentials, and enable SSH. Set any other options you like.
3. Write the image, insert the card into the Pi, and power it on. With Wi-Fi
   configured correctly the Pi usually joins your network within a minute or two.
   No screen or keyboard required.
4. SSH into the Pi and update the base system:
   ```bash
   sudo apt update && sudo apt upgrade -y
   ```
5. Run the install command for your choice of nightly or release build (see the
   [Releases page](https://github.com/adrian-dybwad/Universal-Chess/releases) for
   the exact command and `.deb` for the target version).
6. Wait for the install to complete, reboot, and the board should come up running
   Universal Chess.

### No Wi-Fi? Set the board up over the USB cable

Steps 3 to 5 above assume the Pi can join your Wi-Fi. If it cannot — no network
in reach, a password that turns out to be wrong, a 5 GHz-only access point, or a
plain Pi Zero with no radio at all — the board never appears, and with no screen
or keyboard there is nothing to log into to fix it.

So the fallback has to be in place **before** the first boot, put there from the
same machine that imaged the card. `enable_usb_gadget.py` makes the Pi appear as
a USB Ethernet adapter, so a single cable gives you both a login and, if you
share the host's connection, the internet access the install needs.

Download `enable_usb_gadget.py` from the
[Releases page](https://github.com/adrian-dybwad/Universal-Chess/releases). It is
one self-contained file and needs Python 3.9 or newer on your computer — nothing
else to install.

1. Write the card with the Pi Imager as in steps 1 and 2 above. Configure the
   hostname, user and password, and enable SSH. Wi-Fi details are optional here.
2. Leave the card in the reader. Do not boot it yet — these changes have to be
   in place before the first boot.
3. Run the tool on your computer:

   ```bash
   python3 enable_usb_gadget.py --dry-run   # show what it would change
   python3 enable_usb_gadget.py             # make the changes
   ```

   On Windows use `py enable_usb_gadget.py`. If Python is missing, install it
   from [python.org](https://www.python.org/downloads/) or the Microsoft Store.

   The card is found automatically — `/Volumes/bootfs` on macOS,
   `/media/<user>/bootfs` on Linux, a drive letter on Windows. If detection
   fails, or more than one card is mounted, pass `--boot` with the path to the
   boot partition. The tool describes the card it found and asks you to confirm
   it before writing anything.

4. Eject the card, put it in the Pi, and connect the USB cable. On a Pi Zero use
   the **middle** micro-USB port, the one next to the mini-HDMI — the port
   marked `PWR IN` supplies power only and will not enumerate.
5. Leave the tool running. It waits for the board to appear and then checks that
   name resolution works over the new link. A Pi Zero's first boot runs cloud-init
   on slow hardware; the tool waits 300 seconds before giving up, which
   `--wait-timeout SECONDS` extends.

For the Pi to reach the internet — which it needs for `apt` in step 4 of the
main procedure — the host has to share its own connection over that USB
interface:

| Host | Where to turn it on |
| --- | --- |
| **macOS** | System Settings > General > Sharing > Internet Sharing, sharing to the RNDIS/Ethernet Gadget |
| **Windows** | Network Connections > right-click your internet adapter > Properties > Sharing tab > allow sharing to the gadget adapter |
| **Linux** | NetworkManager: set the `usb0` connection's IPv4 method to *Shared to other computers* |

Windows also needs a one-time RNDIS driver, from
[rpi-usb-gadget releases](https://github.com/raspberrypi/rpi-usb-gadget/releases).
macOS and Linux need no driver.

Then continue from step 4 of the main procedure, connecting with the hostname
you set in the Imager:

```bash
ssh <your-user>@<your-hostname>.local
```

The Pi takes its address by DHCP from the host, so it has no fixed IP and the
address changes between boots. Prefer the hostname. If `.local` does not resolve,
find the address your host leased it:

| Host | Command |
| --- | --- |
| **macOS** | `arp -an \| grep bridge`, or read `/var/db/dhcpd_leases` |
| **Windows** | `arp -a` |
| **Linux** | `ip neigh show dev usb0` |

If the board answers by IP but names do not resolve, the usual cause is a
resolver on your computer that started before the USB interface existed and
never bound to it. The board says so at login, and the same tool diagnoses and
offers to fix it:

```bash
python3 enable_usb_gadget.py --check-dns --fix
```

Two caveats. Enabling USB gadget mode **disables the Pi's USB host port**, so no
USB peripherals while it is on; nothing in Universal Chess uses that port. And on
a Centaur the Pi is powered from the board, so connecting it to a computer can
backfeed the board's 5 V rail — worth checking on your own hardware before
leaving it plugged in.

Full documentation, including troubleshooting for each stage, is in
[`tools/sd-card-setup/README.md`](tools/sd-card-setup/README.md).

## Local development setup (configs and database)

- Active config is read from `/opt/universalchess/config/centaur.ini`. A default template is tracked at `packaging/deb-root/opt/universalchess/defaults/config/centaur.ini`.
- The SQLite database is created at runtime at `/opt/universalchess/db/centaur.db` on first run; it is not tracked in git.
 - The current FEN position is written to `/opt/universalchess/tmp/fen.log` by runtime services.

## Repository layout

- Source (Python package): `src/universalchess/`
- Debian packaging staging root: `packaging/deb-root/`
- Debian install root at runtime: `/opt/universalchess`

## Local Python env (direnv + venv)

- This repo expects the bundled virtualenv at `.venv`. Helper scripts `bin/python` and `bin/pytest` wrap it.
- Auto-activate via direnv:
  1. Install direnv: `brew install direnv`
  2. Ensure shell hook is present:
     ```
     grep -q 'direnv hook zsh' ~/.zshrc || echo 'eval "$(direnv hook zsh)"' >> ~/.zshrc
     source ~/.zshrc
     ```
  3. From repo root, allow: `direnv allow`
- After that, entering the repo activates the venv; run tests with `bin/pytest ...` or python with `bin/python ...`.

## Running locally

- Main app: `./scripts/run.sh` (defaults to `python -m universalchess.main`)
  - Skip auto-update/pull: `./scripts/run.sh --no-update`
- Web UI: `./scripts/run-web.sh`

## Support

Join on Discord: `https://discord.gg/f3DrD6KPM`

## Contributors welcome!
