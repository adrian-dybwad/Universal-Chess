#!/usr/bin/env bash
# Build the Centaur display-translation shim.
#
# The original centaur binary is 32-bit ARM (armhf). Build this shim NATIVELY on
# the Pi (armv7l/armhf), where the default gcc already targets 32-bit ARM, so the
# resulting .so is loadable into the centaur process via LD_PRELOAD.
#
# Usage (on the Pi):
#   ./build.sh                       # -> spishim.so next to this script
#
# Run centaur under the shim (the UC gateway must be listening first):
#   UC_CENTAUR_DISPLAY_SOCK=/run/universalchess/centaur-display.sock \
#   UC_CENTAUR_BUSY_IDLE_HIGH=1 \
#   LD_PRELOAD=$PWD/spishim.so ./centaur
#
# UC_CENTAUR_BUSY_IDLE_HIGH: 1 for a UC8151D-class centaur build (BUSY idle is
# HIGH), 0 for an SSD1680-class build (idle LOW). Tune per the captured trace.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/spishim.c"
OUT="${SCRIPT_DIR}/spishim.so"

CC="${CC:-gcc}"

# Build non-LFS (32-bit off_t/time_t). Current Debian/Raspbian armhf defaults to
# _FILE_OFFSET_BITS=64 + _TIME_BITS=64, under which a C function named `mmap`
# is exported as the symbol `mmap64`. The bundled RPi/_GPIO.so imports the plain
# `mmap@GLIBC_2.4` (confirmed via readelf), so the shim must export `mmap`; that
# requires 32-bit off_t. centaur is 32-bit ARM, so this matches its ABI.
"${CC}" -shared -fPIC -O2 -Wall -Wextra \
    -U_FILE_OFFSET_BITS -D_FILE_OFFSET_BITS=32 \
    -U_TIME_BITS -D_TIME_BITS=32 \
    "${SRC}" -o "${OUT}" -ldl -lpthread

echo "Built ${OUT}"
file "${OUT}" 2>/dev/null || true
