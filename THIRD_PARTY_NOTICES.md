# Third-Party Notices

This project includes third-party components. Each component is governed by its own license terms.

## Fonts

### WenQuanYi Micro Hei (`Font.ttc`)

- **Files**:
  - `src/universalchess/resources/Font.ttc`
- **What it is**:
  - A TrueType Collection (`.ttc`) containing:
    - WenQuanYi Micro Hei
    - WenQuanYi Micro Hei Mono
- **Copyright (as embedded in the font metadata)**:
  - “Digitized data copyright © 2007, Google Corporation. Copyright © 2008-2009 WenQuanYi Board of Trustees (`http://wenq.org/`) and Qianqian Fang”
- **License (as embedded in the font metadata)**:
  - Apache License, Version 2.0
  - License URL: `http://www.apache.org/licenses/LICENSE-2.0`
- **License text**:
  - See `licenses/Apache-2.0.txt`

## Artwork

### Chess Pieces 16x16 One-bit (`chesssprites_onebit.png`)

- **Files**:
  - `src/universalchess/resources/chesssprites_onebit.png`
- **What it is**:
  - One-bit 16x16 pixel-art chess piece sprites (the six pieces in black and white colorways) used by the "onebit" board sprite style.
- **Author**:
  - BerryArray (`https://berryarray.itch.io/chess-pieces-16x16-one-bit`)
- **License**:
  - Creative Commons Zero v1.0 Universal (CC0 1.0) — public domain dedication
  - License URL: `https://creativecommons.org/publicdomain/zero/1.0/`
- **License text**:
  - See `licenses/CC0-1.0.txt`
- **Note**:
  - CC0 places the work in the public domain and does not require attribution; this notice is provided as a courtesy.

### Cburnett chess pieces (`Chess_Pieces_Sprite.svg`, `chesssprites_cburnett.png`)

- **Files**:
  - `src/universalchess/resources/Chess_Pieces_Sprite.svg` (source vector art)
  - `src/universalchess/resources/chesssprites_cburnett.png` (16x16 sprite sheet rasterised from the SVG for the "cburnett" board sprite style)
- **What it is**:
  - The classic Cburnett/Wikimedia vector chess set, widely used across chess software. `chesssprites_cburnett.png` is a downsampled 16x16 COLORWAY sheet produced from the SVG via `scripts/make-svg-sprite-sheet.py`.
- **Author**:
  - Colin M.L. Burnett (Wikimedia Commons user "Cburnett"), `https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces`
- **License**:
  - Multi-licensed by the author under GNU GPL v2 or later, GNU FDL, and a BSD-style license. This project relies on the BSD-style option.
  - License text: See `licenses/BSD-Cburnett.txt`

