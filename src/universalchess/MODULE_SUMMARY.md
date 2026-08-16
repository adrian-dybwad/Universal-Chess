# DGTCentaurMods Module Summary

This document provides a comprehensive overview of all modules, their internal global variables, and responsibilities.

---

## Table of Contents

1. [main.py (Entry Point)](#mainpy-entry-point)
2. [board/ Module](#board-module)
3. [managers/ Module](#managers-module)
4. [state/ Module](#state-module)
5. [services/ Module](#services-module)
6. [controllers/ Module](#controllers-module)
7. [players/ Module](#players-module)
8. [assistants/ Module](#assistants-module)
9. [emulators/ Module](#emulators-module)
10. [epaper/ Module](#epaper-module)
11. [web/ Module](#web-module)

---

## main.py (Entry Point)

### Purpose
Main application entry point - Universal Bluetooth Relay with BLE and RFCOMM support. Connects to target devices via Bluetooth and relays data between the board and connected clients.

### Global Variables
| Variable | Purpose |
|----------|---------|
| `incomplete_shutdown` | Flag indicating if previous shutdown was incomplete (filesystem errors detected) |

### Responsibilities
- Application initialization and startup
- Previous shutdown analysis (check for unclean shutdowns)
- Resource initialization (fonts, sprites, logos)
- Display initialization with splash screen
- Board controller initialization with retry logic
- Event subscription for keys and piece events
- BLE and RFCOMM connection handling
- Menu system coordination
- Shutdown sequence management

---

## board/ Module

Hardware abstraction layer for interacting with the chess board.

### board.py

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `board_meta_properties` | Dictionary storing board metadata (serial number, firmware version, trademark) |
| `display_manager` | Global ePaper display manager instance |
| `INACTIVITY_TIMEOUT_DEFAULT` | Default inactivity timeout (900 seconds = 15 minutes) |
| `INACTIVITY_WARNING_SECONDS` | Warning countdown duration (120 seconds) |
| `MAX_INIT_RETRIES` | Maximum board initialization retry attempts (3) |
| `INIT_TIMEOUT_SECONDS` | Timeout per initialization attempt (10 seconds) |
| `controller` | Global SyncCentaur controller instance |
| `eventsthreadpointer` | Reference to events monitoring thread |
| `eventsrunning` | Flag controlling event thread execution (1=running, 0=paused) |

#### Responsibilities
- Board initialization with retry logic
- LED control (ledArray, ledFromTo, led, ledFlash, ledsOff)
- Sound/beep control with granular sound settings
- Board state reading (getBoardState, getChessState)
- Coordinate transformation between hardware and chess formats
- Inactivity timeout management
- Shutdown countdown display
- Sleep command to DGT controller
- Event subscription (keys, piece lifts/places)

### centaur.py

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `config` | Global ConfigParser instance |
| `lichess_api` | Cached Lichess API token |
| `lichess_range` | Cached Lichess opponent ELO range |
| `centaur_sound` | Cached sound setting |

#### Responsibilities
- Settings accessors (Lichess API, menu visibility, sound)
- Update system management (check, download, install updates)
- Shell command execution
- Path utilities

### settings.py

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `configfile` | Path to centaur.ini config file |
| `defconfigfile` | Path to default config file |

#### Responsibilities
- Configuration file read/write operations
- Key existence guarantees with default fallback
- Section and key management

### network.py

#### Responsibilities
- WiFi WPS connection management
- Network connectivity checks
- Internet socket testing

---

## managers/ Module

High-level managers that coordinate game logic, display, and communication.

### game.py (GameManager)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `_deferred_imports_ready` | Threading.Event for lazy-loaded imports |
| `_deferred_models` | Lazy-loaded database models |
| `_deferred_linear_sum_assignment` | Lazy-loaded scipy function for correction guidance |
| `_deferred_sessionmaker` | Lazy-loaded SQLAlchemy sessionmaker |
| `_deferred_func` | Lazy-loaded SQLAlchemy func |
| `_deferred_select` | Lazy-loaded SQLAlchemy select |
| `_deferred_create_engine` | Lazy-loaded SQLAlchemy create_engine |
| `_import_thread` | Background thread for deferred imports |
| `BOARD_SIZE` | Chess board size (64 squares) |
| `BOARD_WIDTH` | Board width (8 squares) |
| `CENTER_SQUARES` | Set of center squares for resign detection (d4, d5, e4, e5) |
| `STARTING_FEN` | FEN string for starting position |

#### Responsibilities
- Chess game state management (authoritative chess.Board)
- Move validation and execution
- Piece event handling (lift/place)
- Correction mode for fixing misplaced pieces
- Castling support (king-first and rook-first)
- Promotion handling
- Takeback support
- Kings-in-center resign/draw gesture detection
- King-lift resign gesture detection
- Database persistence of games and moves
- Check and queen threat detection
- LED guidance for valid moves

### display.py (DisplayManager)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `_widgets_loaded` | Flag for lazy widget loading |
| `_ChessBoardWidget` | Lazy-loaded ChessBoardWidget class |
| `_GameAnalysisWidget` | Lazy-loaded GameAnalysisWidget class |
| `_ChessClockWidget` | Lazy-loaded ChessClockWidget class |
| `_IconMenuWidget` | Lazy-loaded IconMenuWidget class |
| `_IconMenuEntry` | Lazy-loaded IconMenuEntry class |
| `_SplashScreen` | Lazy-loaded SplashScreen class |
| `_GameOverWidget` | Lazy-loaded GameOverWidget class |
| `_AlertWidget` | Lazy-loaded AlertWidget class |
| `STARTING_FEN` | Starting position FEN string |

#### Responsibilities
- Widget lifecycle management (create, show, hide, cleanup)
- Chess board display widget
- Clock/turn indicator widget
- Analysis engine and widget
- Promotion menu display
- Back button menu (resign/draw/cancel)
- Pause/resume game display
- Check and queen threat alerts
- Game over display
- Hand+Brain hint display
- Score history management for analysis

### protocol.py (ProtocolManager)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `CLIENT_*` | Client type constants (UNKNOWN, MILLENNIUM, PEGASUS, CHESSNUT, LICHESS) |

#### Responsibilities
- Player and assistant management
- Protocol detection flags
- GameManager callback delegation
- Lichess-specific callbacks (clock updates, game info)
- Player lifecycle (start/stop)
- App connection state management

### ble.py (BleManager)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `BLUEZ_*` | BlueZ D-Bus interface constants |
| `GATT_*` | GATT service/characteristic interface constants |
| `MILLENNIUM_UUIDS` | Millennium ChessLink BLE UUIDs |
| `NORDIC_UUIDS` | Nordic UART Service UUIDs (Pegasus) |
| `CHESSNUT_UUIDS` | Chessnut Air BLE UUIDs |
| `CHESSNUT_MANUFACTURER_*` | Chessnut advertisement data |

#### Responsibilities
- BLE GATT service registration
- D-Bus/BlueZ integration
- Client connection/disconnection handling
- Multi-protocol support (Millennium, Pegasus, Chessnut)
- BLE advertisement management
- NoInputNoOutput pairing agent

### connection.py (ConnectionManager)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `_instance` | Singleton instance |

#### Responsibilities
- Protocol data routing between BLE/RFCOMM and ControllerManager
- Data buffering when ControllerManager not ready
- Relay mode forwarding to shadow targets

### rfcomm.py (RfcommManager)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `PIN_CONF_PATHS` | Paths to search for pin.conf file |
| `MAC_ADDRESS_REGEX` | Regex for MAC address validation |

#### Responsibilities
- Classic Bluetooth pairing and discovery
- Device discoverability management
- bt-agent process management
- Paired device management
- RFCOMM connection handling

### menu.py (MenuManager)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `RESULT_MAP` | Maps result strings to MenuResult enum |
| `BREAK_RESULTS` | Set of results that break out of nested menus |
| `_instance` | Singleton instance |

#### Responsibilities
- Menu widget lifecycle
- Key queuing during menu loading
- Selection handling and result conversion
- Break result propagation through nested menus

### engine_manager.py (EngineManager)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `ENGINES_DIR` | Engine installation directory path |
| `BUILD_TMP` | Temporary build directory path |
| `ENGINES` | Dictionary of all engine definitions |
| `_engine_manager` | Singleton instance |

#### Responsibilities
- Engine installation from source
- Engine uninstallation
- Dependency management
- Build process management
- Installation progress tracking

### assistant.py (AssistantManager)

#### Responsibilities
- Assistant lifecycle (start/stop)
- Suggestion request routing
- Hand+Brain and hint assistant factory

### relay.py (RelayManager)

#### Responsibilities
- Shadow target device discovery
- RFCOMM connection to shadow target
- Bidirectional data relay
- Response comparison for debugging

### events.py

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `EVENT_NEW_GAME` | New game event constant (1) |
| `EVENT_BLACK_TURN` | Black's turn event (2) |
| `EVENT_WHITE_TURN` | White's turn event (3) |
| `EVENT_REQUEST_DRAW` | Draw request event (4) |
| `EVENT_RESIGN_GAME` | Resign event (5) |
| `EVENT_LIFT_PIECE` | Piece lift event (6) |
| `EVENT_PLACE_PIECE` | Piece place event (7) |
| `EVENT_PLAYER_READY` | Player ready event (8) |

---

## state/ Module

Lightweight observable state objects with minimal dependencies.

### chess_game.py (ChessGameState)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `_instance` | Singleton instance |

#### Responsibilities
- Authoritative chess.Board ownership
- Position state (FEN, turn, legal moves)
- Game result and termination reason
- Observer notifications for position changes
- Observer notifications for game over
- Piece presence state computation
- Check and queen threat information
- Starting position comparison utilities
- Per-move elapsed time on a monotonic clock (`move_durations_ms`), recorded at
  the instant a move is confirmed so the database writer and the PGN service
  report the same duration for the same move

### chess_clock.py (ChessClockState)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `_instance` | Singleton instance |

#### Responsibilities
- Clock times for both players
- Active player tracking
- Running/paused state
- Timed vs untimed mode
- Observer notifications (tick, state change, flag)

### system.py (SystemState)

#### Responsibilities
- Battery level and charging status
- WiFi connection status
- Charger connection status
- CPU temperature

### chromecast.py (ChromecastState)

#### Responsibilities
- Chromecast connection state
- Available cast devices

### analysis.py (AnalysisState)

#### Responsibilities
- Position analysis results
- Score history for graph display

---

## services/ Module

Long-lived singleton components that manage threads and resources.

### chess_clock.py (ChessClockService)

#### Responsibilities
- Clock countdown thread management
- Time control configuration
- Start/stop/pause/resume operations
- Turn switching
- Player name management

### chess_game.py (ChessGameService)

#### Responsibilities
- Game state coordination
- Move execution and validation
- Incremental in-memory PGN tree, annotated with `[%clk]` / `[%emt]` and the
  `[TimeControl]` header so the live PGN matches the one exported from the
  database

### chromecast.py (ChromecastService)

#### Responsibilities
- Chromecast device discovery
- Image casting to Chromecast
- Connection management

### pgn_time.py (pure functions)

#### Responsibilities
- Render a TimeControl as a storable PGN time-control value
- Expand a stored value into header pairs, splitting a time-odds control into
  `[WhiteTimeControl]` / `[BlackTimeControl]` so `[TimeControl]` stays standard
- Attach `[%clk]` / `[%emt]` embedded commands to a PGN move node, `[%emt]` to
  tenths of a second and `[%clk]` to the whole seconds the clock actually counts

No threads, database, board or display -- unlike the rest of this package it is
a pure formatting module, usable by any PGN exporter.

### system.py (SystemPollingService)

#### Responsibilities
- Periodic system state polling
- Battery/charger status updates
- WiFi status monitoring

---

## controllers/ Module

Manages how games are controlled - local players vs external apps.

### base.py (GameController)

#### Responsibilities
- Abstract base class for controllers
- Common controller interface

### local.py (LocalController)

#### Responsibilities
- Human and engine player coordination
- PlayerManager integration
- Game event handling
- Move execution from players

### remote.py (RemoteController)

#### Global Variables
| Variable | Purpose |
|----------|---------|
| `CLIENT_UNKNOWN` | Unknown client type constant |
| `CLIENT_MILLENNIUM` | Millennium client type |
| `CLIENT_PEGASUS` | Pegasus client type |
| `CLIENT_CHESSNUT` | Chessnut client type |

#### Responsibilities
- Protocol emulator management
- Bluetooth data parsing
- Protocol auto-detection
- Move reception from apps
- Board state synchronization with apps

### manager.py (ControllerManager)

#### Responsibilities
- Controller switching (local <-> remote)
- Event routing to active controller
- Bluetooth connection handling
- Controller lifecycle management

---

## players/ Module

Entities that make moves in chess games.

### base.py (Player)

#### Responsibilities
- Abstract base class for all player types
- `PlayerType` is the move source (`HUMAN`, `ENGINE`, `REMOTE`), not a provider name
- Rebuild-on-new-game is a capability (`requires_rebuild_on_new_game`), not a type
- Common player interface and player state management

### human.py (HumanPlayer)

#### Responsibilities
- Move formation from piece lift/place events
- Any legal move submission

### engine.py (EnginePlayer)

#### Responsibilities
- UCI engine process management
- Move computation with ELO-based settings
- Move submission when physical events match

### hand_brain.py (HandBrainPlayer)

#### Responsibilities
- Catalog type that reports HUMAN or ENGINE by mode
- Piece-type then move phases for Hand+Brain play

### lichess/ (Lichess plugin)

A REMOTE provider package. The game talks to `LichessPlayer`; hosts, tokens,
identity, seek, and the board lobby live here. Plugin tests live in `tests/`.

| File | Purpose |
|------|---------|
| `player.py` | `LichessPlayer` (`PlayerType.REMOTE`, rebuilds on new game) |
| `hosts.py` | `org` / `dev` API servers and `host:user` credential ids |
| `accounts.py` | Plugin credential rows on the generic account store |
| `match.py` | Seek params, berserk client |
| `lobby.py` | Board menu, identity fetch, waiting/started splash |
| `tests/` | Plugin tests (hosts, credentials, seek, lobby, echo guard) |

### manager.py (PlayerManager)

#### Responsibilities
- White and black player coordination
- Move callback routing
- Two-player vs human-vs-computer mode detection
- Player lifecycle management

---

## assistants/ Module

Entities that help the user play (suggestions, hints, guidance).

### base.py (Assistant)

#### Responsibilities
- Abstract base class for assistants
- Suggestion data structure
- Common assistant interface

### hand_brain.py (HandBrainAssistant)

#### Responsibilities
- Piece type suggestions (K, Q, R, B, N, P)
- Engine-based best piece analysis
- Auto-suggestion on player's turn

### hint.py (HintAssistant)

#### Responsibilities
- On-demand move hints
- Engine analysis for hint generation

---

## emulators/ Module

Protocol emulators for chess board companion apps.

### millennium.py (Millennium)

#### Responsibilities
- Millennium ChessLink protocol emulation
- FEN synchronization
- Move parsing from app commands
- Response generation

### pegasus.py (Pegasus)

#### Responsibilities
- DGT Pegasus protocol emulation
- Nordic UART service support
- Board state synchronization

### chessnut.py (Chessnut)

#### Responsibilities
- Chessnut Air protocol emulation
- FEN notification format
- Battery status responses

---

## epaper/ Module

ePaper display framework with widget system.

### framework/manager.py (Manager)

#### Responsibilities
- Display refresh scheduling (full and partial)
- Widget lifecycle management
- Framebuffer rendering
- Thread-safe display updates
- Diff-based partial refresh optimization

### Widgets

| Widget | Responsibilities |
|--------|------------------|
| `ChessBoardWidget` | Chess board position display, piece sprites, highlighting |
| `ChessClockWidget` | Clock times and turn indicator display |
| `GameAnalysisWidget` | Evaluation bar and score history graph |
| `StatusBarWidget` | Top bar with battery, WiFi, Bluetooth, clock |
| `IconMenuWidget` | Menu with icon entries and navigation |
| `KeyboardWidget` | On-screen keyboard for text input |
| `SplashScreen` | Modal splash messages |
| `AlertWidget` | Check and queen threat alerts |
| `GameOverWidget` | Game result and termination display |
| `BatteryWidget` | Battery level indicator |
| `ClockWidget` | Time display |
| `WiFiStatusWidget` | WiFi connection indicator |
| `BluetoothStatusWidget` | Bluetooth connection indicator |
| `ChromecastStatusWidget` | Chromecast connection indicator |

---

## web/ Module

Web interface for configuration and game viewing.

### app.py

#### Responsibilities
- Flask application factory
- Serves the React single-page app (index, assets, icons, manifest, service worker)
- JSON API (`/api/*`) for settings, engines, games, and system control
- Server-sent events (`/events`) and media endpoints (`/screen.jpg`, `/video`, `/logo`)
- TLS CA install page (`/ca-install`, `/ca.pem`) and WebDAV access

The legacy server-rendered Jinja UI (configure/index/PGN/analysis pages, the
Rodent IV tuner, and the `centaurflask.py`/`chessboard.py` helpers) was removed
once the React app superseded it.

---

## Architecture Overview

```
main.py (Entry Point)
    |
    +-- board/ (Hardware Abstraction)
    |     +-- board.py (LED, sounds, state reading)
    |     +-- centaur.py (Settings, updates)
    |     +-- SyncCentaur/AsyncCentaur (Serial communication)
    |
    +-- managers/ (Coordination Layer)
    |     +-- GameManager (Chess logic)
    |     +-- DisplayManager (UI widgets)
    |     +-- ProtocolManager (Player/assistant wiring)
    |     +-- BleManager (Bluetooth Low Energy)
    |     +-- RfcommManager (Classic Bluetooth)
    |     +-- ConnectionManager (Data routing)
    |     +-- MenuManager (Navigation)
    |     +-- EngineManager (UCI engines)
    |
    +-- state/ (Observable State)
    |     +-- ChessGameState (Position, moves)
    |     +-- ChessClockState (Times, active player)
    |     +-- SystemState (Battery, WiFi)
    |
    +-- services/ (Long-lived Components)
    |     +-- ChessClockService (Countdown thread)
    |     +-- ChromecastService (Casting)
    |     +-- SystemPollingService (Status updates)
    |
    +-- controllers/ (Game Control)
    |     +-- LocalController (Human/engine play)
    |     +-- RemoteController (App control)
    |     +-- ControllerManager (Switching)
    |
    +-- players/ (Move Sources)
    |     +-- HumanPlayer (Physical board)
    |     +-- EnginePlayer (UCI engine)
    |     +-- HandBrainPlayer (HUMAN or ENGINE by mode)
    |     +-- lichess/ (REMOTE plugin; tests in package)
    |
    +-- assistants/ (Helpers)
    |     +-- HandBrainAssistant (Piece hints)
    |     +-- HintAssistant (Move hints)
    |
    +-- emulators/ (Protocol Emulation)
    |     +-- Millennium, Pegasus, Chessnut
    |
    +-- epaper/ (Display)
    |     +-- Manager (Refresh scheduling)
    |     +-- Widgets (UI components)
    |
    +-- web/ (Web Interface)
          +-- Flask app for configuration
```

---

## Key Design Patterns

1. **Singleton Pattern**: State objects, services, and managers use singletons for global access
2. **Observer Pattern**: State objects notify observers on changes (position, clock, game over)
3. **Facade Pattern**: AssistantManager hides assistant implementation details
4. **Strategy Pattern**: Controllers and players are interchangeable implementations
5. **Factory Pattern**: Engine and assistant creation functions
6. **Lazy Loading**: Widgets and database imports are deferred to speed startup

---

## Thread Safety Notes

- State objects are NOT thread-safe; callers must synchronize mutations
- Services own countdown/polling threads
- Display manager has a dedicated render thread
- GameManager uses a task queue for ordered I/O operations
- BleManager uses GLib mainloop for D-Bus events

