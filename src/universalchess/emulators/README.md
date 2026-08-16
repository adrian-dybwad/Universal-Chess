# Emulators

Protocol parsing and encoding modules used by `RemoteController` to translate
between chess app protocols and the physical board.

## Purpose

These are library modules imported by `controllers/remote.py`. They provide:
- Protocol packet parsing (decoding commands from apps)
- Response encoding (generating replies in the expected format)
- Board state translation between Centaur and target protocol formats

## Files

| File | Description |
|------|-------------|
| `chessnut.py` | Parses Chessnut Air commands and encodes FEN/battery responses |
| `millennium.py` | Parses Millennium ChessLink packets and encodes responses |
| `pegasus.py` | Parses DGT Pegasus commands and encodes board state |

## Architecture

```
Chess App <--BLE/RFCOMM--> RemoteController <--uses--> emulators/*.py
                                  |
                                  v
                          Physical Centaur Board
```

`RemoteController` owns protocol detection and routing. These modules handle
protocol-specific parse/encode. `ProtocolManager` wires players and assistants;
it does not import the emulators.

## Requirements

- Physical board connected
- Used as part of the Universal-Chess system (not standalone)

## See Also

- `src/universalchess/simulators/` - Standalone board simulators that run without hardware
- `controllers/remote.py` - Creates and drives the emulator instances
