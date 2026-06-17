#!/bin/bash
# Enable nginx HTTPS proxy for universal-chess-web.
# Generates TLS certificates with mkcert if they don't exist yet,
# then enables the nginx site config and restarts nginx.

set -euo pipefail

DGTCM_PATH="${DGTCM_PATH:-/opt/universalchess}"
SSL_DIR="${DGTCM_PATH}/config/ssl"
CERT_FILE="${SSL_DIR}/dgt.local.pem"
KEY_FILE="${SSL_DIR}/dgt.local-key.pem"
CA_CERT="${SSL_DIR}/rootCA.pem"
CA_KEY="${SSL_DIR}/rootCA-key.pem"

# Generate TLS certificates if any are missing
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ] || [ ! -f "$CA_CERT" ] || [ ! -f "$CA_KEY" ]; then
    if ! command -v mkcert >/dev/null 2>&1; then
        echo "::: WARNING: mkcert not found, skipping TLS certificate generation."
        echo "::: Install with: sudo apt install mkcert libnss3-tools"
        echo "::: nginx will not start until certificates are generated."
        exit 1
    fi

    echo "::: Generating TLS certificates for dgt.local"
    mkdir -p "$SSL_DIR"

    export CAROOT="$SSL_DIR"
    mkcert \
        -cert-file "$CERT_FILE" \
        -key-file "$KEY_FILE" \
        dgt.local

    chmod 600 "$CA_KEY" "$KEY_FILE" 2>/dev/null || true
    echo "::: TLS certificates generated in $SSL_DIR"
fi

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/universal-chess-web /etc/nginx/sites-enabled/

if nginx -t; then
    systemctl restart nginx || systemctl start nginx || true
fi
