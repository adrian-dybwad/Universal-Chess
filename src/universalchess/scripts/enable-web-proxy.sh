#!/bin/bash
# Enable nginx HTTPS proxy for universal-chess-web.
#
# Generates the TLS certificate (if missing or issued for an old hostname) via
# the single source of truth -- the universalchess.tls module -- then enables
# the nginx site config and (re)starts nginx.
#
# Certificate generation logic (SANs from the actual hostname, regenerate on
# rename, stable filenames) lives in Python so the install path, the boot
# oneshot, and the /etc/hostname path unit all share one implementation.

set -euo pipefail

DGTCM_PATH="${DGTCM_PATH:-/opt/universalchess}"
CONFIG_DIR="${DGTCM_PATH}/config"
VENV_PYTHON="${DGTCM_PATH}/.venv/bin/python"

if ! command -v mkcert >/dev/null 2>&1; then
    echo "::: WARNING: mkcert not found, skipping TLS certificate generation."
    echo "::: Install with: sudo apt install mkcert libnss3-tools"
    echo "::: nginx will not start until certificates are generated."
    exit 1
fi

# Prefer the app venv (where universalchess + cryptography are importable). Fall
# back to system python3 only if the venv is not yet built.
if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="python3"
fi

echo "::: Generating TLS certificate for $("$PYTHON" -c 'import socket; print(socket.gethostname().split(".")[0] + ".local")' 2>/dev/null || echo 'local hostname')"
PYTHONPATH=/opt "$PYTHON" -m universalchess.tls "$CONFIG_DIR"

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/universal-chess-web /etc/nginx/sites-enabled/

if nginx -t; then
    systemctl restart nginx || systemctl start nginx || true
fi
