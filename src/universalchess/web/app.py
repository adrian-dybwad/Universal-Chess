# This file is part of the DGTCentaur Mods open source software
# ( https://github.com/EdNekebno/DGTCentaur )
#
# DGTCentaur Mods is free software: you can redistribute
# it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# DGTCentaur Mods is distributed in the hope that it will
# be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this file.  If not, see
#
# https://github.com/EdNekebno/DGTCentaur/blob/master/LICENSE.md
#
# This and any other notices must remain intact and unaltered in any
# distribution, modification, variant, or derivative of this software.

# Defer annotation evaluation so PEP 604 unions (e.g. `dict[...] | None`) parse
# as strings instead of executing at import time. Without this, module-level
# annotations like `_piece_images: dict[str, Image.Image] | None = None` raise
# TypeError on Python 3.9, which the board still targets (Raspberry Pi OS
# Bullseye). Matches the convention used across the rest of the package.
from __future__ import annotations

from flask import Flask, render_template, Response, request, redirect, send_file, send_from_directory, abort, stream_with_context, url_for, jsonify
from werkzeug.exceptions import NotFound
from werkzeug.utils import secure_filename
from universalchess.utils.safe_path import safe_under_base
from universalchess.db import models
from universalchess.paths import get_current_fen, get_current_placement, get_resource_path
from universalchess.services.game_broadcast import get_subscriber, GameState
from universalchess.paths import EPAPER_STATIC_JPG, CONFIG_DIR
from .chessboard import LiveBoard
from . import centaurflask
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine, MetaData
from sqlalchemy.sql import func
from sqlalchemy import select
from sqlalchemy import delete
import os
import time
import pathlib
import io
import functools
import threading
import tempfile
import datetime
import chess
import chess.pgn
import json
import urllib.parse
import base64
import pwd
import subprocess  # nosec B404 - subprocess is only ever invoked with fixed argv lists, never shell=True
from xml.sax.saxutils import escape  # nosec B406 - saxutils.escape performs output encoding (escaping), not XML parsing

from universalchess.web.piece_svg import (
    generate_piece_svg,
    PieceSvgOptions,
    get_piece_images,
)

# Conditionally import crypt (removed in Python 3.13+, may not be available)
try:
    import crypt
    HAS_CRYPT = True
except ImportError:
    HAS_CRYPT = False

# Conditionally import spwd (removed in Python 3.13+, may not be available)
try:
    import spwd
    HAS_SPWD = True
except ImportError:
    HAS_SPWD = False

# Preferred password-auth backend: the PyPI "python-pam" module, whose
# pam.pam().authenticate(username, password) API is what verify_webdav_authentication
# relies on. This is required on Python 3.13+ where the crypt/spwd fallbacks below
# no longer exist in the stdlib.
#
# Note: do NOT fall back to Debian's "PAM" (uppercase, the PyPAM C extension). It
# imports successfully but exposes a conversation-callback API with no
# (username, password) authenticate() - aliasing it here would set HAS_PAM=True
# while every authentication silently fails. When python-pam is absent we leave
# HAS_PAM False and rely on the crypt/spwd fallbacks (older OS/Python).
HAS_PAM = False
try:
    import pam
    HAS_PAM = True
except ImportError as e:
    import sys
    print(
        f"Warning: python-pam module not available: {e}. "
        "Install it into the venv with: pip install python-pam",
        file=sys.stderr,
    )

app = Flask(__name__)
app.config['UCI_UPLOAD_EXTENSIONS'] = ['.txt']
app.config['UCI_UPLOAD_PATH'] = str(pathlib.Path(__file__).parent.resolve()) + "/../engines/"

# React app static files directory (built during deb package build)
# In production: /opt/universalchess/web/react-app
# In development: ../web-app/dist (relative to this file)
REACT_APP_DIR = pathlib.Path(__file__).parent.parent / "web" / "react-app"
REACT_DEV_DIR = pathlib.Path(__file__).parent.parent / "web-app" / "dist"

def get_react_app_dir():
    """Get the React app directory, preferring production path."""
    if REACT_APP_DIR.exists():
        return REACT_APP_DIR
    if REACT_DEV_DIR.exists():
        return REACT_DEV_DIR
    return None

# Cache control settings
CACHE_LONG = 86400 * 7  # 7 days for static assets that rarely change
CACHE_SHORT = 3600      # 1 hour for assets that may change
CACHE_NONE = 0          # No caching for dynamic content

# Content Security Policy applied to every response.
# Notes on the relaxations (each is required by an existing, trusted feature):
#   - script-src 'unsafe-inline': the legacy Jinja templates (configure.html,
#     pgn.html, analyse.html, ...) embed inline <script> blocks. The React build
#     uses external bundles and does not rely on this.
#   - script-src 'wasm-unsafe-eval' + worker-src blob:: the React analysis board
#     runs Stockfish compiled to WebAssembly inside a Web Worker.
#   - connect-src 'self': SSE (/events) and the JSON API are same-origin.
# object-src 'none', base-uri 'self' and frame-ancestors 'self' are the
# meaningful hardening (no plugins, no <base> hijack, no framing/clickjacking).
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'self'",
])


def apply_security_headers(response):
    """Attach baseline security headers to a response.

    Sets nosniff, anti-clickjacking, a strict referrer policy and the CSP.
    Applied to all responses so API/SSE/media paths are covered too; the CSP
    only constrains document/script execution contexts.
    """
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('Content-Security-Policy', CONTENT_SECURITY_POLICY)
    return response


# File extensions that should be cached
CACHEABLE_EXTENSIONS = {
    '.js': CACHE_LONG,
    '.css': CACHE_LONG,
    '.woff': CACHE_LONG,
    '.woff2': CACHE_LONG,
    '.ttf': CACHE_LONG,
    '.eot': CACHE_LONG,
    '.png': CACHE_LONG,
    '.jpg': CACHE_LONG,
    '.jpeg': CACHE_LONG,
    '.gif': CACHE_LONG,
    '.svg': CACHE_LONG,
    '.ico': CACHE_LONG,
    '.bmp': CACHE_LONG,
    '.webp': CACHE_LONG,
}


@app.after_request
def add_cache_headers(response):
    """Add security headers (always) and cache headers (by content/path)."""
    # Security headers apply to every response, including the early returns
    # below for already-cached or dynamic content.
    apply_security_headers(response)

    # Skip cache handling if Cache-Control already set (e.g., SSE, dynamic).
    if 'Cache-Control' in response.headers:
        return response
    
    path = request.path
    
    # Static files - cache based on extension
    if path.startswith('/static/'):
        ext = os.path.splitext(path)[1].lower()
        max_age = CACHEABLE_EXTENSIONS.get(ext, CACHE_SHORT)
        response.headers['Cache-Control'] = f'public, max-age={max_age}'
        return response
    
    # Piece SVGs - cache long (they're generated but don't change)
    if path.startswith('/pieces/') and path.endswith('.svg'):
        response.headers['Cache-Control'] = f'public, max-age={CACHE_LONG}'
        return response
    
    # Dynamic content - no cache
    # FEN, events, and other API endpoints
    if path in ('/fen', '/events', '/placement') or path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    
    # HTML pages - short cache with revalidation
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response
    
    # Default - short cache
    response.headers['Cache-Control'] = f'public, max-age={CACHE_SHORT}'
    return response


# ---------------------------------------------------------------------------
# Inactivity timer reset: any user-initiated web request resets the board's
# sleep countdown.  Fires only for API and legacy action endpoints (POST or
# GET to /api/*), not for static assets, SSE streams, or the periodic /fen
# poll.  Best-effort: failures are silently ignored so a broken IPC socket
# never blocks the web response.
# ---------------------------------------------------------------------------
_INACTIVITY_RESET_PREFIXES = ("/api/",)
_INACTIVITY_RESET_EXACT = (
    "/configure", "/deletegame/", "/lichesskey/", "/lichessrange/",
    "/menuoptions/", "/return2dgtcentaurmods",
    "/uploadengine", "/delengine/", "/rodentivtuner",
)


@app.after_request
def reset_board_inactivity(response):
    """Signal user activity to the board so the sleep timer resets."""
    path = request.path
    if any(path.startswith(p) for p in _INACTIVITY_RESET_PREFIXES) or (
        request.method == "POST"
        and any(path.startswith(p) for p in _INACTIVITY_RESET_EXACT)
    ):
        try:
            from universalchess.services.game_broadcast import send_board_command
            send_board_command("reset_inactivity")
        except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
    return response


# System paths for conditional features
ENGINES_DIR = "/opt/universalchess/engines"
RODENTIV_PATH = os.path.join(ENGINES_DIR, "rodentIV")

# WebDAV security constants
WEBDAV_BASE_PATH = str(pathlib.Path.home())


def _internal_error(exception):
    """Build a generic 500 JSON error response and log the real exception.

    Prevents leaking internal details (file paths, DB errors, stack traces) to
    the client while preserving them server-side for debugging. Every catch-all
    ``except Exception`` handler should use this instead of ``str(e)``.
    """
    app.logger.exception("Internal error: %s", exception)
    return jsonify({"success": False, "error": "Internal server error"}), 500


def is_rodentiv_installed() -> bool:
    """Check if Rodent IV engine is installed."""
    return os.path.isfile(RODENTIV_PATH) and os.access(RODENTIV_PATH, os.X_OK)


def is_centaur_software_installed() -> bool:
    """Check if a complete original DGT Centaur install is present.

    Delegates to ``centaur_app_installed`` (the shared gate) so the template
    flag, the /api/system/info probe, the on-board menu, and the launcher all
    agree on one definition: the executable *plus* engines/ and fonts/. Requiring
    the full set means a partial import is never offered for launch (it would
    hang on the splash with no engine/fonts).
    """
    from universalchess.services.centaur_import import centaur_app_installed

    return centaur_app_installed()


@app.context_processor
def inject_template_globals():
    """Inject global variables into all templates."""
    # Static asset versioning: used to bust caches (browser + service worker).
    # Use installed /opt/universalchess/VERSION when available; fall back to a
    # fixed development string so it doesn't change on every request.
    static_version = "dev"
    try:
        version_path = pathlib.Path("/opt/universalchess/VERSION")
        if version_path.exists():
            static_version = version_path.read_text().strip() or static_version
    except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
        pass
    return {
        'rodentiv_installed': is_rodentiv_installed(),
        'centaur_software_installed': is_centaur_software_installed(),
        'static_version': static_version,
    }

def verify_webdav_authentication():
    """
    Verifies HTTP Basic Authentication for WebDAV requests.
    Checks that the user is a valid local system user and password is correct.
    
    Returns:
        Tuple (is_authenticated, username) where is_authenticated is True if
        the request has valid credentials for a local system user, username is
        the authenticated username or None.
    """
    auth_header = request.headers.get("Authorization", "")
    
    if not auth_header.startswith("Basic "):
        return (False, None)
    
    try:
        # Decode Basic Auth credentials
        encoded_credentials = auth_header[6:]  # Remove "Basic "
        
        # Decode base64
        try:
            decoded_bytes = base64.b64decode(encoded_credentials, validate=True)
            decoded_credentials = decoded_bytes.decode("utf-8")
        except Exception as e:
            app.logger.warning(f"WebDAV auth: Base64 decode failed: {e}")
            return (False, None)
        
        # Split username and password
        if ":" not in decoded_credentials:
            app.logger.warning(f"WebDAV auth: Invalid credential format")
            return (False, None)
        
        username, password = decoded_credentials.split(":", 1)
        username = username.strip()
        password = password.strip()
        
        if not username:
            return (False, None)
        
        # Detect macOS Finder placeholder credentials and reject early
        if username.lower().startswith("no user") or (len(username) > 0 and len(password) == 0 and username.lower() in ["", "guest", "anonymous"]):
            return (False, None)
        
        # Reject empty passwords for security
        if len(password) == 0:
            return (False, None)
    except Exception as e:
        app.logger.warning(f"WebDAV auth: Failed to decode credentials: {e}")
        return (False, None)
    
    # Verify user exists in system
    try:
        pwd_entry = pwd.getpwnam(username)
    except KeyError:
        return (False, None)
    
    # Verify password using available authentication method
    password_valid = False
    
    # Try PAM authentication first (most reliable on Linux systems)
    if HAS_PAM:
        try:
            p = pam.pam()
            if p.authenticate(username, password):
                password_valid = True
        except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
    
    # If PAM not available, try crypt-based verification
    if not password_valid:
        try:
            hashed_password = None
            
            # Try shadow password first if available
            if HAS_SPWD:
                try:
                    spwd_entry = spwd.getspnam(username)
                    hashed_password = spwd_entry.sp_pwd
                except (KeyError, PermissionError, OSError):  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
                    pass
            
            # Fall back to regular password database if shadow not available or accessible
            if hashed_password is None:
                hashed_password = pwd_entry.pw_passwd
                # If password hash is 'x', it means password is in shadow file
                # If spwd is not available, we'll need to use subprocess fallback
                if hashed_password == 'x':  # noqa: S105  # nosec B105 - shadow-file sentinel 'x' (password lives in /etc/shadow), not a credential
                    hashed_password = None  # Set to None to skip crypt verification and use subprocess
            
            # Only check for empty/disabled passwords if hashed_password is not None
            # (None means we're skipping crypt verification to use subprocess fallback)
            if hashed_password is not None:
                # Empty password hash means no password set - deny for security
                if not hashed_password or hashed_password == '*':  # noqa: S105  # nosec B105 - shadow-file sentinel '*' (account disabled), not a credential
                    return (False, None)
            
            # Use crypt module if available (and hashed_password is not None)
            if HAS_CRYPT and hashed_password is not None:
                try:
                    if hashed_password.startswith('$'):
                        # Modern crypt format (SHA-256, SHA-512, etc.)
                        computed = crypt.crypt(password, hashed_password)
                        if computed == hashed_password:
                            password_valid = True
                    else:
                        # Traditional DES crypt (deprecated but still used)
                        computed = crypt.crypt(password, hashed_password[:2])
                        if computed == hashed_password:
                            password_valid = True
                except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
                    pass
        
        except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
    
    # Final fallback: use subprocess to verify via system authentication
    # This is less reliable as su may require TTY
    if not password_valid:
        proc = None
        try:
            # Use expect-like approach via subprocess
            proc = subprocess.Popen(  # noqa: S603  # nosec B603 B607 - argv list (no shell); 'su' is a standard system binary; deliberate credential probe
                ['su', username, '-c', 'echo SUCCESS'],  # noqa: S607 - argv list (no shell); 'su' is a standard system binary; deliberate credential probe
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = proc.communicate(input=password + '\n', timeout=2)
            # If authentication succeeded, we should see "SUCCESS" in output
            if proc.returncode == 0 and 'SUCCESS' in stdout:
                password_valid = True
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError, OSError):  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
        finally:
            # Ensure subprocess resources are cleaned up
            if proc is not None:
                try:
                    # Close pipes if they're still open
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
                    if proc.stdout and not proc.stdout.closed:
                        proc.stdout.close()
                    if proc.stderr and not proc.stderr.closed:
                        proc.stderr.close()
                    # Terminate process if still running
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
                    pass
    
    if password_valid:
        return (True, username)
    
    return (False, None)

def require_webdav_authentication():
    """
    Checks if WebDAV request is authenticated. If not, returns 401 response.
    
    Returns:
        Response object with 401 status if not authenticated, None if authenticated
    """
    is_authenticated, username = verify_webdav_authentication()
    if not is_authenticated:
        response = Response('Authentication required', mimetype='text/plain', status=401)
        response.headers['WWW-Authenticate'] = 'Basic realm="WebDAV"'
        # Add CORS headers if needed for browser-based clients
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS, PROPFIND, MOVE, MKCOL, LOCK, UNLOCK, PROPPATCH'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, Depth'
        return response
    return None


def requires_auth(f):
    """Decorator to require HTTP Basic Auth for API endpoints.
    
    Uses the same authentication mechanism as WebDAV (Linux system users).
    Returns 401 if not authenticated.
    
    Note: Uses 'xBasic' instead of 'Basic' to prevent browser from showing
    its native login dialog. The React app handles authentication UI instead.
    """
    from functools import wraps
    
    @wraps(f)
    def decorated(*args, **kwargs):
        is_authenticated, username = verify_webdav_authentication()
        if not is_authenticated:
            response = Response(
                json.dumps({"error": "Authentication required"}),
                mimetype='application/json',
                status=401
            )
            # Use 'xBasic' to suppress browser's native auth dialog
            # The frontend React app shows its own LoginDialog instead
            response.headers['WWW-Authenticate'] = 'xBasic realm="Universal Chess"'
            return response
        return f(*args, **kwargs)
    return decorated


def sanitize_path(request_path):
    """
    Sanitizes and validates a request path to prevent path traversal attacks.
    
    Args:
        request_path: The raw path from the request
        
    Returns:
        A tuple (is_valid, sanitized_path) where is_valid is True if the path
        is safe, and sanitized_path is the normalized path.
    """
    if not request_path:
        return (False, None)
    
    # Remove newlines and other control characters
    sanitized = request_path.replace("\n", "").replace("\r", "").replace("\t", "")
    
    # Decode URL encoding to detect encoded path traversal attempts
    try:
        sanitized = urllib.parse.unquote(sanitized)
    except Exception:
        return (False, None)
    
    # Check for path traversal attempts before normalization
    if ".." in request_path or ".." in sanitized:
        return (False, None)
    
    # Normalize and contain under the WebDAV base using the shared guard. This
    # avoids pathlib.Path.resolve()/relative_to (not recognized as a path
    # sanitizer by static analysis) in favour of os.path.realpath + startswith.
    try:
        base_real = os.path.realpath(WEBDAV_BASE_PATH)
        contained = safe_under_base(WEBDAV_BASE_PATH, sanitized.lstrip("/") or ".")
        if contained is None:
            return (False, None)
        relative_str = os.path.relpath(contained, base_real)
        # Return as absolute path starting with /
        return (True, "/" + relative_str if relative_str != "." else "/")
    except Exception:
        return (False, None)

def escape_xml(text):
    """
    Escapes XML special characters to prevent XML injection attacks.
    
    Args:
        text: The text to escape
        
    Returns:
        Escaped text safe for XML
    """
    if text is None:
        return ""
    return escape(str(text), {"'": "&apos;", '"': "&quot;"})

def normalize_path(path):
    """
    Normalizes a path by removing trailing slashes.
    
    Args:
        path: The path to normalize
        
    Returns:
        Normalized path
    """
    if path != "/" and path[-1:] == "/":
        return path[:len(path)-1]
    return path

def format_date_iso(timestamp):
    """
    Formats a timestamp as ISO 8601 date string.
    
    Args:
        timestamp: Unix timestamp or datetime
        
    Returns:
        ISO 8601 formatted date string
    """
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(timestamp))

def format_date_rfc(timestamp):
    """
    Formats a timestamp as RFC 1123 date string.
    
    Args:
        timestamp: Unix timestamp or datetime
        
    Returns:
        RFC 1123 formatted date string
    """
    return time.strftime('%a, %d %b %Y %H:%M:%S %Z', time.localtime(timestamp))

def build_file_properties_xml(file_path, href_path):
    """
    Builds XML properties for a file or directory.
    
    Args:
        file_path: Full filesystem path to the file/directory
        href_path: WebDAV path for href (will be escaped)
        
    Returns:
        XML string with file properties
    """
    props = []
    props.append('<D:response>')
    props.append('<D:href>' + escape_xml(href_path) + '</D:href>')
    props.append('<D:propstat>')
    props.append('<D:prop>')
    
    if os.path.isfile(file_path):
        props.append('<D:getcontentlength>' + str(os.path.getsize(file_path)) + '</D:getcontentlength>')
    
    props.append('<D:resourcetype>')
    if os.path.isdir(file_path):
        props.append('<D:collection/>')
    props.append('</D:resourcetype>')
    
    props.append('<D:creationdate>' + format_date_iso(os.path.getctime(file_path)) + '</D:creationdate>')
    props.append('<D:lastmodified>' + format_date_rfc(os.path.getmtime(file_path)) + '</D:lastmodified>')
    
    props.append('</D:prop>')
    props.append('<D:status>HTTP/1.1 200 OK</D:status>')
    props.append('</D:propstat>')
    props.append('</D:response>')
    
    return '\n'.join(props)

def build_collection_properties_xml(href_path, creation_date=None, last_modified=None):
    """
    Builds XML properties for a virtual collection (like /PGNs).
    
    Args:
        href_path: WebDAV path for href (will be escaped)
        creation_date: Optional creation date string (ISO format)
        last_modified: Optional last modified date string (RFC format)
        
    Returns:
        XML string with collection properties
    """
    if creation_date is None:
        creation_date = '2003-07-01T01:01:00Z'
    if last_modified is None:
        last_modified = 'Thu, 21 Sep 2023 18:50:14 BST'
    
    props = []
    props.append('<D:response>')
    props.append('<D:href>' + escape_xml(href_path) + '</D:href>')
    props.append('<D:propstat>')
    props.append('<D:prop>')
    props.append('<D:resourcetype>')
    props.append('<D:collection/>')
    props.append('</D:resourcetype>')
    props.append('<D:creationdate>' + creation_date + '</D:creationdate>')
    props.append('<D:lastmodified>' + last_modified + '</D:lastmodified>')
    props.append('</D:prop>')
    props.append('<D:status>HTTP/1.1 200 OK</D:status>')
    props.append('</D:propstat>')
    props.append('</D:response>')
    
    return '\n'.join(props)

def build_pgn_properties_xml(gameitem, href_base="/PGNs/"):
    """
    Builds XML properties for a PGN file entry.
    
    Args:
        gameitem: Dictionary with game data (id, source, event, created_at)
        href_base: Base path for href (default "/PGNs/")
        
    Returns:
        XML string with PGN file properties
    """
    pgn_name = gameitem["id"] + "_" + gameitem["source"] + "_" + gameitem["event"].replace(" ", "_") + '.pgn'
    safe_pgn_name = escape_xml(pgn_name)
    href_path = href_base + safe_pgn_name
    
    created_at = gameitem["created_at"]
    creation_date_iso = created_at.replace(" ", "T") + "Z"
    
    props = []
    props.append('<D:response>')
    props.append('<D:href>' + href_path + '</D:href>')
    props.append('<D:propstat>')
    props.append('<D:prop>')
    props.append('<D:getcontentlength>0</D:getcontentlength>')
    props.append('<D:resourcetype></D:resourcetype>')
    props.append('<D:creationdate>' + creation_date_iso + '</D:creationdate>')
    props.append('<D:lastmodified>' + created_at + '</D:lastmodified>')
    props.append('</D:prop>')
    props.append('<D:status>HTTP/1.1 200 OK</D:status>')
    props.append('</D:propstat>')
    props.append('</D:response>')
    
    return '\n'.join(props)

def build_multistatus_xml(responses):
    """
    Builds a complete WebDAV multistatus XML response.
    
    Args:
        responses: List of XML response strings
        
    Returns:
        Complete multistatus XML string
    """
    xml = ['<?xml version="1.0" encoding="utf-8" ?><D:multistatus xmlns:D="DAV:">']
    xml.extend(responses)
    xml.append('</D:multistatus>')
    return '\n'.join(xml)

def get_game_data_from_session(session, game_id):
    """
    Retrieves game data from the database session.
    
    Args:
        session: SQLAlchemy session
        game_id: Game ID to retrieve
        
    Returns:
        Tuple of game data or None if not found
    """
    gamedata = session.execute(
        select(models.Game.created_at, models.Game.source, models.Game.event, 
               models.Game.site, models.Game.round, models.Game.white, 
               models.Game.black, models.Game.result, models.Game.id).
        where(models.Game.id == game_id)
    ).first()
    return gamedata

def build_gameitem_from_gamedata(gamedata):
    """
    Builds a gameitem dictionary from database gamedata tuple.
    
    Args:
        gamedata: Tuple from database query
        
    Returns:
        Dictionary with game item data
    """
    gameitem = {}
    gameitem["id"] = str(gamedata[8])
    gameitem["created_at"] = str(gamedata[0])
    src = os.path.basename(str(gamedata[1]))
    if src.endswith('.py'):
        src = src[:-3]
    gameitem["source"] = src
    gameitem["event"] = str(gamedata[2])
    gameitem["site"] = str(gamedata[3])
    gameitem["round"] = str(gamedata[4])
    gameitem["white"] = str(gamedata[5])
    gameitem["black"] = str(gamedata[6])
    gameitem["result"] = str(gamedata[7])
    return gameitem

def join_path(base_path, *parts):
    """
    Safely joins path components, handling edge cases.
    
    Args:
        base_path: Base path (should not end with /)
        *parts: Additional path components
        
    Returns:
        Joined path string
    """
    if base_path == "/":
        return "/" + "/".join(str(p) for p in parts if p)
    else:
        parts_str = "/".join(str(p) for p in parts if p)
        if parts_str:
            return base_path + "/" + parts_str
        return base_path

def get_engine_path():
    """
    Gets the engine directory path.
    
    Returns:
        Path string to the engines directory
    """
    return str(pathlib.Path(__file__).parent.resolve()) + "/../engines/"

def resolve_engine_file(filename):
    """Resolve an engine filename to a safe path inside the engines directory.

    Defends /uploadengine and /delengine against path traversal: the name is
    run through secure_filename (which strips directory separators and parent
    references) and safe_under_base then verifies the result is contained in the
    engines directory. A direct-child check rejects any residual nesting.
    Returns the resolved absolute path string, or None if the name is empty or
    would escape the engines directory.
    """
    if not filename:
        return None
    safe = secure_filename(filename)
    if not safe:
        return None
    target = safe_under_base(get_engine_path(), safe)
    if target is None or os.path.dirname(target) != os.path.realpath(get_engine_path()):
        return None
    return target

def extract_game_id_from_path(path):
    """
    Extracts game ID from a PGN path string.
    
    Args:
        path: Path like "/PGNs/123_source_event.pgn"
        
    Returns:
        Game ID as integer if valid, None otherwise
    """
    if not path or len(path) < 7:
        return None
    idnum = path[6:]  # Skip "/PGNs/"
    idnum = idnum[:idnum.find("_")] if "_" in idnum else idnum[:idnum.find(".")]
    if idnum.isdigit():
        return int(idnum)
    return None

def parse_fen_to_board_string(fen):
    """
    Converts FEN notation to a board string representation.
    
    Args:
        fen: FEN string
        
    Returns:
        Board string with pieces in order
    """
    board = fen.replace("/", "")
    # Replace numbers with spaces
    for num in range(1, 9):
        board = board.replace(str(num), " " * num)
    return board

def paste_chess_piece(image, piece_char, piece_image, x_offset, y_offset, col, row, sqsize):
    """
    Pastes a chess piece image onto the board if the piece character matches.
    
    Args:
        image: PIL Image to paste onto
        piece_char: Character representing the piece ('r', 'b', 'n', 'q', 'k', 'p', or uppercase)
        piece_image: PIL Image of the piece to paste
        x_offset: X offset for board position
        y_offset: Y offset for board position
        col: Column (0-7)
        row: Row (0-7)
        sqsize: Size of each square
    """
    x_pos = x_offset + 18 + int(col * sqsize + 1)
    y_pos = y_offset + 16 + int(row * sqsize + 1)
    image.paste(piece_image, (x_pos, y_pos), piece_image)

def draw_chess_board(draw, x_offset, y_offset, sqsize):
    """
    Draws a chess board background with alternating square colors.
    
    Args:
        draw: PIL ImageDraw object
        x_offset: X offset for board position
        y_offset: Y offset for board position
        sqsize: Size of each square
    """
    col = 229
    xp = x_offset + 16
    yp = y_offset + 16
    for r in range(0, 8):
        if r / 2 == r // 2:
            col = 229
        else:
            col = 178
        for c in range(0, 8):
            draw.rectangle([(xp, yp), (xp + sqsize, yp + sqsize)], fill=(col, col, col), outline=(col, col, col))
            xp = xp + sqsize
            if col == 178:
                col = 229
            else:
                col = 178
        yp = yp + sqsize
        xp = x_offset + 16

def render_chess_pieces(image, curfen, piece_images, x_offset, y_offset, sqsize):
    """
    Renders chess pieces onto the board image based on FEN board string.
    
    Args:
        image: PIL Image to render onto
        curfen: Board string from FEN (64 characters)
        piece_images: Dictionary mapping piece chars to PIL Images
        x_offset: X offset for board position
        y_offset: Y offset for board position
        sqsize: Size of each square
    """
    row = 0
    col = 0
    for r in range(0, 64):
        item = curfen[r]
        if item in piece_images:
            paste_chess_piece(image, item, piece_images[item], x_offset, y_offset, col, row, sqsize)
        col = col + 1
        if col == 8:
            col = 0
            row = row + 1

def convert_menu_option(value):
    """
    Converts menu option from true/false to checked/unchecked.
    
    Args:
        value: "true", "false", "checked", or "unchecked"
        
    Returns:
        "checked" or "unchecked"
    """
    if value == "true":
        return "checked"
    elif value == "false":
        return "unchecked"
    return value

def get_menu_option_display(getter_func):
    """
    Gets menu option display value (checked or empty string).
    
    Args:
        getter_func: Function that returns "checked" or "unchecked"
        
    Returns:
        "checked" or ""
    """
    value = getter_func() or "checked"
    if value == "unchecked":
        return ""
    return value

def build_chess_game_from_id(session, game_id):
    """
    Builds a chess.pgn.Game object from a game ID in the database.
    
    Args:
        session: SQLAlchemy session
        game_id: Game ID to retrieve
        
    Returns:
        chess.pgn.Game object or None if not found
    """
    gamedata = session.execute(
        select(models.Game.created_at, models.Game.source, models.Game.event, 
               models.Game.site, models.Game.round, models.Game.white, 
               models.Game.black, models.Game.result).
        where(models.Game.id == game_id)
    ).first()
    
    if not gamedata:
        return None
    
    g = chess.pgn.Game()
    
    # Build source name
    src = os.path.basename(str(gamedata[1]))
    if src.endswith('.py'):
        src = src[:-3]
    
    # Set headers
    g.headers["Source"] = src
    g.headers["Date"] = str(gamedata[0])
    g.headers["Event"] = str(gamedata[2])
    g.headers["Site"] = str(gamedata[3])
    g.headers["Round"] = str(gamedata[4])
    g.headers["White"] = str(gamedata[5])
    g.headers["Black"] = str(gamedata[6])
    g.headers["Result"] = str(gamedata[7])
    
    # Clean up None values
    for key in g.headers:
        if g.headers[key] == "None":
            g.headers[key] = ""
    
    # Get moves ordered by time
    moves = session.execute(
        select(models.GameMove.move_at, models.GameMove.move, models.GameMove.fen).
        where(models.GameMove.gameid == game_id).
        order_by(models.GameMove.id)
    ).all()
    
    # Add moves to game
    # First record is initial position (empty move), subsequent records are actual moves
    node = g
    for move_data in moves:
        move_str = move_data[1]
        if move_str:  # Skip empty moves (initial position record)
            try:
                move = chess.Move.from_uci(move_str)
                node = node.add_variation(move)
            except ValueError:  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
                # Invalid move, skip
                pass
    
    return g

def get_db_session():
    """
    Creates and returns a new database session.
    
    Returns:
        SQLAlchemy session object
    """
    Session = sessionmaker(bind=models.engine)
    return Session()

def generate_pgn_string(game_id):
    """
    Generates a PGN string for a given game ID.
    
    Args:
        game_id: Game ID to export
        
    Returns:
        PGN string or None if game not found
    """
    session = get_db_session()
    try:
        g = build_chess_game_from_id(session, game_id)
        if not g:
            return None
        
        exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
        return g.accept(exporter)
    except Exception:
        return None
    finally:
        session.close()

@app.before_request
def handle_preflight():
    # WebDAV methods that require authentication
    webdav_methods = ["PROPFIND", "DELETE", "PUT", "MOVE", "MKCOL", "LOCK", "UNLOCK", "PROPPATCH"]
    
    # OPTIONS method doesn't require auth (needed for WebDAV discovery)
    if request.method == "OPTIONS":
        res = Response()
        res.headers['Allow'] = 'OPTIONS, GET, HEAD, PROPFIND, DELETE, PUT, MOVE, MKCOL, LOCK, UNLOCK, PROPPATCH'
        res.headers['DAV'] = "1,2"
        return res
    
    # Check authentication for all WebDAV methods except OPTIONS
    if request.method in webdav_methods:
        auth_response = require_webdav_authentication()
        if auth_response:
            return auth_response
    
    # GET method for WebDAV (when User-Agent indicates WebDAV client)
    if request.method == "GET":
        user_agent = request.headers.get("User-Agent", "").lower()
        # Only require auth for WebDAV GET requests
        if user_agent.find("webdav") >= 0 or user_agent.find("cyberduck") >= 0:
            auth_response = require_webdav_authentication()
            if auth_response:
                return auth_response

    # Override PROPFIND
    if request.method == "PROPFIND":
        # Sanitize and validate the path
        is_valid, thispath = sanitize_path(request.path)
        if not is_valid:
            return Response('', mimetype='application/xml', status=403)
        
        thispath = normalize_path(thispath)
        
        if thispath == "/":
            responses = []
            # Root directory properties - build as collection with explicit 0 size
            root_props = build_collection_properties_xml(
                "/", 
                creation_date=format_date_iso(os.path.getctime(WEBDAV_BASE_PATH)),
                last_modified=format_date_rfc(os.path.getctime(WEBDAV_BASE_PATH))
            )
            # Insert getcontentlength after resourcetype
            root_props = root_props.replace(
                '</D:resourcetype>',
                '</D:resourcetype>\n<D:getcontentlength>0</D:getcontentlength>'
            )
            responses.append(root_props)
            
            # Depth 1: list contents
            if int(request.headers.get("Depth", 0)) == 1:
                full_base_dir = safe_under_base(WEBDAV_BASE_PATH, thispath)
                if full_base_dir is not None and os.path.isdir(full_base_dir):
                    for fn in os.listdir(full_base_dir):
                        full_file_path = join_path(WEBDAV_BASE_PATH, fn)
                        href_path = join_path(thispath, fn)
                        responses.append(build_file_properties_xml(full_file_path, href_path))
                
                # Add virtual PGNs directory
                responses.append(build_collection_properties_xml("/PGNs"))
            
            xml_response = build_multistatus_xml(responses)
            return Response(xml_response, mimetype='application/xml', status=207)
        elif thispath == "/PGNs":
            # Return a list of PGN games
            responses = []
            responses.append(build_collection_properties_xml("/PGNs"))
            
            # Depth 1: list PGN files
            if int(request.headers.get("Depth", 0)) == 1:
                session = get_db_session()
                try:
                    gamedata = session.execute(
                        select(models.Game.created_at, models.Game.source, models.Game.event, 
                               models.Game.site, models.Game.round, models.Game.white, 
                               models.Game.black, models.Game.result, models.Game.id).
                        order_by(models.Game.id.desc())
                    ).all()
                    
                    for x in range(min(100, len(gamedata))):
                        gameitem = build_gameitem_from_gamedata(gamedata[x])
                        responses.append(build_pgn_properties_xml(gameitem))
                except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
                    pass
                finally:
                    session.close()
            
            xml_response = build_multistatus_xml(responses)
            return Response(xml_response, mimetype='application/xml', status=207)
        elif thispath.find("/PGNs/") >= 0:
            # A PGN file properties request
            idnum = extract_game_id_from_path(thispath)
            
            if idnum is None:
                return Response("", mimetype='text/plain', status=404)
            session = get_db_session()
            try:
                gamedata = get_game_data_from_session(session, idnum)
                if not gamedata:
                    return Response("", mimetype='text/plain', status=404)
                
                gameitem = build_gameitem_from_gamedata(gamedata)
                responses = [build_pgn_properties_xml(gameitem)]
                xml_response = build_multistatus_xml(responses)
                return Response(xml_response, mimetype='application/xml', status=207)
            except Exception:
                return Response("", mimetype='text/plain', status=404)
            finally:
                session.close()            
        else:
            # Regular file or directory
            full_path = safe_under_base(WEBDAV_BASE_PATH, thispath)
            if full_path is None or not os.path.exists(full_path):
                return Response('', mimetype='application/xml', status=404)
            
            responses = []
            responses.append(build_file_properties_xml(full_path, thispath))
            
            # Depth 1: list directory contents
            if int(request.headers.get("Depth", 0)) == 1 and os.path.isdir(full_path):
                for fn in os.listdir(full_path):
                    full_file_path = join_path(full_path, fn)
                    href_path = join_path(thispath, fn)
                    responses.append(build_file_properties_xml(full_file_path, href_path))
            
            xml_response = build_multistatus_xml(responses)
            return Response(xml_response, mimetype='application/xml', status=207)        
    
    if request.method == "DELETE":
        # Deletes file or folder
        is_valid, sanitized_path = sanitize_path(request.path)
        if not is_valid or sanitized_path == "/":
            return Response('', mimetype='application/xml', status=403)
        full_path = safe_under_base(WEBDAV_BASE_PATH, sanitized_path)
        if full_path is None:
            return Response('', mimetype='application/xml', status=403)
        try:
            if os.path.isfile(full_path):
                os.remove(full_path)
            elif os.path.isdir(full_path):
                os.rmdir(full_path)
        except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
        res = Response()
        return res   
    
    if request.method == "MOVE":     
        # Validate source path
        is_valid_src, sanitized_src = sanitize_path(request.path)
        if not is_valid_src or sanitized_src == "/":
            return Response('', mimetype='application/xml', status=403)
        
        # Validate destination path
        destination = request.headers.get("Destination", "")
        if not destination:
            return Response('', mimetype='application/xml', status=400)
        
        # Extract path from destination header (format: http://host/path or /path)
        if destination.startswith("http://") or destination.startswith("https://"):
            destination = destination[destination.find("/", 8):]
        elif not destination.startswith("/"):
            destination = "/" + destination
        
        is_valid_dst, sanitized_dst = sanitize_path(destination)
        if not is_valid_dst or sanitized_dst == "/":
            return Response('', mimetype='application/xml', status=403)
        
        full_src = safe_under_base(WEBDAV_BASE_PATH, sanitized_src)
        full_dst = safe_under_base(WEBDAV_BASE_PATH, sanitized_dst)
        if full_src is None or full_dst is None:
            return Response('', mimetype='application/xml', status=403)
        try:
            os.rename(full_src, full_dst)
        except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
        res = Response(status = 200)
        return res 

    if request.method == "PUT":    
        is_valid, sanitized_path = sanitize_path(request.path)
        if not is_valid or sanitized_path == "/":
            return Response('', mimetype='application/xml', status=403)
        
        # Block writes to PGNs directory
        if sanitized_path.find("/PGNs/") >= 0:
            return Response('', mimetype='application/xml', status=404)
        
        full_path = safe_under_base(WEBDAV_BASE_PATH, sanitized_path)
        if full_path is None:
            return Response('', mimetype='application/xml', status=403)
        try:
            # Create parent directories if needed
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(request.data)
            
            # If this file was called /777.txt then run chmod 777 on any path in it
            if sanitized_path == "/777.txt":
                try:
                    with open(WEBDAV_BASE_PATH + "/777.txt", "r") as f:
                        lines = f.readlines()
                        for x in lines:
                            try:
                                # Validate path in file before chmod
                                path_line = x.strip()
                                is_valid_chmod_path, chmod_path = sanitize_path(path_line)
                                chmod_target = safe_under_base(WEBDAV_BASE_PATH, chmod_path)
                                if is_valid_chmod_path and chmod_path != "/" and chmod_target is not None:
                                    os.chmod(chmod_target, 0o0777)  # noqa: S103  # nosec B103 - DGT Centaur WebDAV '777.txt' feature; target already contained by safe_under_base
                            except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
                                pass
                except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
                    pass
        except Exception:
            return Response('', mimetype='application/xml', status=500)
        
        res = Response(status = 201)
        return res         
    
    if request.method == "MKCOL":
        # Makes a folder
        is_valid, sanitized_path = sanitize_path(request.path)
        if not is_valid or sanitized_path == "/":
            return Response('', mimetype='application/xml', status=403)
        full_path = safe_under_base(WEBDAV_BASE_PATH, sanitized_path)
        if full_path is None:
            return Response('', mimetype='application/xml', status=403)
        try:
            os.makedirs(full_path, exist_ok=True)
        except Exception:
            return Response('', mimetype='application/xml', status=500)
        res = Response()
        return res  
    
    if request.method == "LOCK":
        # Validate path
        is_valid, sanitized_path = sanitize_path(request.path)
        if not is_valid:
            return Response('', mimetype='application/xml', status=403)
        
        # Extract lock data from request
        s = str(request.data)
        def extract_xml_tag(content, tag):
            start_tag = "<D:" + tag + ">"
            end_tag = "</D:" + tag + ">"
            start_idx = content.find(start_tag)
            if start_idx < 0:
                return ""
            start_idx += len(start_tag)
            end_idx = content.find(end_tag, start_idx)
            if end_idx < 0:
                return ""
            return content[start_idx:end_idx]
        
        locktype = escape_xml(extract_xml_tag(s, "locktype"))
        lockscope = escape_xml(extract_xml_tag(s, "lockscope"))
        lockowner = escape_xml(extract_xml_tag(s, "owner"))
        safe_path = escape_xml(sanitized_path)
        
        # Build lock response XML
        lock_response = []
        lock_response.append('<D:response>')
        lock_response.append('<D:href>' + safe_path + '</D:href>')
        lock_response.append('<D:propstat>')
        lock_response.append('<D:prop>')
        lock_response.append('<D:lockdiscovery>')
        lock_response.append('<D:activelock>')
        lock_response.append(locktype)
        lock_response.append(lockscope)
        lock_response.append('<D:depth>Infinity</D:depth>')
        lock_response.append(lockowner)
        lock_response.append('<D:timeout>Second-3600</D:timeout>')
        lock_response.append('<D:locktoken>')
        lock_response.append('<D:href>opaquelocktoken:e71d4fae-5dec-22d6-fea5-00a0c91e6be4</D:href>')
        lock_response.append('</D:locktoken>')
        lock_response.append('</D:activelock>')
        lock_response.append('</D:lockdiscovery>')
        lock_response.append('</D:prop>')
        lock_response.append('<D:status>HTTP/1.1 200 OK</D:status>')
        lock_response.append('</D:propstat>')
        lock_response.append('</D:response>')
        
        xml_response = build_multistatus_xml(['\n'.join(lock_response)])
        return Response(xml_response, mimetype='application/xml', status=207)
    
    if request.method == "UNLOCK":        
        return Response("", mimetype='text/html', status=204)     
    
    if request.method == "PROPPATCH":
        # Validate path
        is_valid, sanitized_path = sanitize_path(request.path)
        if not is_valid:
            return Response('', mimetype='application/xml', status=403)
        
        # Build simple success response
        prop_response = []
        prop_response.append('<D:response>')
        prop_response.append('<D:href>' + escape_xml(sanitized_path) + '</D:href>')
        prop_response.append('<D:propstat>')
        prop_response.append('<D:status>HTTP/1.1 200 OK</D:status>')
        prop_response.append('</D:propstat>')
        prop_response.append('</D:response>')
        
        xml_response = build_multistatus_xml(['\n'.join(prop_response)])
        return Response(xml_response, mimetype='application/xml', status=207)        

    if request.method == "GET":       
        # a webdav request
        is_valid, sanitized_path = sanitize_path(request.path)
        if not is_valid:
            return Response("", mimetype='text/plain', status=403)
        
        if sanitized_path.find("/PGNs/") >= 0 and sanitized_path != "/PGNs/desktop.ini":
            # PGN file
            game_id = extract_game_id_from_path(sanitized_path)
            if game_id is None:
                return Response("", mimetype='text/plain', status=404)
            
            pgn_string = generate_pgn_string(game_id)
            if pgn_string is None:
                return Response("", mimetype='text/plain', status=404)
            
            return Response(pgn_string, mimetype='application/xml', status=207)
        else:
            user_agent = request.headers.get("User-Agent", "").lower()
            if user_agent.find("webdav") >= 0 or user_agent.find("cyberduck") >= 0:
                full_path = safe_under_base(WEBDAV_BASE_PATH, sanitized_path)
                if full_path is None:
                    return Response("", mimetype='text/plain', status=403)
                try:
                    if os.path.isfile(full_path):
                        with open(full_path, "rb") as f:
                            contents = f.read()
                        resp = Response(contents, mimetype='application/binary', status=200)   
                        return resp
                    else:
                        return Response("", mimetype='text/plain', status=404)
                except Exception:
                    return Response("", mimetype='text/plain', status=500)          


@app.route("/", methods=["GET"])
def index():
    """Serve React app if available, otherwise fall back to legacy."""
    react_dir = get_react_app_dir()
    if react_dir:
        return send_file(react_dir / "index.html")
    # Fall back to legacy
    return render_template('index.html', fen=get_current_placement())


@app.route("/assets/<path:filename>")
def react_assets(filename):
    """Serve React app static assets (JS, CSS, etc.)."""
    react_dir = get_react_app_dir()
    if react_dir:
        try:
            return send_from_directory(react_dir / "assets", filename)
        except NotFound:  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
    abort(404)


@app.route("/icons/<path:filename>")
def react_icons(filename):
    """Serve React app icons."""
    react_dir = get_react_app_dir()
    if react_dir:
        try:
            return send_from_directory(react_dir / "icons", filename)
        except NotFound:  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
    abort(404)


@app.route("/stockfish/<path:filename>")
def react_stockfish(filename):
    """Serve Stockfish WASM files for React app analysis."""
    react_dir = get_react_app_dir()
    if react_dir:
        try:
            return send_from_directory(react_dir / "stockfish", filename)
        except NotFound:  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
    abort(404)


@app.route("/manifest.json")
def react_manifest():
    """Serve React app PWA manifest."""
    react_dir = get_react_app_dir()
    if react_dir:
        manifest_path = react_dir / "manifest.json"
        if manifest_path.exists():
            return send_file(manifest_path)
    abort(404)


@app.route("/sw.js")
def react_service_worker():
    """Serve React app service worker."""
    react_dir = get_react_app_dir()
    if react_dir:
        sw_path = react_dir / "sw.js"
        if sw_path.exists():
            return send_file(sw_path)
    abort(404)


# Legacy UI routes - serve the old Flask templates
@app.route("/legacy")
@app.route("/legacy/")
def legacy_index():
    """Legacy UI index page."""
    return render_template('index.html', fen=get_current_placement())


@app.route("/legacy/pgn")
def legacy_pgn():
    """Legacy UI games page."""
    return render_template('pgn.html')


@app.route("/legacy/configure")
def legacy_configure():
    """Legacy UI configure page."""
    return render_template('configure.html')


@app.route("/legacy/support")
def legacy_support():
    """Legacy UI support page."""
    return render_template('support.html')


@app.route("/legacy/license")
def legacy_license():
    """Legacy UI license page."""
    gpl3_path = pathlib.Path(__file__).parent.parent.parent.parent / "LICENSE"
    apache2_path = pathlib.Path(__file__).parent.parent.parent.parent / "licenses" / "Apache-2.0.txt"
    gpl3_text = gpl3_path.read_text() if gpl3_path.exists() else "GPL-3.0 license file not found."
    apache2_text = apache2_path.read_text() if apache2_path.exists() else "Apache-2.0 license file not found."
    return render_template('license.html', gpl3_text=gpl3_text, apache2_text=apache2_text)


@app.route("/legacy/analyse/<gameid>")
def legacy_analyse(gameid):
    """Legacy UI analyse page."""
    return render_template('analyse.html', game_id=gameid)


@app.route("/legacy/rodentivtuner")
def legacy_rodentivtuner():
    """Legacy UI Rodent IV tuner page."""
    return render_template('rodentivtuner.html')


@app.route("/fen")
def fen():
    return get_current_placement()

@app.route("/rodentivtuner")
def tuner():

        return render_template('rodentivtuner.html')

@app.route("/rodentivtuner" , methods=["POST"])
def tuner_upload_file():
    uploaded_file = request.files['file']
    if uploaded_file.filename != '':
        safe_name = secure_filename(uploaded_file.filename)
        if not safe_name:
            abort(400)
        file_ext = os.path.splitext(safe_name)[1]
        file_name = os.path.splitext(safe_name)[0]
        if file_ext not in app.config['UCI_UPLOAD_EXTENSIONS']:
            abort(400)
        personalities_dir = os.path.join(app.config['UCI_UPLOAD_PATH'], "personalities")
        save_path = safe_under_base(personalities_dir, safe_name)
        if save_path is None:
            abort(400)
        uploaded_file.save(str(save_path))
        with open(app.config['UCI_UPLOAD_PATH'] + "personalities/basic.ini", "r+") as file:
            for line in file:
                if file_name in line:
                    break
            else: # not found, we are at the eof
                file.write(file_name + '=' + file_name + '.txt\n') # append missing data
        with open(app.config['UCI_UPLOAD_PATH'] + "rodentIV.uci", "r+") as file:
            for line in file:
                if file_name in line:
                    break
            else: # not found, we are at the eof  
                file.write('\n') # append missing data
                file.write('[' + file_name + ']\n') # append missing data
                file.write('PersonalityFile = ' + file_name + ' ' + file_name + '.txt' + '\n') # append missing data
                file.write('UCI_LimitStrength = true\n') # append missing data
                file.write('UCI_Elo = 1200\n') # append missing data
    return render_template('index.html')
@app.route("/pgn")
def pgn():
    return render_template('pgn.html')

@app.route("/configure")
def configure():
    # Get the lichessapikey
    showEngines = get_menu_option_display(centaurflask.get_menuEngines)
    showHandBrain = get_menu_option_display(centaurflask.get_menuHandBrain)
    show1v1Analysis = get_menu_option_display(centaurflask.get_menu1v1Analysis)
    showEmulateEB = get_menu_option_display(centaurflask.get_menuEmulateEB)
    showCast = get_menu_option_display(centaurflask.get_menuCast)
    showSettings = get_menu_option_display(centaurflask.get_menuSettings)
    showAbout = get_menu_option_display(centaurflask.get_menuAbout)
    
    return render_template('configure.html', 
                         lichesskey=centaurflask.get_lichess_api(), 
                         lichessrange=centaurflask.get_lichess_range(),
                         menuEngines=showEngines, 
                         menuHandBrain=showHandBrain, 
                         menu1v1Analysis=show1v1Analysis,
                         menuEmulateEB=showEmulateEB, 
                         menuCast=showCast, 
                         menuSettings=showSettings, 
                         menuAbout=showAbout)

@app.route("/support")
def support():
    return render_template('support.html')

@app.route("/license")
def license():
    # Load license texts
    gpl3_text = ""
    apache2_text = ""
    
    # Try to load GPL-3.0 text
    gpl3_path = pathlib.Path(__file__).parent.parent.parent.parent / "LICENSE"
    if gpl3_path.exists():
        try:
            gpl3_text = gpl3_path.read_text()
        except Exception:
            gpl3_text = "See https://www.gnu.org/licenses/gpl-3.0.txt"
    else:
        gpl3_text = "See https://www.gnu.org/licenses/gpl-3.0.txt"
    
    # Try to load Apache-2.0 text for Font.ttc
    apache2_path = pathlib.Path(__file__).parent.parent.parent.parent / "licenses" / "Apache-2.0.txt"
    if apache2_path.exists():
        try:
            apache2_text = apache2_path.read_text()
        except Exception:
            apache2_text = "See https://www.apache.org/licenses/LICENSE-2.0"
    else:
        apache2_text = "See https://www.apache.org/licenses/LICENSE-2.0"
    
    return render_template('license.html', gpl3_text=gpl3_text, apache2_text=apache2_text)

@app.route("/return2dgtcentaurmods", methods=["POST"])
@requires_auth
def return2dgtcentaurmods():
    os.system("pkill centaur")  # noqa: S605,S607  # nosec B605 B607 - fixed command string, no user input; route is behind @requires_auth
    time.sleep(1)
    os.system("sudo systemctl restart universal-chess.service")  # noqa: S605,S607  # nosec B605 B607 - fixed command string, no user input; route is behind @requires_auth
    return "ok"

@app.route("/lichesskey/<key>", methods=["POST"])
@requires_auth
def lichesskey(key):
    centaurflask.set_lichess_api(key)
    os.system("sudo systemctl restart universal-chess.service")  # noqa: S605,S607  # nosec B605 B607 - fixed command string, no user input; route is behind @requires_auth
    return "ok"

@app.route("/lichessrange/<newrange>", methods=["POST"])
@requires_auth
def lichessrange(newrange):
    centaurflask.set_lichess_range(newrange)
    return "ok"

@app.route("/menuoptions/<engines>/<handbrain>/<analysis>/<emulateeb>/<cast>/<settings>/<about>", methods=["POST"])
@requires_auth
def menuoptions(engines, handbrain, analysis, emulateeb, cast, settings, about):
    centaurflask.set_menuEngines(convert_menu_option(engines))
    centaurflask.set_menuHandBrain(convert_menu_option(handbrain))
    centaurflask.set_menu1v1Analysis(convert_menu_option(analysis))
    centaurflask.set_menuEmulateEB(convert_menu_option(emulateeb))
    centaurflask.set_menuCast(convert_menu_option(cast))
    centaurflask.set_menuSettings(convert_menu_option(settings))
    centaurflask.set_menuAbout(convert_menu_option(about))
    return "ok"

@app.route("/analyse/<gameid>")
def analyse(gameid):
    return render_template('analysis.html', gameid=gameid)

@app.route("/deletegame/<gameid>", methods=["POST"])
@requires_auth
def deletegame(gameid):
    session = get_db_session()
    try:
        stmt = delete(models.GameMove).where(models.GameMove.gameid == gameid)
        session.execute(stmt)
        stmt = delete(models.Game).where(models.Game.id == gameid)
        session.execute(stmt)
        session.commit()
    finally:
        session.close()
    return "ok"

@app.route("/getgames/<page>")
def getGames(page):
    # Return batches of 10 games by listing games in reverse order
    session = get_db_session()
    try:
        gamedata = session.execute(
            select(models.Game.created_at, models.Game.source, models.Game.event, models.Game.site, models.Game.round,
                   models.Game.white, models.Game.black, models.Game.result, models.Game.id).
                order_by(models.Game.id.desc())
        ).all()
        t = (int(page) * 10) - 10
        games = {}
        try:
            for x in range(0, 10):
                if x + t < len(gamedata):
                    gameitem = build_gameitem_from_gamedata(gamedata[x + t])
                    games[x] = gameitem
        except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
        return jsonify(games)
    finally:
        session.close()

@app.route("/engines")
def engines():
    # Return a list of engines and uci files. Essentially the contents our our engines folder
    files = {}
    enginepath = get_engine_path()
    enginefiles = os.listdir(enginepath)
    for x, f in enumerate(enginefiles):
        files[x] = str(f)
    return jsonify(files)

@app.route("/uploadengine", methods=['POST'])
@requires_auth
def uploadengine():
    file = request.files.get('file')
    if file is None or file.filename == '':
        abort(400)
    target = resolve_engine_file(file.filename)
    if target is None:
        abort(400)
    file.save(str(target))
    # Engines are executed, so they need the execute bit, but must not be
    # world-writable (0o777 previously allowed any local user to replace the
    # binary). 0o755: owner-writable, group/other read+execute only.
    os.chmod(str(target), 0o755)  # noqa: S103  # nosec B103 - engine needs the exec bit; 0o755 is least-permissive; path contained by safe_under_base
    return redirect("/configure")

@app.route("/delengine/<enginename>", methods=["POST"])
@requires_auth
def delengine(enginename):
    target = resolve_engine_file(enginename)
    if target is None or not target.is_file():
        abort(404)
    os.remove(str(target))
    return "ok"

@app.route("/getpgn/<gameid>")
def makePGN(gameid):
    # Export a PGN of the specified game
    pgn_string = generate_pgn_string(int(gameid))
    if pgn_string is None:
        return "", 404
    return pgn_string


@app.route("/logo")
def logo_image():
    """Serve the knight logo from resources."""
    logo_path = get_resource_path("knight_logo.bmp")
    if os.path.exists(logo_path):
        return send_file(logo_path, mimetype='image/bmp')
    # Fallback to icon
    return redirect(url_for('static', filename='icons/icon.svg'))


@app.route("/pieces/<piece_code>.svg")
def piece_svg(piece_code: str):
    """Serve an on-the-fly SVG for chessboard.js piece rendering."""
    try:
        svg = generate_piece_svg(piece_code, options=PieceSvgOptions(size=80))
    except ValueError:
        abort(404)

    response = Response(svg, mimetype="image/svg+xml")
    response.headers['Cache-Control'] = 'public, max-age=604800, immutable'  # 7 days
    return response

# Piece images are generated from SVGs on-demand (lazy-loaded and cached)
# The size matches the original PNG pieces for video frame generation
_piece_images: dict[str, Image.Image] | None = None


def _get_piece_images() -> dict[str, Image.Image]:
    """Lazy-load piece images from SVG generation.
    
    Returns:
        Dictionary mapping FEN piece characters to PIL Images.
    """
    global _piece_images
    if _piece_images is None:
        _piece_images = get_piece_images(size=120)
    return _piece_images


logo = Image.open(str(pathlib.Path(__file__).parent.resolve()) + "/../web/static/logo_mods_web.png")
moddate = -1
sc = None
epaper_path = EPAPER_STATIC_JPG
if os.path.isfile(epaper_path):
    sc = Image.open(epaper_path)
    moddate = os.stat(epaper_path)[8]

# /video frame production is change-driven, not clock-driven. A 1920x1080 JPEG
# render takes longer than a frame interval on the board's single ARMv6 core, so
# rendering every tick pegged the core and starved the rest of the web server
# whenever any client (Chromecast, the board-control page, OBS) was connected.
# Instead a cheap fingerprint (position, plus the e-paper snapshot mtime for the
# classic layout) decides when a frame actually changed; unchanged frames reuse
# the cached JPEG. POLL is how quickly a move becomes a new frame; KEEPALIVE is
# the longest gap between frames sent on an idle board so Chromecast's LIVE
# receiver and <img>-based MJPEG viewers keep their connection open.
VIDEO_POLL_INTERVAL_SECONDS = 0.2
VIDEO_KEEPALIVE_SECONDS = 1.0

# Canonical 16:9 render size. Chromecast and OBS use the full size; the
# in-browser board-control view requests a smaller width (?w=) to cut JPEG
# encode and transfer cost. Width is clamped to [MIN, NATIVE]; upscaling is
# never done (a larger request just gets native).
VIDEO_NATIVE_WIDTH = 1920
VIDEO_NATIVE_HEIGHT = 1080
VIDEO_MIN_WIDTH = 320
VIDEO_JPEG_QUALITY = 30

try:
    _VIDEO_RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:  # Pillow < 9.1
    _VIDEO_RESAMPLE = Image.BILINEAR


def _video_target_dimensions(requested_width) -> tuple[int, int]:
    """Clamp a requested frame width to a sane 16:9 (width, height).

    Returns the canonical 1920x1080 when no/invalid width is requested. A
    smaller width reduces encode and transfer cost; the height is derived to
    preserve the 16:9 aspect so the receiver never letterboxes or stretches.
    An out-of-range or non-numeric request is clamped (or falls back) rather
    than raising, because the value comes straight from a query string.
    """
    if requested_width is None:
        return VIDEO_NATIVE_WIDTH, VIDEO_NATIVE_HEIGHT
    try:
        width = int(requested_width)
    except (TypeError, ValueError):
        return VIDEO_NATIVE_WIDTH, VIDEO_NATIVE_HEIGHT
    width = max(VIDEO_MIN_WIDTH, min(width, VIDEO_NATIVE_WIDTH))
    height = round(width * VIDEO_NATIVE_HEIGHT / VIDEO_NATIVE_WIDTH)
    return width, height


def _epaper_snapshot_mtime():
    """Return the e-paper snapshot mtime, or None when it is absent.

    Cheap change signal for the classic layout, which composites the latest
    e-paper JPEG beside the board. Uses the stat tuple's index 8 (st_mtime) to
    match _render_classic_cast_frame's own snapshot-cache check, so the
    fingerprint and the render agree on what "changed" means. None (file
    missing) is a stable value, so a missing snapshot does not by itself force
    re-renders.
    """
    try:
        return os.stat(EPAPER_STATIC_JPG)[8]
    except OSError:
        return None


def _video_frame_fingerprint(source, fen):
    """Signature of everything that affects a rendered frame for ``source``.

    A frame is re-rendered only when this value changes, which is what lets an
    idle board cost ~0 CPU. The position affects both layouts; the classic
    layout additionally composites the e-paper snapshot, so its mtime
    participates only there - including it for live_board would force needless
    re-renders on every e-paper refresh.
    """
    if source == "classic":
        return (source, fen, _epaper_snapshot_mtime())
    return (source, fen)


class _VideoFrameCache:
    """Render-once-per-change JPEG cache shared by all /video clients.

    Concurrent clients at the same key (e.g. Chromecast plus the board-control
    page) share a single render: the first to observe a new fingerprint renders
    and stores the encoded bytes under the lock; the rest reuse it. CPU cost
    therefore tracks how often the board changes, not how many clients are
    connected. Rendering runs under the lock because the classic layout also
    reads/writes the snapshot globals (sc/moddate); serializing renders on a
    single-core board is no loss and removes that latent data race.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frames = {}  # key -> (fingerprint, jpeg_bytes)

    def get(self, key, fingerprint, render):
        """Return (jpeg_bytes, rendered).

        rendered is True when ``render`` was invoked (the fingerprint changed),
        letting the caller distinguish a fresh frame from a keepalive reuse.
        """
        with self._lock:
            cached = self._frames.get(key)
            if cached is not None and cached[0] == fingerprint:
                return cached[1], False
            jpeg = render()
            self._frames[key] = (fingerprint, jpeg)
            return jpeg, True


_video_frame_cache = _VideoFrameCache()


def _parse_config_bool(value: str, default: bool = True) -> bool:
    """Parse board config booleans written as True/False, on/off, yes/no, or 1/0."""
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in ("true", "on", "1", "yes"):
        return True
    if normalized in ("false", "off", "0", "no"):
        return False
    return default


def get_chromecast_use_live_board() -> bool:
    """Return whether Chromecast /video should default to Live Board mode."""
    from universalchess.board.settings import Settings

    return _parse_config_bool(
        Settings.read("chromecast", "use_live_board", "True"),
        default=True,
    )


def set_chromecast_use_live_board(use_live_board: bool) -> None:
    """Persist the Chromecast source mode shared by web and e-paper menus."""
    from universalchess.board.settings import Settings

    Settings.write(
        "chromecast",
        "use_live_board",
        "True" if use_live_board else "False",
        "True",
    )


def _selected_chromecast_video_source() -> str:
    """Resolve the requested /video source.

    Bare /video is the long-standing classic feed. The Live Board checkbox is
    applied by Chromecast starts through an explicit source query parameter.
    """
    source = (request.args.get("source") or "").strip().lower()
    if source in ("live_board", "classic"):
        return source
    return "classic"


def _render_live_board_frame(curfen, piece_images):
    """Render the refreshed Chromecast frame: Live Board content only.

    Built in RGB (not RGBA): pieces are pasted using their own alpha as the mask,
    so no full-image alpha channel is needed and the expensive per-frame
    RGBA->RGB convert before JPEG encoding is avoided.
    """
    image = Image.new(mode="RGB", size=(1920, 1080), color=(18, 18, 18))
    draw = ImageDraw.Draw(image)
    sqsize = 130.9
    board_width = int(8 * sqsize + 32)
    x_offset = int((1920 - board_width) / 2)
    y_offset = 0

    draw.rectangle(
        [(x_offset, 0), (x_offset + board_width, 1080)],
        fill=(33, 33, 33),
        outline=(33, 33, 33),
    )
    draw.rectangle(
        [(x_offset + 9, 9), (x_offset + board_width - 9, 1071)],
        fill=(225, 225, 225),
        outline=(225, 225, 225),
    )
    draw.rectangle(
        [(x_offset + 12, 12), (x_offset + board_width - 13, 1067)],
        fill=(33, 33, 33),
        outline=(33, 33, 33),
    )

    draw_chess_board(draw, x_offset, y_offset, sqsize)
    render_chess_pieces(image, curfen, piece_images, x_offset, 16, sqsize)
    return image


def _render_classic_cast_frame(curfen, piece_images):
    """Render the classic Chromecast frame with e-paper image beside the board.

    Built in RGB (see _render_live_board_frame): the board pieces and the logo
    are pasted with their own alpha masks, and the e-paper snapshot has no alpha,
    so the full-image RGBA->RGB convert is unnecessary.
    """
    global logo, sc, moddate
    x_offset = 345
    y_offset = 16
    sqsize = 130.9

    image = Image.new(mode="RGB", size=(1920, 1080), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([(x_offset, 0), (x_offset + 1329 - 100, 1080)], fill=(33, 33, 33), outline=(33, 33, 33))
    draw.rectangle([(x_offset + 9, 9), (x_offset + 1220 - 149, 1071)], fill=(225, 225, 225), outline=(225, 225, 225))
    draw.rectangle([(x_offset + 12, 12), (x_offset + 1216 - 149, 1067)], fill=(33, 33, 33), outline=(33, 33, 33))

    draw_chess_board(draw, x_offset, 0, sqsize)
    render_chess_pieces(image, curfen, piece_images, x_offset, y_offset, sqsize)

    try:
        newmoddate = os.stat(EPAPER_STATIC_JPG)[8]
        if newmoddate != moddate or sc is None:
            with Image.open(EPAPER_STATIC_JPG) as snapshot:
                sc = snapshot.convert("RGB").copy()
            moddate = newmoddate
    except (OSError, UnidentifiedImageError) as e:
        app.logger.warning("Could not load e-paper snapshot for Chromecast classic mode: %s", e)
        if sc is None:
            sc = Image.new(mode="RGB", size=(400, 360), color=(255, 255, 255))
    image.paste(sc, (x_offset + 1216 - 130, 635))
    image.paste(logo, (x_offset + 1216 - 130, 0), logo)
    return image


def _encode_video_jpeg(image, dimensions):
    """Resize (if needed) and JPEG-encode a rendered RGB frame.

    Frames render at native 1920x1080; a smaller client gets a single downscale
    here. BILINEAR is enough for the large flat board regions and far cheaper
    than higher-order filters on an ARMv6 core.
    """
    if (image.width, image.height) != tuple(dimensions):
        image = image.resize(tuple(dimensions), _VIDEO_RESAMPLE)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=VIDEO_JPEG_QUALITY)
    return output.getvalue()


def _render_encoded_frame(source, curfen, piece_images, dimensions):
    """Render the requested layout for ``source`` and return JPEG bytes.

    Pure given its inputs, which is what makes the change-detection cache and
    its tests straightforward: identical inputs always yield the same frame.
    """
    if source == "classic":
        image = _render_classic_cast_frame(curfen, piece_images)
    else:
        image = _render_live_board_frame(curfen, piece_images)
    return _encode_video_jpeg(image, dimensions)


def _build_multipart_frame(jpeg: bytes) -> bytes:
    """Wrap encoded JPEG bytes as one multipart/x-mixed-replace part."""
    return (b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n'
            b'Content-Length: ' + f"{len(jpeg)}".encode() + b'\r\n'
            b'\r\n' + jpeg + b'\r\n')


def generateVideoFrame(source="classic", dimensions=(VIDEO_NATIVE_WIDTH, VIDEO_NATIVE_HEIGHT)):
    """Yield an MJPEG stream, re-rendering only when the frame content changes.

    The board position is polled every VIDEO_POLL_INTERVAL_SECONDS so a move
    becomes a new frame quickly, but a frame is encoded only when the
    fingerprint changes (see _video_frame_fingerprint / _VideoFrameCache). When
    nothing changes, the cached JPEG is re-sent at most every
    VIDEO_KEEPALIVE_SECONDS to keep Chromecast's LIVE receiver and <img>-based
    viewers connected - no render or re-encode happens for that keepalive. An
    idle board therefore performs essentially no work regardless of how many
    clients are connected.

    source and dimensions are resolved by the caller (the view) so the generator
    never touches the request context while streaming.
    """
    piece_images = _get_piece_images()
    key = (source, tuple(dimensions))

    last_sent = 0.0
    while True:
        loop_started = time.monotonic()
        curfen = parse_fen_to_board_string(get_current_fen())
        fingerprint = _video_frame_fingerprint(source, curfen)
        render = functools.partial(
            _render_encoded_frame, source, curfen, piece_images, dimensions
        )
        jpeg, rendered = _video_frame_cache.get(key, fingerprint, render)

        now = time.monotonic()
        if rendered or (now - last_sent) >= VIDEO_KEEPALIVE_SECONDS:
            yield _build_multipart_frame(jpeg)
            last_sent = now

        elapsed = time.monotonic() - loop_started
        remaining = VIDEO_POLL_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

# Buttons the interactive board-control page may press. Mirrors
# board.INJECTABLE_KEYS but kept local so the web process validates names without
# importing the board/hardware modules. LONG_PLAY is intentionally absent: it is
# a derived hold gesture, not a real button; a PLAY long-press (shutdown) is
# reached via long_press=True, never as a tap.
_REMOTE_KEYS = frozenset({"BACK", "TICK", "UP", "DOWN", "HELP", "PLAY"})


@app.route('/video')
def video_feed():
    source = _selected_chromecast_video_source()
    dimensions = _video_target_dimensions(request.args.get("w"))
    return Response(
        stream_with_context(generateVideoFrame(source, dimensions)),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route("/api/board/key", methods=["POST"])
@requires_auth
def api_board_key():
    """Press one of the board's physical buttons. Requires authentication.

    Backs the interactive board-control page. Forwards a ``key_press`` command to
    the main process, which injects it onto the board's key queue so it is
    handled exactly like a physical press. ``long_press`` reproduces a held
    button (e.g. PLAY long-press starts the shutdown countdown). Rejects unknown
    buttons; a short tap can never trigger the shutdown gesture because that
    requires an explicit long press of PLAY.

    Body: {"key": "BACK"|"TICK"|"UP"|"DOWN"|"HELP"|"PLAY", "long_press": bool}
    """
    try:
        body = request.get_json(silent=True) or {}
        key = (body.get("key") or "").strip().upper()
        if key not in _REMOTE_KEYS:
            return jsonify({"success": False, "error": "Unknown key"}), 400
        long_press = bool(body.get("long_press"))

        from universalchess.services.game_broadcast import send_board_command

        sent = send_board_command("key_press", {"key": key, "long_press": long_press})
        if sent:
            action = "Long-pressed" if long_press else "Pressed"
            return jsonify({"success": True, "message": f"{action} {key}"})
        return jsonify({"success": False, "error": "Board not running"}), 503
    except Exception as e:
        return _internal_error(e)

def fenToImage(fen):
    global logo
    piece_images = _get_piece_images()
    curfen = parse_fen_to_board_string(fen)
    image = Image.new(mode="RGBA", size=(1200, 1080), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([(0, 0), (1329 - 100, 1080)], fill=(33, 33, 33), outline=(33, 33, 33))
    draw.rectangle([(9, 9), (1220 - 149, 1071)], fill=(225, 225, 225), outline=(225, 225, 225))
    draw.rectangle([(12, 12), (1216 - 149, 1067)], fill=(33, 33, 33), outline=(33, 33, 33))
    
    x_offset = 0
    y_offset = 16
    sqsize = 130.9
    draw_chess_board(draw, x_offset, 0, sqsize)
    render_chess_pieces(image, curfen, piece_images, x_offset, y_offset, sqsize)
    
    image.paste(logo, (1216 - 145, 0), logo)
    image = image.resize((400, 360))
    return image

@app.route("/getgif/<gameid>")
def getgif(gameid):
    # Export a GIF animation of the specified game
    session = get_db_session()
    try:
        g = build_chess_game_from_id(session, int(gameid))
        if not g:
            return "", 404
        
        imlist = []
        board = g.board()
        imlist.append(fenToImage(board.fen()))
        for move in g.mainline_moves():
            board.push(move)
            imlist.append(fenToImage(board.fen()))
        
        membuf = io.BytesIO()
        imlist[0].save(membuf,
                   save_all=True, append_images=imlist[1:], optimize=False, duration=1000, loop=0, format='gif')
        membuf.seek(0)
        return send_file(membuf, mimetype='image/gif')
    except Exception:
        return "", 404
    finally:
        session.close()


# ==============================================================================
# Settings API
# ==============================================================================

def get_all_settings():
    """Read all settings from centaur.ini as a nested dictionary."""
    from universalchess.board.settings import Settings
    import configparser
    
    config = configparser.ConfigParser()
    config.read(Settings.configfile)
    
    result = {}
    for section in config.sections():
        result[section] = dict(config.items(section))
    
    # Also read from defaults for any missing sections
    defconfig = configparser.ConfigParser()
    defconfig.read(Settings.defconfigfile)
    for section in defconfig.sections():
        if section not in result:
            result[section] = dict(defconfig.items(section))
        else:
            # Merge defaults for missing keys
            for key, value in defconfig.items(section):
                if key not in result[section]:
                    result[section][key] = value
    
    return result


def save_all_settings(settings_dict, *, broadcast: bool = True):
    """Save all settings to centaur.ini from a nested dictionary.
    
    Args:
        settings_dict: Nested dict of settings to save.
        broadcast: If True, broadcast settings_changed event to SSE clients
                   and notify the main process to reload settings.
    """
    from universalchess.board.settings import Settings
    import configparser
    
    config = configparser.ConfigParser()
    config.read(Settings.configfile)
    
    for section, values in settings_dict.items():
        if not config.has_section(section):
            config.add_section(section)
        for key, value in values.items():
            # Handle booleans
            if isinstance(value, bool):
                value = 'True' if value else 'False'
            config.set(section, key, str(value))
    
    Settings.write_config(config)
    
    if broadcast:
        # Notify SSE clients (React app)
        broadcast_sse_event("settings_changed")
        # Notify main process to reload settings (hot reload)
        try:
            from universalchess.services.game_broadcast import notify_main_process_settings_changed
            notify_main_process_settings_changed()
        except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
            pass  # Main process notification is optional


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    """Get all settings from centaur.ini as JSON."""
    try:
        settings = get_all_settings()
        return jsonify(settings)
    except Exception as e:
        return _internal_error(e)


@app.route("/api/settings", methods=["POST"])
@requires_auth
def api_save_settings():
    """Save settings to centaur.ini from JSON body. Requires authentication."""
    try:
        settings = request.get_json()
        if not settings:
            return jsonify({"success": False, "error": "No settings provided"}), 400
        
        save_all_settings(settings)
        return jsonify({"success": True})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/settings/apply", methods=["POST"])
@requires_auth
def api_apply_settings():
    """Apply saved settings to the running chess board. Requires authentication.

    Sends a hot-reload signal to the main process (the same notification that a
    save emits) rather than restarting the service. A restart would interrupt an
    in-progress game and bounce the board; a hot reload re-reads centaur.ini and
    rebuilds the live display in place.

    Note: a small number of startup-only settings (notably DATABASE.database_uri,
    which binds the DB engine at process start) are NOT picked up by a hot reload
    and still require a manual service restart.
    """
    try:
        from universalchess.services.game_broadcast import notify_main_process_settings_changed
        notified = notify_main_process_settings_changed()
        if notified:
            return jsonify({"success": True, "message": "Settings reloaded"})
        # The main process may not be running (e.g. board service stopped); the
        # settings are still persisted and will load on next start.
        return jsonify({
            "success": True,
            "message": "Settings saved; board not running to reload",
        })
    except Exception as e:
        return _internal_error(e)


@app.route("/api/sprites", methods=["GET"])
def api_get_sprites():
    """List available chess sprite-sheet identifiers for the Sprites selector.

    The board's ResourceLoader singleton is owned by the main process, not this
    web process, so a fresh loader is constructed over the same resource
    directories to reuse its discovery logic (scans for chesssprites_<id>.bmp,
    user overrides merged over system sheets, 'default' first).
    """
    try:
        from universalchess.resources import ResourceLoader
        from universalchess.paths import RESOURCES_DIR, USER_RESOURCES_DIR

        loader = ResourceLoader(RESOURCES_DIR, USER_RESOURCES_DIR)
        sheets = loader.list_chess_sprite_sheets()
        if not sheets:
            sheets = [ResourceLoader.DEFAULT_SPRITE_SHEET]
        return jsonify(sheets)
    except Exception as e:
        app.logger.warning(f"Failed to list sprite sheets: {e}")
        return jsonify(["default"])


@app.route("/api/menu-schema", methods=["GET"])
def api_get_menu_schema():
    """Serve the shared menu catalog for the web UI to render settings from.

    The catalog (menu.json) is the single source of truth shared with the board.
    Served read-only and unauthenticated like /api/sprites; it contains only menu
    structure, labels, help tips, icons, and option sets - no secrets or values.
    """
    try:
        from universalchess.menus.catalog import get_catalog

        return jsonify(get_catalog().raw_menu())
    except Exception as e:
        app.logger.warning(f"Failed to load menu schema: {e}")
        return _internal_error(e)


@app.route("/api/positions", methods=["GET"])
def api_get_positions():
    """List predefined board positions for the web Positions page.

    Reads the same positions.ini the board uses (via load_positions_config), so
    web and board share one position catalog. Served read-only and
    unauthenticated like /api/sprites; positions contain no secrets. Shape:
        {"categories": [{"name": str,
                          "positions": [{"name", "fen", "hint"}]}]}
    """
    try:
        from universalchess.utils.positions import load_positions_config

        raw = load_positions_config(app.logger)
        categories = [
            {
                "name": category,
                "positions": [
                    {"name": name, "fen": fen, "hint": hint}
                    for name, (fen, hint) in entries.items()
                ],
            }
            for category, entries in raw.items()
        ]
        return jsonify({"categories": categories})
    except Exception as e:
        app.logger.warning(f"Failed to load positions: {e}")
        return _internal_error(e)


@app.route("/api/board/setup-position", methods=["POST"])
@requires_auth
def api_board_setup_position():
    """Set up a predefined position on the board. Requires authentication.

    Validates the FEN, then asks the main process to abort any running game and
    set up the position. The web UI is responsible for confirming with the user
    when a game is in progress before calling this (the board records the
    interrupted game as abandoned, result = "*").

    Body: {"fen": str, "name"?: str, "hint"?: str}
    """
    try:
        import chess

        body = request.get_json(silent=True) or {}
        fen = (body.get("fen") or "").strip()
        if not fen:
            return jsonify({"success": False, "error": "No FEN provided"}), 400
        try:
            chess.Board(fen)
        except ValueError:
            return jsonify({"success": False, "error": "Invalid FEN"}), 400

        name = (body.get("name") or "Position").strip()
        hint = body.get("hint")
        params = {"fen": fen, "name": name}
        if hint:
            params["hint"] = hint

        from universalchess.services.game_broadcast import send_board_command

        sent = send_board_command("setup_position", params)
        if sent:
            return jsonify({"success": True, "message": f"Setting up {name}"})
        return jsonify({
            "success": False,
            "error": "Board not running",
        }), 503
    except Exception as e:
        return _internal_error(e)


@app.route("/api/board/abort-game", methods=["POST"])
@requires_auth
def api_board_abort_game():
    """Abort the running game on the board. Requires authentication.

    Asks the main process to record the in-progress game as abandoned
    (result = "*") and return to the menu. The web UI confirms with the user
    before calling this.
    """
    try:
        from universalchess.services.game_broadcast import send_board_command

        sent = send_board_command("abort_game")
        if sent:
            return jsonify({"success": True, "message": "Game aborted"})
        return jsonify({"success": False, "error": "Board not running"}), 503
    except Exception as e:
        return _internal_error(e)


# ============================================================================
# System actions (reset / power / Original Centaur)
# ============================================================================
# These mirror the board's System and Power menus. Each privileged action is
# routed to the main process over the board-command channel so it runs the exact
# same board-side code path as the on-board menu (e.g. shutdown shows the board's
# splash and cleans up hardware via cleanup_and_exit). The web process never
# performs the shutdown/reboot itself, which avoids the historical divergence
# where /shutdownboard powered off without the board's cleanup. Shutdown, reboot
# and Original Centaur make the web UI unavailable; the UI confirms first.


def _system_board_action(command: str, success_message: str):
    """Forward a system action to the board and shape the JSON response.

    Returns success when the board accepted the command, 503 when the board is
    not running (so the UI can say the board is offline rather than report a
    false success), and 500 on unexpected errors.
    """
    try:
        from universalchess.services.game_broadcast import send_board_command

        sent = send_board_command(command)
        if sent:
            # Record operator-initiated lifecycle actions (reboot/shutdown/reset/
            # run-centaur) in the persistent event log. Only when accepted, so a
            # board-offline 503 is not logged as if it happened.
            from universalchess.services.event_log import log_event
            log_event("system", success_message, level="info")
            return jsonify({"success": True, "message": success_message})
        return jsonify({"success": False, "error": "Board not running"}), 503
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/info", methods=["GET"])
def api_system_info():
    """Return read-only system capabilities for the web UI.

    ``centaur_available`` mirrors the board's own check (the on-board menu hides
    the Original Centaur entry when the install is incomplete), so the web UI can
    do the same without importing board/hardware modules. "Available" means a
    complete install (executable + engines/ + fonts/), not just the executable --
    a partial import is not launchable.
    """
    from universalchess.services.centaur_import import centaur_app_installed

    try:
        system_user = pwd.getpwuid(os.getuid()).pw_name
        return jsonify({
            "centaur_available": centaur_app_installed(),
            "username": system_user,
        })
    except Exception as e:
        return _internal_error(e)


def _read_debug_serial_enabled() -> bool:
    """Return whether [system] debug_serial is enabled (tolerant of spellings)."""
    from universalchess.board.settings import Settings
    value = Settings.read('system', 'debug_serial', 'False')
    return str(value).strip().lower() in ('1', 'true', 'on', 'yes')


@app.route("/api/system/debug-serial", methods=["GET"])
def api_get_debug_serial():
    """Report whether serial debug logging is enabled.

    Read-only and unauthenticated like the other GET probes; it exposes only a
    single boolean flag, no secrets. The Debug card uses it to show the current
    switch state on load.
    """
    try:
        return jsonify({"enabled": _read_debug_serial_enabled()})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/debug-serial", methods=["POST"])
@requires_auth
def api_set_debug_serial():
    """Enable/disable raw serial debug logging. Requires authentication.

    Persists [system] debug_serial via save_all_settings so SSE clients and the
    main process are notified like any other settings change. The board reads
    this flag only at startup (the discovery handshake it captures happens once
    at boot), so the user enables it, reboots to capture the handshake, then
    downloads the debug log. Body: {"enabled": bool}.
    """
    try:
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        save_all_settings({"system": {"debug_serial": enabled}})
        return jsonify({"success": True, "enabled": enabled})
    except Exception as e:
        return _internal_error(e)


def _read_display_flag(name: str, default: bool = False) -> bool:
    """Return whether a [display] boolean flag is enabled (tolerant of spellings).

    Shared reader for [display] on/off settings (the high_contrast override, the
    three_color switch, the update-batching option) so they parse stored values
    identically to the board process. ``default`` is the value when the key is
    absent, so an un-configured board reports the intended shipped state (e.g.
    batching on).
    """
    from universalchess.board.settings import Settings
    value = Settings.read('display', name, 'True' if default else 'False')
    return str(value).strip().lower() in ('1', 'true', 'on', 'yes')


# Map the board-reported active controller (display_selection.CONTROLLER_*) to
# the waveform-profile family the web layer filters on. Kept as a plain table so
# the web process needs no import from the RPi.GPIO-dependent driver modules.
_CONTROLLER_TO_WAVEFORM_FAMILY = {
    "UC8151D": "uc8151d",
    "SSD1680": "ssd16xx",
}


def _active_waveform_controller():
    """Return the active controller's waveform family, or None if unknown.

    Reads the cross-process display-status file the board writes at startup. The
    family selects which profiles the UI offers (a UC8151D table is meaningless
    on the SSD1680 driver and vice versa). None when the board has not reported,
    the display is disabled, or it reported a controller with no profile family.
    """
    from universalchess.board import hardware_info
    status = hardware_info.read_display_status()
    if not status or not status.get("initialized"):
        return None
    return _CONTROLLER_TO_WAVEFORM_FAMILY.get(status.get("active_controller"))


def _read_selected_profile_key(controller=None) -> str:
    """Return the configured waveform-profile key, resolved to a known profile.

    A blank or stale stored key -- or one belonging to the other controller after
    a panel swap -- resolves to the active controller's verified default, so the
    UI never shows an unknown selection and the board never runs with no waveform.
    """
    from universalchess.board.settings import Settings
    from universalchess.epaper.framework.waveshare import waveform_profiles as wp
    key = str(Settings.read('display', 'waveform_profile', '')).strip()
    return wp.get_profile(key, controller).key


def _display_tuning_available() -> bool:
    """Whether to offer the display-tuning card: whenever a panel is active.

    Both controllers have selectable waveform profiles now (the SSD1680 V1
    fallback and the primary UC8151D, including replacement-panel variants), so
    the card is surfaced whenever the board reported an initialized panel with a
    known controller family. It stays hidden when the display is disabled or the
    board has not reported yet.
    """
    return _active_waveform_controller() is not None


@app.route("/api/system/display-tuning", methods=["GET"])
def api_get_display_tuning():
    """Report the active panel's waveform profiles, selection and availability.

    Read-only and unauthenticated like the other GET probes; exposes only
    profile metadata (key/label/source/url/controller) for the *active*
    controller and the current selection -- no secrets, no raw waveform bytes.
    The list is filtered to the live controller so the UI never offers a table
    the active driver cannot drive. ``available`` is False until the board
    reports an initialized panel, hiding the card when there is nothing to tune.
    """
    try:
        from universalchess.epaper.framework.waveshare import waveform_profiles as wp
        controller = _active_waveform_controller()
        return jsonify({
            "available": controller is not None,
            "active_controller": controller,
            "profiles": wp.profiles_metadata(controller),
            "selected": _read_selected_profile_key(controller),
            "high_contrast": _read_display_flag("high_contrast"),
            # Three-color (red/white/black) mode. Both the UC8151D (V2) and SSD1680
            # (V1) drivers can drive tri-color BWR panels, so the toggle is offered
            # whenever a panel is active; tri-color is a property of the physical
            # panel, not the controller family.
            "three_color": _read_display_flag("three_color"),
            "three_color_supported": controller is not None,
            # Update batching (default on): coalesce a rapid burst of refreshes to
            # one refresh of the final frame so the panel does not lag behind when
            # updates arrive faster than it can draw. Applies to every panel, so
            # it is reported whenever the card is available.
            "batch_updates": _read_display_flag("batch_updates", default=True),
        })
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/display-tuning", methods=["POST"])
@requires_auth
def api_set_display_tuning():
    """Select a waveform profile for the active panel and apply it live. Requires auth.

    Persists [display] waveform_profile and high_contrast, then sends the board
    process a ``display_profile`` command so it re-inits the panel and forces a
    full refresh -- the new waveform/voltages take effect without a reboot. An
    unknown profile key, or one that does not target the active controller, is
    rejected (400) so a client bug fails loudly rather than writing an invalid
    selection. Body: {"profile": "<key>", "high_contrast": bool}.
    """
    try:
        from universalchess.epaper.framework.waveshare import waveform_profiles as wp

        controller = _active_waveform_controller()
        body = request.get_json(silent=True) or {}
        updates = {}
        if "profile" in body:
            key = str(body["profile"]).strip()
            # Validate against the active controller's family so a UC8151D key
            # cannot be persisted for an SSD1680 panel (or vice versa). When the
            # board has not reported a controller yet, fall back to any-known.
            if not wp.is_known_profile(key, controller):
                return jsonify({"success": False, "error": f"unknown profile: {key}"}), 400
            updates["waveform_profile"] = key
        if "high_contrast" in body:
            updates["high_contrast"] = bool(body["high_contrast"])
        if "three_color" in body:
            updates["three_color"] = bool(body["three_color"])
        if "batch_updates" in body:
            updates["batch_updates"] = bool(body["batch_updates"])
        if not updates:
            return jsonify({"success": False, "error": "no profile, high_contrast, three_color or batch_updates given"}), 400

        save_all_settings({"display": updates})

        # Apply live (no reboot): the board process re-inits the panel and forces
        # a full refresh. Best-effort -- if the board is not running, the change
        # still takes effect on the next boot from the persisted settings.
        from universalchess.services.game_broadcast import send_board_command
        applied_live = send_board_command("display_profile", {
            "profile": _read_selected_profile_key(controller),
            "high_contrast": _read_display_flag("high_contrast"),
        })

        return jsonify({
            "success": True,
            "applied_live": bool(applied_live),
            "selected": _read_selected_profile_key(controller),
            "high_contrast": _read_display_flag("high_contrast"),
            "three_color": _read_display_flag("three_color"),
            "batch_updates": _read_display_flag("batch_updates", default=True),
        })
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/debug-log", methods=["GET"])
@requires_auth
def api_download_debug_log():
    """Download the board debug log for support. Requires authentication.

    Serves ~/debug.log (rewritten each boot by board.logging). Auth-gated
    because a full debug log can contain diagnostic detail about the system.
    Returns 404 when no log exists yet (board has not run since install).
    """
    try:
        log_path = pathlib.Path.home() / "debug.log"
        if not log_path.is_file():
            return jsonify({"success": False, "error": "No debug log found"}), 404
        return send_file(
            str(log_path),
            mimetype="text/plain",
            as_attachment=True,
            download_name="debug.log",
        )
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/event-log", methods=["GET"])
@requires_auth
def api_system_event_log():
    """Return recent application events for the Settings event-log viewer.

    Auth-gated like the debug-log download: event messages can name engines,
    versions and failure details. Reads the persistent JSON-lines log
    (services.event_log), newest first, bounded by ``limit`` (1..1000, default
    200). Returns ``{"events": [...]}`` -- an empty list when nothing has been
    logged yet (fresh device), never a 404, so the viewer renders an empty state.
    """
    try:
        from universalchess.services.event_log import read_events

        # Clamp the caller-supplied limit to a sane window; a bad value falls
        # back to the default rather than erroring.
        try:
            limit = int(request.args.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(1000, limit))
        return jsonify({"events": read_events(limit=limit)})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/stats", methods=["GET"])
def api_system_stats():
    """Return live system telemetry (CPU temp/usage, memory, disk, uptime, load).

    Read-only and unauthenticated like the other GET probes. Values are sampled
    on each request via the shared ``universalchess.board.system_info`` reader,
    so the web "System" card and the e-paper About screen report identical
    numbers. The reader takes a short CPU sampling window (see system_info), so
    this endpoint blocks briefly; the UI polls it on an interval rather than per
    keystroke.
    """
    try:
        from universalchess.board.system_info import get_system_info

        return jsonify(get_system_info().to_dict())
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/hardware", methods=["GET"])
def api_system_hardware():
    """Return boot-stable hardware identity (wireless chip, versions, display).

    Read-only and unauthenticated like the other GET probes. Unlike
    ``/api/system/stats`` (per-second telemetry, polled), these facts are fixed
    for the life of the boot, so ``get_hardware_info`` caches them and the UI
    fetches this once. Includes the Broadcom wireless chip stepping and a
    Wi-Fi-hotspot health verdict (see ``board.hardware_info``).
    """
    try:
        from universalchess.board.hardware_info import get_hardware_info

        return jsonify(get_hardware_info().to_dict())
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/battery", methods=["GET"])
def api_system_battery():
    """Return the latest battery level/charger state for the navbar indicator.

    Read-only and unauthenticated like the other GET probes. Battery is read from
    the board controller, which exists only in the main process; that process
    broadcasts every change over the game socket and the web subscriber caches the
    latest snapshot. Live updates reach the browser over SSE -- this endpoint just
    seeds the indicator on load. When nothing is cached yet (fresh web start,
    before the board re-broadcasts), ask the board to re-broadcast so the next SSE
    push fills it, and return the unknown contract (nulls) for now.

    Contract: {battery_level: 0-20|null, battery_percent: 0-100|null,
    charger_connected: bool}.
    """
    try:
        from universalchess.services.game_broadcast import (
            get_subscriber,
            request_battery_status_broadcast,
        )

        cached = get_subscriber().get_last_battery_status()
        if cached is None:
            request_battery_status_broadcast()
            return jsonify(
                {
                    "battery_level": None,
                    "battery_percent": None,
                    "charger_connected": False,
                }
            )
        return jsonify(
            {
                "battery_level": cached.get("battery_level"),
                "battery_percent": cached.get("battery_percent"),
                "charger_connected": bool(cached.get("charger_connected", False)),
            }
        )
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/reset", methods=["POST"])
@requires_auth
def api_system_reset():
    """Reset all game/player settings to defaults. Requires authentication.

    Runs the same reset as the board's Reset Settings menu (clear sections +
    reload defaults). The web UI confirms before calling this.
    """
    return _system_board_action("reset_settings", "Settings reset to defaults")


@app.route("/api/system/shutdown", methods=["POST"])
@requires_auth
def api_system_shutdown():
    """Power off the board. Requires authentication.

    Routes to the board's shutdown path (splash + hardware cleanup); the web UI
    becomes unavailable. The UI confirms before calling this.
    """
    return _system_board_action("shutdown", "Shutting down")


@app.route("/api/system/reboot", methods=["POST"])
@requires_auth
def api_system_reboot():
    """Reboot the board. Requires authentication.

    Routes to the board's reboot path (LED sweep + shutdown). The web UI becomes
    unavailable until the board comes back. The UI confirms before calling this.
    """
    return _system_board_action("reboot", "Rebooting")


@app.route("/api/system/run-centaur", methods=["POST"])
@requires_auth
def api_system_run_centaur():
    """Hand control to the original DGT Centaur software. Requires authentication.

    Runs the same handoff as the main menu's Original Centaur action, which stops
    Universal Chess (and this web server). The board chooses translate vs direct
    mode from [centaur] direct_mode (set via the endpoints below). The UI warns
    before calling this.
    """
    return _system_board_action("run_centaur", "Launching original Centaur software")


@app.route("/api/system/centaur-mode", methods=["GET"])
def api_get_centaur_mode():
    """Report whether Original Centaur launches in direct mode.

    Read-only and unauthenticated like the other GET probes; it exposes only a
    single boolean. The Original Centaur card uses it to show the Direct Mode
    toggle's current state on load. direct_mode=false means translate mode (the
    default), where centaur's display is routed through UC's gateway.
    """
    try:
        from universalchess.board.settings import Settings
        from universalchess.services.power import centaur_direct_mode_enabled
        return jsonify({"direct_mode": centaur_direct_mode_enabled(Settings.read)})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/centaur-mode", methods=["POST"])
@requires_auth
def api_set_centaur_mode():
    """Set Original Centaur's launch mode. Requires authentication.

    Persists [centaur] direct_mode via save_all_settings so the board process is
    notified like any other settings change and reads the new value at the next
    launch. Body: {"direct_mode": bool}. False (translate mode) is the default:
    centaur runs under the display shim and UC re-renders its frames onto the
    fitted panel; True (direct mode) lets centaur drive the panel natively.
    """
    try:
        body = request.get_json(silent=True) or {}
        direct_mode = bool(body.get("direct_mode"))
        save_all_settings({"centaur": {"direct_mode": direct_mode}})
        return jsonify({"success": True, "direct_mode": direct_mode})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/centaur-engine", methods=["GET"])
def api_get_centaur_engine():
    """Report the engine and UCI options the Centaur proxy will use.

    Read-only and unauthenticated like the other GET probes. The Original Centaur
    card uses this to populate the engine selector and option fields. direct_mode
    aside, this only affects translate-mode play, where Centaur's engine path is
    the UC proxy. Options are returned as an object (parsed from the stored JSON).
    """
    try:
        from universalchess.board.settings import Settings
        from universalchess.services.centaur_engine_proxy.config import load_proxy_config

        config = load_proxy_config(Settings.read)
        return jsonify({"engine": config.engine_name, "options": config.options})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/centaur-engine", methods=["POST"])
@requires_auth
def api_set_centaur_engine():
    """Set the engine and UCI options the Centaur proxy uses. Requires auth.

    Body: {"engine": str, "options": {name: value}}. Persists [centaur_engine]
    via save_all_settings (options as a JSON string), so the proxy reads the new
    values at the next launch. The proxy still clamps Hash/MultiPV to the memory
    floor, so options here cannot push the board into an OOM.
    """
    try:
        from universalchess.services.centaur_engine_proxy.config import (
            DEFAULT_ENGINE,
            ENGINE_KEY,
            OPTIONS_KEY,
            CONFIG_SECTION,
        )

        body = request.get_json(silent=True) or {}
        engine = str(body.get("engine") or DEFAULT_ENGINE).strip() or DEFAULT_ENGINE
        options = body.get("options") or {}
        if not isinstance(options, dict):
            return jsonify({"success": False, "error": "options must be an object"}), 400
        save_all_settings({
            CONFIG_SECTION: {ENGINE_KEY: engine, OPTIONS_KEY: json.dumps(options)},
        })
        return jsonify({"success": True, "engine": engine, "options": options})
    except Exception as e:
        return _internal_error(e)


def _centaur_is_running() -> bool:
    """Whether the original Centaur software is currently running.

    Detection matches the centaur main process by exact name. This is robust
    across both launch modes -- translate runs ``./centaur`` as the pi user and
    direct runs ``sudo ./centaur`` (root), but the process name is ``centaur`` in
    both, while its engine subprocess has a different name and is not matched.
    Process names under ``/proc`` are world-readable, so the (non-root) web
    process can see a root-owned direct-mode centaur too.
    """
    import subprocess  # nosec B404 - fixed, trusted 'pgrep' invocation, no user input
    result = subprocess.run(  # nosec B603 B607
        ["pgrep", "-x", "centaur"], capture_output=True, timeout=5  # noqa: S607
    )
    return result.returncode == 0


@app.route("/api/system/centaur-status", methods=["GET"])
def api_get_centaur_status():
    """Report whether the original Centaur software is currently running.

    Read-only and unauthenticated like the other GET probes. The Original Centaur
    card polls this so its single action button stays state-aware: it offers
    "Switch to Original Centaur" when stopped and "Return to Universal Chess"
    when running.
    """
    try:
        return jsonify({"running": _centaur_is_running()})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/return-to-universal", methods=["POST"])
@requires_auth
def api_system_return_to_universal():
    """Stop the original Centaur software and bring Universal Chess back up.

    Requires authentication. Counterpart to run-centaur, used when centaur is
    running. While centaur runs, the Universal Chess main process is blocked
    inside its ``subprocess.run(centaur)`` handoff and cannot service board
    actions, so -- unlike run-centaur, which routes through the board -- this runs
    entirely in the independent web process:

    1. Signal the centaur main process to exit. It is a child in the
       universal-chess.service control group in both translate and direct modes.
    2. Restart universal-chess.service. The restart's control-group stop reaps any
       stragglers (e.g. centaur's engine subprocess), and the fresh start has
       Universal Chess reclaim the serial board and the e-paper panel.

    The brief pause lets centaur exit on the signal so the restart is prompt
    instead of waiting on systemd's stop timeout. ``pkill``/``systemctl`` are
    fixed commands with no user input, behind ``@requires_auth``.
    """
    try:
        import subprocess  # nosec B404 - fixed, trusted commands below, no user input
        subprocess.run(["pkill", "-x", "centaur"], check=False, timeout=5)  # noqa: S607  # nosec B603 B607
        time.sleep(1)
        subprocess.run(  # nosec B603 B607
            ["sudo", "systemctl", "restart", "universal-chess.service"],  # noqa: S607
            check=False, timeout=30,
        )
        return jsonify({"success": True})
    except Exception as e:
        return _internal_error(e)


def _resolve_centaur_import_script():
    """Locate the Centaur SD image-generator script to offer for download.

    Prefers the packaged copy under /opt/universalchess/tools (installed by the
    build) and falls back to the repo tools/ dir for development. Returns the
    path string, or None if neither exists.
    """
    from universalchess.paths import BASE_DIR

    candidates = [
        os.path.join(BASE_DIR, "tools", "centaur-import", "make-centaur-image.sh"),
        str(pathlib.Path(__file__).resolve().parents[3] / "tools" / "centaur-import" / "make-centaur-image.sh"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


@app.route("/api/system/centaur-import-script", methods=["GET"])
def api_system_centaur_import_script():
    """Serve the make-centaur-image.sh helper as a download.

    The user runs this on the computer holding the original SD card to produce
    the uploadable image. Read-only and unauthenticated like the other GET
    helpers: it is a fixed, secret-free shell script. Served as an attachment so
    the browser saves it rather than rendering it.
    """
    script_path = _resolve_centaur_import_script()
    if script_path is None:
        return jsonify({"success": False, "error": "Import script not found."}), 404
    return send_file(
        script_path,
        mimetype="text/x-shellscript",
        as_attachment=True,
        download_name="make-centaur-image.sh",
    )


@app.route("/api/system/import-centaur", methods=["POST"])
@requires_auth
def api_system_import_centaur():
    """Install the original Centaur software from an uploaded SD image.

    Accepts a multipart upload (field ``image``) of the gzip ext4 image produced
    by tools/centaur-import/make-centaur-image.sh. The file is streamed to the
    service tmp dir (the dir the mount helper allow-lists), then the import
    service loop-mounts it read-only, extracts the app to the managed
    CENTAUR_HOME, strips debug cruft, and validates the file set. On success the
    Original Centaur Switch/Return controls become available.

    Returns 400 with an actionable message when the upload is missing/misnamed or
    the image lacks a complete Centaur app, and 500 on unexpected errors. The
    CentaurImportError text is author-written and path-free, so it is safe to
    surface to the client. The uploaded image is always removed afterwards so a
    ~200 MB artifact does not accumulate in tmp.
    """
    from universalchess.paths import TMP_DIR
    from universalchess.services.centaur_import import (
        CentaurImportError,
        install_from_image,
    )

    upload = request.files.get("image")
    if upload is None or not upload.filename:
        return jsonify({"success": False, "error": "No image file was uploaded."}), 400

    safe = secure_filename(upload.filename)
    # Only the gzip image artifact is accepted; a wrong file is user error (400),
    # not a server fault.
    if not safe.endswith(".gz"):
        return (
            jsonify({
                "success": False,
                "error": "Upload must be the .img.gz image produced by make-centaur-image.sh.",
            }),
            400,
        )

    os.makedirs(TMP_DIR, exist_ok=True)
    # Contain the save path inside TMP_DIR; this is also the dir the mount helper
    # restricts images to, so a path that escapes it would be refused downstream.
    target = safe_under_base(TMP_DIR, safe)
    if target is None:
        return jsonify({"success": False, "error": "Invalid image filename."}), 400

    try:
        # FileStorage.save streams the body to disk in chunks rather than holding
        # the ~200 MB image in memory.
        upload.save(str(target))
        result = install_from_image(target)
        return jsonify({
            "success": True,
            "installed_path": result.installed_path,
            "file_count": result.file_count,
        })
    except CentaurImportError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return _internal_error(e)
    finally:
        try:
            if os.path.exists(str(target)):
                os.remove(str(target))
        except OSError:  # noqa: S110  # nosec B110 - best-effort tmp cleanup; failure is non-fatal
            pass


# ============================================================================
# Connectivity - WiFi
# ============================================================================
# The WiFi system calls (iwlist/nmcli/rfkill) run in this web process via the
# shared, UI-agnostic universalchess.connectivity.wifi core (same code the board
# menu uses). Status is read-only and unauthenticated like other GET endpoints;
# scan/connect/forget/enable are privileged (sudo) and change network state, so
# they require authentication. Changing or forgetting the active network can drop
# the very connection the browser is using; the UI warns before doing so.


@app.route("/api/connectivity/wifi/status", methods=["GET"])
def api_wifi_status():
    """Return current WiFi adapter status (enabled, connected SSID, IP, signal)."""
    try:
        from universalchess.epaper.wifi_info import get_wifi_status

        return jsonify(get_wifi_status())
    except Exception as e:
        app.logger.warning(f"Failed to get WiFi status: {e}")
        return _internal_error(e)


@app.route("/api/connectivity/wifi/scan", methods=["POST"])
@requires_auth
def api_wifi_scan():
    """Scan for nearby WiFi networks. Requires authentication (privileged scan)."""
    try:
        from universalchess.connectivity import wifi as wifi_core

        return jsonify({"networks": wifi_core.scan_networks(app.logger)})
    except Exception as e:
        app.logger.warning(f"WiFi scan failed: {e}")
        return _internal_error(e)


@app.route("/api/connectivity/wifi/saved", methods=["GET"])
@requires_auth
def api_wifi_saved():
    """List saved WiFi networks, flagging the active one. Requires authentication."""
    try:
        from universalchess.connectivity import wifi as wifi_core

        return jsonify({"networks": wifi_core.list_saved_networks(app.logger)})
    except Exception as e:
        app.logger.warning(f"Failed to list saved WiFi networks: {e}")
        return _internal_error(e)


@app.route("/api/connectivity/wifi/connect", methods=["POST"])
@requires_auth
def api_wifi_connect():
    """Connect to a WiFi network. Requires authentication.

    Body: {"ssid": str, "password"?: str}. Returns the core's success flag and a
    short human-readable message (e.g. "Wrong password") on failure.
    """
    try:
        from universalchess.connectivity import wifi as wifi_core

        body = request.get_json(silent=True) or {}
        ssid = (body.get("ssid") or "").strip()
        if not ssid:
            return jsonify({"success": False, "error": "No SSID provided"}), 400
        password = body.get("password") or None
        success, message = wifi_core.connect_network(ssid, password, app.logger)
        status_code = 200 if success else 400
        return jsonify({"success": success, "message": message}), status_code
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/wifi/forget", methods=["POST"])
@requires_auth
def api_wifi_forget():
    """Forget a saved WiFi network. Requires authentication.

    Body: {"ssid": str}. Returns success=False with 404 if no matching saved
    profile existed, so the UI does not claim it forgot a network it never had.
    """
    try:
        from universalchess.connectivity import wifi as wifi_core

        body = request.get_json(silent=True) or {}
        ssid = (body.get("ssid") or "").strip()
        if not ssid:
            return jsonify({"success": False, "error": "No SSID provided"}), 400
        removed = wifi_core.forget_network(ssid, app.logger)
        if removed:
            return jsonify({"success": True, "message": f"Forgot {ssid}"})
        return jsonify({"success": False, "error": "No saved network found"}), 404
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/wifi/enable", methods=["POST"])
@requires_auth
def api_wifi_enable():
    """Enable or disable the WiFi radio via rfkill. Requires authentication.

    Body: {"enabled": bool}.
    """
    try:
        from universalchess.epaper.wifi_info import enable_wifi, disable_wifi

        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        ok = enable_wifi() if enabled else disable_wifi()
        return jsonify({"success": ok, "enabled": enabled})
    except Exception as e:
        return _internal_error(e)


# ============================================================================
# Connectivity - Bluetooth
# ============================================================================
# Read/manage operations run in this web process over the system D-Bus via the
# shared connectivity.bluetooth helpers (BlueZ allows multiple client
# connections and these never touch the board's pairing agent). Pairing a new
# keyboard and confirming an incoming pairing require the board's KeyboardDisplay
# agent in the main process, so those are routed over IPC (board_command +
# SSE events), not handled here. All actions require authentication.


@app.route("/api/connectivity/bluetooth/status", methods=["GET"])
def api_bt_status():
    """Return Bluetooth radio state and the list of paired devices."""
    try:
        from universalchess.connectivity import bluetooth as bt

        return jsonify(bt.get_status(log=app.logger))
    except Exception as e:
        app.logger.warning(f"Failed to get Bluetooth status: {e}")
        return _internal_error(e)


@app.route("/api/connectivity/bluetooth/enable", methods=["POST"])
@requires_auth
def api_bt_enable():
    """Enable or disable the Bluetooth radio. Body: {"enabled": bool}."""
    try:
        from universalchess.connectivity import bluetooth as bt

        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        ok = bt.set_enabled(enabled, log=app.logger)
        return jsonify({"success": ok, "enabled": enabled})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/bluetooth/scan", methods=["POST"])
@requires_auth
def api_bt_scan():
    """Scan for nearby Bluetooth keyboards (bounded window). Requires auth."""
    try:
        from universalchess.connectivity import bluetooth as bt

        return jsonify({"devices": bt.scan_keyboards(log=app.logger)})
    except Exception as e:
        app.logger.warning(f"Bluetooth scan failed: {e}")
        return _internal_error(e)


def _bt_device_action(action_name):
    """Run a connect/disconnect/forget action keyed by a request body address.

    Shared by the three management endpoints: validates the address (the manager
    raises ValueError on a malformed MAC -> 400) and maps the boolean result to a
    JSON response. Keeps the three routes free of duplicated parsing/error code.
    """
    from universalchess.connectivity import bluetooth as bt

    body = request.get_json(silent=True) or {}
    address = (body.get("address") or "").strip()
    if not address:
        return jsonify({"success": False, "error": "No address provided"}), 400
    try:
        ok = action_name(bt, address, app.logger)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid address"}), 400
    return jsonify({"success": ok})


@app.route("/api/connectivity/bluetooth/connect", methods=["POST"])
@requires_auth
def api_bt_connect():
    """Connect an already-paired Bluetooth device. Body: {"address": str}."""
    try:
        from universalchess.connectivity import bluetooth as bt

        body = request.get_json(silent=True) or {}
        address = (body.get("address") or "").strip()
        if not address:
            return jsonify({"success": False, "error": "No address provided"}), 400
        try:
            status = bt.connect_device_status(address, log=app.logger)
        except ValueError:
            return jsonify({"success": False, "error": "Invalid address"}), 400
        if status == "ok":
            return jsonify({"success": True})
        if status == "auth_failed":
            return jsonify({
                "success": False,
                "stalePairing": True,
                "error": "Saved pairing was rejected. Remove it and pair again?",
            })
        return jsonify({
            "success": False,
            "error": "Could not connect. Make sure the device is on and nearby.",
        })
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/bluetooth/disconnect", methods=["POST"])
@requires_auth
def api_bt_disconnect():
    """Disconnect a connected Bluetooth device. Body: {"address": str}."""
    try:
        return _bt_device_action(lambda bt, addr, lg: bt.disconnect_device(addr, log=lg))
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/bluetooth/forget", methods=["POST"])
@requires_auth
def api_bt_forget():
    """Forget (unpair) a Bluetooth device. Body: {"address": str}."""
    try:
        return _bt_device_action(lambda bt, addr, lg: bt.forget_device(addr, log=lg))
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/bluetooth/pair", methods=["POST"])
@requires_auth
def api_bt_pair():
    """Pair a new Bluetooth keyboard via the board. Body: {"address": str}.

    Pairing needs the board's KeyboardDisplay agent (main process), so this is a
    fire-and-forget command to the board. Progress is reported back over SSE as
    'bt_passkey' (code to show) and 'bt_pair_result' (started/success) events,
    which the Connectivity page listens for.
    """
    try:
        from universalchess.services.game_broadcast import send_board_command

        body = request.get_json(silent=True) or {}
        address = (body.get("address") or "").strip()
        if not address:
            return jsonify({"success": False, "error": "No address provided"}), 400
        sent = send_board_command("bt_pair", {"address": address})
        if sent:
            return jsonify({"success": True, "message": "Pairing started"})
        return jsonify({"success": False, "error": "Board not running"}), 503
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/bluetooth/pair-confirm", methods=["POST"])
@requires_auth
def api_bt_pair_confirm():
    """Accept or reject an incoming Bluetooth pairing shown on the board.

    Body: {"accept": bool}. Resolves the board's active pairing prompt (the same
    one shown on the e-paper) from the web, mirrored with the board so whichever
    surface acts first decides.
    """
    try:
        from universalchess.services.game_broadcast import send_board_command

        body = request.get_json(silent=True) or {}
        accept = bool(body.get("accept"))
        sent = send_board_command("bt_pair_confirm", {"accept": accept})
        if sent:
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Board not running"}), 503
    except Exception as e:
        return _internal_error(e)


# ============================================================================
# Connectivity - Chromecast
# ============================================================================
# Discovery is a stateless mDNS scan run in this web process. The active stream
# is owned by the ChromecastService singleton in the board (main) process (it
# also writes the e-paper snapshots the stream serves), so start/stop are board
# commands and the current status is mirrored to the web over SSE
# ('chromecast_state' events). All actions require authentication.


@app.route("/api/connectivity/chromecast/discover", methods=["POST"])
@requires_auth
def api_cast_discover():
    """Discover Chromecast devices on the network (bounded scan). Requires auth."""
    try:
        from universalchess.connectivity import chromecast as cast

        return jsonify({"devices": cast.discover(log=app.logger)})
    except Exception as e:
        app.logger.warning(f"Chromecast discovery failed: {e}")
        return _internal_error(e)


@app.route("/api/connectivity/chromecast/start", methods=["POST"])
@requires_auth
def api_cast_start():
    """Start streaming the board to a Chromecast device. Body: {"device": str}."""
    try:
        from universalchess.services.game_broadcast import send_board_command

        body = request.get_json(silent=True) or {}
        device = (body.get("device") or "").strip()
        if not device:
            return jsonify({"success": False, "error": "No device provided"}), 400
        source = "live_board" if get_chromecast_use_live_board() else "classic"
        sent = send_board_command(
            "chromecast_start",
            {"device": device, "source": source},
        )
        if sent:
            return jsonify({"success": True, "message": f"Streaming to {device}"})
        return jsonify({"success": False, "error": "Board not running"}), 503
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/chromecast/source", methods=["GET"])
def api_cast_source_get():
    """Return the selected Chromecast display source."""
    return jsonify({"useLiveBoard": get_chromecast_use_live_board()})


@app.route("/api/connectivity/chromecast/source", methods=["POST"])
@requires_auth
def api_cast_source_set():
    """Persist the selected Chromecast display source. Body: {"useLiveBoard": bool}."""
    try:
        body = request.get_json(silent=True) or {}
        raw_value = body.get("useLiveBoard", True)
        use_live_board = (
            raw_value
            if isinstance(raw_value, bool)
            else _parse_config_bool(raw_value, default=True)
        )
        set_chromecast_use_live_board(use_live_board)
        broadcast_sse_event("settings_changed")
        return jsonify({"success": True, "useLiveBoard": use_live_board})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/chromecast/stop", methods=["POST"])
@requires_auth
def api_cast_stop():
    """Stop streaming. Body: optional {"device": str} to stop one device.

    With no device (or empty), stops every active stream ("Stop all").
    """
    try:
        from universalchess.services.game_broadcast import send_board_command

        body = request.get_json(silent=True) or {}
        device = (body.get("device") or "").strip()
        payload = {"device": device} if device else {}
        sent = send_board_command("chromecast_stop", payload)
        if sent:
            msg = f"Stopped {device}" if device else "Streaming stopped"
            return jsonify({"success": True, "message": msg})
        return jsonify({"success": False, "error": "Board not running"}), 503
    except Exception as e:
        return _internal_error(e)


@app.route("/api/connectivity/chromecast/status", methods=["POST"])
@requires_auth
def api_cast_status():
    """Ask the board to (re)broadcast its current Chromecast status over SSE.

    The status lives in the board process; this triggers a 'chromecast_state'
    event the Connectivity page consumes, so the page can fill on load without a
    request/response IPC channel.
    """
    try:
        from universalchess.services.game_broadcast import send_board_command

        sent = send_board_command("chromecast_status")
        return jsonify({"success": bool(sent)})
    except Exception as e:
        return _internal_error(e)


# Upscale factor for the sprite-sheet preview. The native sheet is 16px-per-cell
# pixel art; nearest-neighbour scaling keeps it crisp at a web-visible size.
SPRITE_PREVIEW_SCALE = 5


@app.route("/api/sprites/<sheet>/image", methods=["GET"])
def api_get_sprite_image(sheet):
    """Serve the full sprite sheet as a scaled PNG for the Sprites preview.

    Renders every piece in the sheet (both the light-square and dark-square
    rows) so the web Sprites selector can show exactly what the board draws.
    The sheet name is validated against the discovered set, so it cannot be
    used for path traversal.
    """
    try:
        from universalchess.resources import ResourceLoader
        from universalchess.paths import RESOURCES_DIR, USER_RESOURCES_DIR

        loader = ResourceLoader(RESOURCES_DIR, USER_RESOURCES_DIR)
        if sheet not in loader.list_chess_sprite_sheets():
            abort(404)

        filename = f"{ResourceLoader._SPRITE_SHEET_PREFIX}{sheet}{ResourceLoader._SPRITE_SHEET_SUFFIX}"
        img = loader.get_image(filename)
        if img is None:
            abort(404)

        if img.mode not in ("L", "RGB"):
            img = img.convert("L")
        scaled = img.resize(
            (img.width * SPRITE_PREVIEW_SCALE, img.height * SPRITE_PREVIEW_SCALE),
            Image.NEAREST,
        )

        buf = io.BytesIO()
        scaled.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")
    except Exception as e:
        app.logger.warning(f"Failed to render sprite sheet '{sheet}': {e}")
        abort(404)


@app.route("/api/engines", methods=["GET"])
def api_get_engines():
    """Get list of installed engines for dropdowns."""
    try:
        from universalchess.managers.engine_manager import EngineManager, ENGINES
        
        engine_manager = EngineManager()
        engines_list = []
        
        for name, engine_def in ENGINES.items():
            is_installed = engine_def.is_system_package or engine_manager.is_installed(name)
            engines_list.append({
                "name": name,
                "display_name": engine_def.display_name,
                "installed": is_installed
            })
        
        return jsonify(engines_list)
    except Exception as e:
        # Fallback if engine manager not available
        return jsonify([{"name": "stockfish", "display_name": "Stockfish", "installed": True}])


# Engine personality profiles are editable only for engines with a parameter
# schema in engine_profiles.PROFILE_SCHEMAS (currently Rodent IV). The editor
# reads/writes the same .uci file the engine player loads at game start, so
# edits take effect on the next game; the [DEFAULT] section is preserved.
from universalchess.services import engine_profiles

_DEFAULT_ENGINES_UCI_DIR = str(
    pathlib.Path(__file__).parent.parent / "defaults" / "engines"
)


def _config_uci_path(engine_name):
    """Resolve the writable config ``.uci`` path for an engine.

    Guards the engine name against path traversal via ``safe_under_base``;
    returns ``None`` if the name is empty or escapes the engines config dir.
    """
    return safe_under_base(os.path.join(CONFIG_DIR, "engines"), f"{engine_name}.uci")


def _defaults_uci_path(engine_name):
    """Resolve the packaged defaults ``.uci`` path (seed/fallback source)."""
    return safe_under_base(_DEFAULT_ENGINES_UCI_DIR, f"{engine_name}.uci")


@app.route("/api/engines/<engine_name>/profiles", methods=["GET"])
def api_get_engine_profiles(engine_name):
    """Return the editable profile schema and current profiles for an engine.

    Engines without a schema return ``editable: false`` so the UI hides the
    editor. Profiles come from the writable config ``.uci``, falling back to the
    packaged defaults on a fresh install.
    """
    groups = engine_profiles.get_schema(engine_name)
    if groups is None:
        return jsonify({
            "engine": engine_name, "editable": False, "schema": [], "profiles": [],
        })
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return jsonify({"success": False, "error": "Invalid engine"}), 400
    profiles = engine_profiles.read_profiles(config_path, _defaults_uci_path(engine_name))
    return jsonify({
        "engine": engine_name,
        "editable": True,
        "schema": engine_profiles.schema_to_json(groups),
        "profiles": profiles,
    })


# Profile mutations use POST, not PUT/DELETE: the app's WebDAV before_request
# (handle_preflight) intercepts every PUT/DELETE app-wide and demands WebDAV
# auth, so REST routes cannot use those verbs. All engine endpoints (install,
# uninstall, resume, cancel) already use POST for the same reason.
@app.route("/api/engines/<engine_name>/profiles/<profile_name>", methods=["POST"])
def api_save_engine_profile(engine_name, profile_name):
    """Create or replace a profile. Body: ``{"values": {key: value}}``.

    Values are validated against the engine schema (unknown keys and
    out-of-range values are rejected, not clamped, because Rodent applies them
    verbatim) before the section is written atomically. The whole section is
    replaced, so the client must submit the complete set of keys to retain.
    """
    groups = engine_profiles.get_schema(engine_name)
    if groups is None:
        return jsonify({"success": False, "error": "Engine has no editable profiles"}), 404
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return jsonify({"success": False, "error": "Invalid engine"}), 400
    body = request.get_json(silent=True) or {}
    values = body.get("values", {})
    # Validate up front and surface the message as a returned value. Catching
    # ProfileValidationError and returning str(e) would route a caught
    # exception's text into the response (CodeQL py/stack-trace-exposure); the
    # value-based checks below carry the same user-facing messages without that.
    if not engine_profiles.is_valid_profile_name(profile_name):
        return jsonify({"success": False, "error": "Invalid profile name"}), 400
    value_error = engine_profiles.validation_error(groups, values)
    if value_error is not None:
        return jsonify({"success": False, "error": value_error}), 400
    engine_profiles.write_profile(
        config_path, profile_name, values, groups, _defaults_uci_path(engine_name)
    )
    return jsonify({"success": True})


@app.route("/api/engines/<engine_name>/profiles/<profile_name>/delete", methods=["POST"])
def api_delete_engine_profile(engine_name, profile_name):
    """Delete a profile section.

    404 if the engine has no editable profiles or the profile does not exist;
    400 if the name is reserved (``DEFAULT``).
    """
    groups = engine_profiles.get_schema(engine_name)
    if groups is None:
        return jsonify({"success": False, "error": "Engine has no editable profiles"}), 404
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return jsonify({"success": False, "error": "Invalid engine"}), 400
    # Reject the reserved section by value rather than catching the exception
    # delete_profile would raise and echoing it back (CodeQL py/stack-trace-exposure).
    blocked = engine_profiles.delete_blocked_reason(profile_name)
    if blocked is not None:
        return jsonify({"success": False, "error": blocked}), 400
    removed = engine_profiles.delete_profile(
        config_path, profile_name, _defaults_uci_path(engine_name)
    )
    if not removed:
        return jsonify({"success": False, "error": "Profile not found"}), 404
    return jsonify({"success": True})


@app.route("/api/engines/<engine_name>/levels", methods=["GET"])
def api_get_engine_levels(engine_name):
    """Get ELO levels and personalities for an engine from its .uci file."""
    try:
        import configparser
        import pathlib
        
        # Look for .uci file in config or defaults directories
        uci_paths = [
            pathlib.Path("/opt/universalchess/config/engines") / f"{engine_name}.uci",
            pathlib.Path(__file__).parent.parent / "defaults" / "engines" / f"{engine_name}.uci",
        ]
        
        uci_path = None
        for path in uci_paths:
            if path.exists():
                uci_path = path
                break
        
        if not uci_path:
            return jsonify(["Default"])
        
        config = configparser.ConfigParser()
        config.read(str(uci_path))
        
        levels = []
        for section in config.sections():
            if section != "DEFAULT":
                levels.append(section)
        
        # Ensure "Default" is always first option if not already present
        if "Default" not in levels:
            levels.insert(0, "Default")
        
        return jsonify(levels)
    except Exception as e:
        return jsonify(["Default"])


@app.route("/api/engines/all", methods=["GET"])
def api_get_all_engines():
    """Get full details of all engines for management UI."""
    try:
        from universalchess.managers.engine_manager import (
            EngineManager, ENGINES, arch_unsupported_reason, get_current_arch,
            canonical_ref,
        )

        engine_manager = EngineManager()
        # Resolve once: the device architecture is constant for this process, and
        # it determines which engines can be installed here.
        arch = get_current_arch()
        engines_list = []

        for name, engine_def in ENGINES.items():
            is_installed = engine_def.is_system_package or engine_manager.is_installed(name)
            unsupported_reason = arch_unsupported_reason(engine_def, arch)
            # Source-built engines support the ref picker; system packages and
            # bundled engines (no repo_url) do not. Reported here so the list view
            # can decide whether to fetch/show the picker without a per-engine
            # network round-trip (tags come from the dedicated /refs endpoint).
            source_installable = not engine_def.is_system_package and engine_def.repo_url is not None
            engines_list.append({
                "name": name,
                "display_name": engine_def.display_name,
                "summary": engine_def.summary,
                "description": engine_def.description,
                "installed": is_installed,
                "is_system_package": engine_def.is_system_package,
                "can_uninstall": engine_def.can_uninstall,
                "estimated_install_minutes": engine_def.estimated_install_minutes,
                "has_prebuilt": engine_def.has_prebuilt,
                "has_profiles": name in engine_profiles.PROFILE_SCHEMAS,
                # Architecture support for THIS device. `supported` drives the UI's
                # install button state; `unsupported_reason` explains why when False.
                "supported": unsupported_reason is None,
                "unsupported_reason": unsupported_reason,
                # Ref tracking (local-only, no network): whether the engine supports
                # the picker, the canonical/recommended ref, and the ref currently
                # installed (None if unknown/not installed). The selectable tag list
                # is served by GET /api/engines/<name>/refs.
                "source_installable": source_installable,
                "recommended_ref": canonical_ref(engine_def) if source_installable else None,
                "installed_ref": engine_manager.get_installed_ref(name) if source_installable else None,
                "is_custom": False,
            })

        # Operator-added engines are appended after the catalog. They are
        # "installed" when their binary exists and is executable; their source
        # (upload vs url) drives the description. They expose no ref picker or
        # profiles and are always uninstallable.
        for custom in _custom_engine_store.list():
            # Resolve under the engines dir via the containment guard so the
            # path built from the (registry-stored) id cannot escape it.
            binary = safe_under_base(_ENGINES_DIR, custom.id)
            installed = binary is not None and os.path.exists(binary) and os.access(binary, os.X_OK)
            description = (
                "Uploaded engine binary."
                if custom.source == "upload"
                else f"Installed from {custom.url}"
            )
            engines_list.append({
                "name": custom.id,
                "display_name": custom.display_name,
                "summary": "Custom engine",
                "description": description,
                "installed": installed,
                "is_system_package": False,
                "can_uninstall": True,
                "estimated_install_minutes": 0,
                "has_prebuilt": False,
                "has_profiles": False,
                "supported": True,
                "unsupported_reason": None,
                "source_installable": False,
                "recommended_ref": None,
                "installed_ref": None,
                "is_custom": True,
            })

        return jsonify(engines_list)
    except Exception as e:
        return _internal_error(e)


# Engine installation progress is owned by engine_install_state.STORE: a single,
# disk-persisted, structured state (stage + derived percent) so the UI reflects
# real progress on a fresh page load AND survives a process/board restart. On
# startup, an install that was running when the process stopped is reconciled to
# `interrupted` so the UI can offer a manual resume instead of polling a dead
# install.
from universalchess.services import engine_install_state
_engine_install_store = engine_install_state.STORE
_engine_install_store.reconcile_interrupted()


# Custom (operator-added) engines: a binary uploaded from the browser or fetched
# from an HTTPS URL. These are not in the hardcoded ENGINES catalog; the registry
# records them and the binary lives at ENGINES_DIR/<id> exactly like a catalog
# single-binary engine, so installed-checks and runtime path resolution treat
# them identically. The engine_manager helpers (arch detection / safe tar
# extraction) are reused. The names below are module globals so tests can point
# them at temp locations (mirroring _engine_install_store / CONFIG_DIR).
from universalchess.managers.engine_manager import get_current_arch, _safe_extract_tar
from universalchess.services import custom_engines as _custom_engines
from universalchess.services.custom_engine_registry import (
    CustomEngine,
    CUSTOM_ENGINE_STORE as _custom_engine_store,
)

_ENGINES_DIR = ENGINES_DIR
# Cap for an uploaded or downloaded engine payload. Engine binaries (even with a
# bundled NNUE) are well under this; the cap bounds memory/disk for a hostile or
# accidental oversized upload/download.
_MAX_ENGINE_PAYLOAD_BYTES = 256 * 1024 * 1024


def _now_iso() -> str:
    """UTC ISO-8601 timestamp for recording when a custom engine was added."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _looks_like_gzip(path: str) -> bool:
    """Whether a file begins with the gzip magic bytes (a .tar.gz payload).

    Used to classify a downloaded payload as archive-vs-raw-binary by content
    rather than trusting the URL's extension.
    """
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def _save_capped(src, dst, cap: int) -> Optional[int]:
    """Copy ``src`` to ``dst`` in chunks, returning the byte count or None if over ``cap``.

    Returning None (rather than raising) lets the caller surface a clean 413 for
    an oversized upload without writing the whole stream first.
    """
    total = 0
    while True:
        chunk = src.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            return None
        dst.write(chunk)
    return total


def _download_capped(url: str, cap: int, on_fraction=None):
    """Stream an HTTPS URL to a temp file under a size cap.

    Returns ``(temp_path, None)`` on success or ``(None, error)`` on failure /
    oversize. The URL is assumed already validated by
    ``custom_engines.validate_download_url`` (HTTPS, non-private target).
    """
    import urllib.request
    import urllib.error

    fd, tmp_path = tempfile.mkstemp(prefix="engine_dl_")
    out = os.fdopen(fd, "wb")
    try:
        # https scheme is enforced upstream by validate_download_url, so file:/
        # custom schemes cannot reach here (the S310/B310 audit concern).
        req = urllib.request.Request(url, headers={"User-Agent": "Universal-Chess"})  # noqa: S310  # nosec B310
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  # nosec B310
            try:
                total_expected = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total_expected = 0
            downloaded = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > cap:
                    out.close()
                    os.unlink(tmp_path)
                    return None, "Downloaded file is too large."
                out.write(chunk)
                if on_fraction and total_expected > 0:
                    on_fraction(min(downloaded / total_expected, 1.0))
        out.close()
        return tmp_path, None
    except (urllib.error.URLError, OSError, ValueError) as e:
        out.close()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        # The exception text can carry the URL / urllib internals; this error is
        # surfaced to the client via the install-status endpoint, so log the
        # detail server-side and return a generic message (CWE-209).
        app.logger.warning("Custom engine download failed for %s: %s", url, e)
        return None, "Download failed."


def _remove_custom_engine_files(engine_id: str) -> bool:
    """Delete a custom engine's binary at ENGINES_DIR/<id>.

    Defends against an id that would resolve outside the engines directory even
    though ids are validated at add time (defense in depth). Returns False if the
    target would escape the engines dir.
    """
    target = safe_under_base(_ENGINES_DIR, engine_id)
    if target is None:
        return False
    if os.path.exists(target):
        os.remove(target)
    return True


def _run_custom_url_install(engine_id: str, display_name: str, url: str):
    """Background worker: download a custom engine from ``url`` and install it.

    Streams the (already-validated) HTTPS URL to a temp file under a size cap,
    classifies it as raw-binary vs gzip archive by content, places exactly one
    validated, arch-matching binary at ENGINES_DIR/<id>, registers it, and
    records the structured result through the shared install-state store so the
    UI progress bar and activity banner reflect it.
    """
    from universalchess.services.engine_install_state import InstallStage

    tmp_path = None
    try:
        _engine_install_store.update(InstallStage.DOWNLOADING, f"Downloading {display_name}...")
        tmp_path, dl_err = _download_capped(
            url,
            _MAX_ENGINE_PAYLOAD_BYTES,
            lambda frac: _engine_install_store.update(
                InstallStage.DOWNLOADING, f"Downloading {display_name}...", frac
            ),
        )
        if dl_err:
            _engine_install_store.finish(success=False, error=dl_err)
            return

        _engine_install_store.update(InstallStage.INSTALLING_FILES, f"Installing {display_name}...")
        # Resolve the destination through the containment guard before it reaches
        # any filesystem operation (the id is validated, but this is the barrier
        # static analysis recognizes against path injection).
        dest_path = safe_under_base(_ENGINES_DIR, engine_id)
        if dest_path is None:
            _engine_install_store.finish(success=False, error="Invalid engine id.")
            return
        err = _custom_engines.install_binary_payload(
            source_path=tmp_path,
            is_archive=_looks_like_gzip(tmp_path),
            dest_path=dest_path,
            expected_arch=get_current_arch(),
            safe_extract=_safe_extract_tar,
        )
        if err:
            _engine_install_store.finish(success=False, error=err)
            return

        _custom_engine_store.add(
            CustomEngine(
                id=engine_id,
                display_name=display_name,
                source="url",
                url=url,
                created_at=_now_iso(),
            )
        )
        _engine_install_store.finish(success=True, error=None)
    except Exception as e:
        app.logger.exception("Custom URL engine install failed: %s", e)
        _engine_install_store.finish(success=False, error="Installation failed")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _run_engine_install(engine_name: str, ref: Optional[str] = None):
    """Background thread to install an engine, persisting structured progress.

    The stage_callback writes each update to the store so a concurrent
    GET /api/engines/status (and any fresh page load) sees the live stage and
    percent. ``ref`` is the optional git ref the user chose in the tag picker
    (None for the canonical ref).
    """
    from universalchess.managers.engine_manager import EngineManager

    def on_stage(stage, message, fraction):
        _engine_install_store.update(stage, message, fraction)

    try:
        engine_manager = EngineManager()
        success = engine_manager.install_engine(engine_name, stage_callback=on_stage, ref=ref)
        error = None if success else (engine_manager.get_install_error() or "Installation failed")
        _engine_install_store.finish(success=success, error=error)
    except Exception as e:
        app.logger.exception("Engine install failed: %s", e)
        _engine_install_store.finish(success=False, error="Installation failed")


def _start_engine_install(engine_name: str, ref: Optional[str] = None):
    """Initialize the persisted state and spawn the install thread.

    Caller is responsible for validating the engine name, the ref, and that no
    install is already active.
    """
    from universalchess.managers.engine_manager import ENGINES
    engine = ENGINES[engine_name]
    _engine_install_store.start(
        engine_name,
        engine.display_name,
        estimated_seconds=engine.estimated_install_minutes * 60,
    )
    thread = threading.Thread(target=_run_engine_install, args=(engine_name, ref), daemon=True)
    thread.start()


@app.route("/api/engines/<engine_name>/refs", methods=["GET"])
def api_get_engine_refs(engine_name):
    """List the selectable git refs (tags/branches) for a source-built engine.

    Merges live GitHub tags (best-effort; degrades to locally-known refs offline)
    with the catalog pin, the working-ref history, and the installed ref, flagging
    each. Drives the tag picker; the UI omits the picker when
    ``source_installable`` is False.
    """
    try:
        from universalchess.managers.engine_manager import EngineManager, ENGINES
        if engine_name not in ENGINES:
            return jsonify({"error": f"Unknown engine: {engine_name}"}), 404
        engine_manager = EngineManager()
        return jsonify(engine_manager.get_engine_refs(engine_name))
    except Exception as e:
        return _internal_error(e)


@app.route("/api/engines/install", methods=["POST"])
@requires_auth
def api_install_engine():
    """Start installing an engine, optionally from a chosen git ref.

    Requires authentication: installing runs apt/source builds and modifies the
    system, so it is gated like the other privileged mutations (update install,
    delengine, board/system control).
    """
    try:
        data = request.get_json()
        engine_name = data.get("engine") if data else None
        ref = data.get("ref") if data else None

        if not engine_name:
            return jsonify({"success": False, "error": "No engine specified"}), 400

        from universalchess.managers.engine_manager import ENGINES, is_valid_ref
        if engine_name not in ENGINES:
            return jsonify({"success": False, "error": f"Unknown engine: {engine_name}"}), 400

        # An empty/omitted ref means "canonical" (the prior behavior). A provided
        # ref must be syntactically valid before it reaches `git clone --branch`.
        if ref is not None and ref != "" and not is_valid_ref(ref):
            return jsonify({"success": False, "error": f"Invalid ref: {ref}"}), 400
        ref = ref or None

        if _engine_install_store.status_dict()["active"]:
            return jsonify({
                "success": False,
                "error": f"Already installing {_engine_install_store.status_dict()['engine']}"
            }), 409

        _start_engine_install(engine_name, ref=ref)
        return jsonify({"success": True, "message": f"Installing {engine_name}"})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/engines/upload", methods=["POST"])
@requires_auth
def api_upload_engine():
    """Upload a custom UCI engine binary or .tar.gz. Requires authentication.

    The uploaded file becomes an executable the board will launch, so this is
    gated like install. The id is validated to a filesystem-safe token that
    cannot collide with the catalog, the upload is staged to a size-capped temp
    file, and the binary's architecture must match this device. A .tar.gz must
    resolve to exactly one matching-arch binary.
    """
    try:
        engine_id = (request.form.get("id") or "").strip()
        display_name = (request.form.get("display_name") or "").strip()

        from universalchess.managers.engine_manager import ENGINES
        existing_ids = {e.id for e in _custom_engine_store.list()}
        id_err = _custom_engines.validate_engine_id(
            engine_id, builtin_ids=set(ENGINES), existing_ids=existing_ids
        )
        if id_err:
            return jsonify({"success": False, "error": id_err}), 400
        name_err = _custom_engines.validate_display_name(display_name)
        if name_err:
            return jsonify({"success": False, "error": name_err}), 400

        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"success": False, "error": "No file uploaded"}), 400

        is_archive = uploaded.filename.endswith(".tar.gz") or uploaded.filename.endswith(".tgz")

        tmp_fd, tmp_path = tempfile.mkstemp(prefix="engine_upload_")
        try:
            with os.fdopen(tmp_fd, "wb") as tmp:
                copied = _save_capped(uploaded.stream, tmp, _MAX_ENGINE_PAYLOAD_BYTES)
            if copied is None:
                return jsonify({"success": False, "error": "Uploaded file is too large."}), 413

            # Resolve under the engines dir via the containment guard before the
            # path reaches any filesystem operation (path-injection barrier).
            dest_path = safe_under_base(_ENGINES_DIR, engine_id)
            if dest_path is None:
                return jsonify({"success": False, "error": "Invalid engine id."}), 400
            err = _custom_engines.install_binary_payload(
                source_path=tmp_path,
                is_archive=is_archive,
                dest_path=dest_path,
                expected_arch=get_current_arch(),
                safe_extract=_safe_extract_tar,
            )
            if err:
                return jsonify({"success": False, "error": err}), 400
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        _custom_engine_store.add(
            CustomEngine(
                id=engine_id,
                display_name=display_name,
                source="upload",
                url=None,
                created_at=_now_iso(),
            )
        )
        return jsonify({"success": True, "message": f"Uploaded {display_name}"})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/engines/install-url", methods=["POST"])
@requires_auth
def api_install_engine_from_url():
    """Install a custom engine by downloading it from an HTTPS URL. Requires auth.

    The URL is restricted to HTTPS and may not resolve to a private/loopback/
    link-local address (SSRF guard). The download, architecture validation, and
    placement run asynchronously through the shared install-state store so the UI
    shows progress; only one install may run at a time.
    """
    try:
        data = request.get_json(silent=True) or {}
        engine_id = (data.get("id") or "").strip()
        display_name = (data.get("display_name") or "").strip()
        url = (data.get("url") or "").strip()

        from universalchess.managers.engine_manager import ENGINES
        existing_ids = {e.id for e in _custom_engine_store.list()}
        id_err = _custom_engines.validate_engine_id(
            engine_id, builtin_ids=set(ENGINES), existing_ids=existing_ids
        )
        if id_err:
            return jsonify({"success": False, "error": id_err}), 400
        name_err = _custom_engines.validate_display_name(display_name)
        if name_err:
            return jsonify({"success": False, "error": name_err}), 400
        url_err = _custom_engines.validate_download_url(url)
        if url_err:
            return jsonify({"success": False, "error": url_err}), 400

        if _engine_install_store.status_dict()["active"]:
            return jsonify({
                "success": False,
                "error": f"Already installing {_engine_install_store.status_dict()['engine']}",
            }), 409

        _engine_install_store.start(engine_id, display_name, estimated_seconds=120)
        thread = threading.Thread(
            target=_run_custom_url_install,
            args=(engine_id, display_name, url),
            daemon=True,
        )
        thread.start()
        return jsonify({"success": True, "message": f"Installing {display_name}"})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/engines/uninstall", methods=["POST"])
@requires_auth
def api_uninstall_engine():
    """Uninstall an engine. Requires authentication (system mutation)."""
    try:
        data = request.get_json()
        engine_name = data.get("engine")
        
        if not engine_name:
            return jsonify({"success": False, "error": "No engine specified"}), 400
        
        from universalchess.managers.engine_manager import EngineManager, ENGINES

        # Custom (operator-added) engines are not in the catalog: remove the
        # binary and the registry entry directly. Checked before the catalog
        # membership test so a custom id is not rejected as "Unknown engine".
        custom = _custom_engine_store.get(engine_name)
        if custom is not None:
            _remove_custom_engine_files(engine_name)
            _custom_engine_store.remove(engine_name)
            return jsonify({"success": True})

        if engine_name not in ENGINES:
            return jsonify({"success": False, "error": f"Unknown engine: {engine_name}"}), 400
        
        engine_def = ENGINES[engine_name]
        if not engine_def.can_uninstall:
            return jsonify({"success": False, "error": "This engine cannot be uninstalled"}), 400
        
        engine_manager = EngineManager()
        success = engine_manager.uninstall_engine(engine_name)
        
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Uninstall failed"})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/engines/status", methods=["GET"])
def api_engine_status():
    """Get current engine installation status.

    Returns the structured state (stage, message, derived percent, active,
    interrupted, result). Percent is computed at read time so the build bar
    advances smoothly between polls without the backend ticking.
    """
    return jsonify(_engine_install_store.status_dict())


@app.route("/api/system/activity", methods=["GET"])
def api_system_activity():
    """Aggregate active background tasks for the top-of-screen web banner.

    Combines the structured engine-install state with the BlueZ self-heal
    progress record into one uniform list (see services/background_activity) so
    the banner can render any background work generically. ``read_progress`` and
    ``status_dict`` never raise, so a missing marker/state degrades to "idle"
    rather than erroring the poll. Returns ``{"active": bool, "activities": []}``.
    """
    from universalchess.managers.bluez_patch_status import read_progress
    from universalchess.services.background_activity import activity_snapshot

    return jsonify(activity_snapshot(
        _engine_install_store.status_dict(),
        read_progress(),
    ))


@app.route("/api/engines/resume", methods=["POST"])
def api_resume_engine_install():
    """Resume an install that was interrupted by a process/board restart.

    Valid only when the persisted state is `interrupted`. Relaunches the install
    for that engine; the cached git clone is reused (git pull), so engine source
    is not re-downloaded. True mid-build continuation is not possible -- "resume"
    re-runs the install operation, which is idempotent.
    """
    try:
        status = _engine_install_store.status_dict()
        if status["active"]:
            return jsonify({"success": False, "error": f"Already installing {status['engine']}"}), 409
        if not status["interrupted"]:
            return jsonify({"success": False, "error": "No interrupted install to resume"}), 400

        engine_name = status["engine"]
        from universalchess.managers.engine_manager import ENGINES
        if engine_name not in ENGINES:
            _engine_install_store.clear()
            return jsonify({"success": False, "error": f"Unknown engine: {engine_name}"}), 400

        _start_engine_install(engine_name)
        return jsonify({"success": True, "message": f"Resuming {engine_name}"})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/engines/cancel", methods=["POST"])
def api_cancel_engine_install():
    """Dismiss an interrupted or finished install by clearing its persisted state.

    Does not abort an actively running build (out of scope): returns 409 if an
    install is currently running so the running thread is never orphaned.
    """
    try:
        if _engine_install_store.status_dict()["active"]:
            return jsonify({"success": False, "error": "Cannot cancel a running install"}), 409
        _engine_install_store.clear()
        return jsonify({"success": True})
    except Exception as e:
        return _internal_error(e)


# -----------------------------------------------------------------------------
# Update System API
# -----------------------------------------------------------------------------

@app.route("/api/updates/status", methods=["GET"])
def api_update_status():
    """Get current update system status.
    
    Returns:
        JSON with channel, versions, pending update status, etc.
    """
    try:
        from universalchess.services.update_service import get_update_service
        service = get_update_service()
        return jsonify(service.get_status_dict())
    except Exception as e:
        return _internal_error(e)


@app.route("/api/updates/check", methods=["POST"])
@requires_auth
def api_update_check():
    """Check for available updates.
    
    Returns:
        JSON with update availability and version info
    """
    try:
        from universalchess.services.update_service import get_update_service
        service = get_update_service()
        release = service.check_for_updates()
        
        if release:
            return jsonify({
                "update_available": True,
                "version": release.version,
                "tag": release.tag,
                "name": release.name,
                "published_at": release.published_at,
                "is_nightly": release.is_nightly,
                "download_size": release.download_size,
                "body": release.body,
            })
        else:
            return jsonify({
                "update_available": False,
                "current_version": service.get_current_version(),
            })
    except Exception as e:
        return _internal_error(e)


@app.route("/api/updates/download", methods=["POST"])
@requires_auth
def api_update_download():
    """Download the available update.
    
    Returns:
        JSON with success status and download path
    """
    try:
        from universalchess.services.update_service import get_update_service
        service = get_update_service()
        
        deb_path = service.download_update()
        if deb_path:
            return jsonify({
                "success": True,
                "path": str(deb_path),
            })
        else:
            return jsonify({
                "success": False,
                "error": "Download failed",
            }), 500
    except Exception as e:
        return _internal_error(e)


@app.route("/api/updates/install", methods=["POST"])
@requires_auth
def api_update_install():
    """Install the pending update.
    
    Returns:
        JSON with success status
    """
    try:
        from universalchess.services.update_service import get_update_service
        service = get_update_service()
        
        if service.install_pending_update():
            # The install runs asynchronously in a transient systemd unit, so
            # success here means it was *launched*, not completed. The board
            # and web service are restarted by the package postinst when the
            # install finishes; the client should expect a brief disconnect.
            return jsonify({
                "success": True,
                "message": "Update installation started. The board will restart when it completes.",
            })
        else:
            return jsonify({
                "success": False,
                "error": "Could not start the update installation",
            }), 500
    except Exception as e:
        return _internal_error(e)


@app.route("/api/updates/channel", methods=["GET"])
def api_update_channel_get():
    """Get current update channel."""
    try:
        from universalchess.services.update_service import get_update_service
        service = get_update_service()
        return jsonify({
            "channel": service.get_channel().value,
        })
    except Exception as e:
        return _internal_error(e)


@app.route("/api/updates/channel", methods=["POST"])
@requires_auth
def api_update_channel_set():
    """Set update channel.
    
    Body:
        {"channel": "stable" | "nightly"}
    """
    try:
        from universalchess.services.update_service import get_update_service, UpdateChannel
        service = get_update_service()
        
        data = request.get_json(force=True)
        channel_str = data.get("channel", "stable")
        
        try:
            channel = UpdateChannel(channel_str)
        except ValueError:
            return jsonify({"error": f"Invalid channel: {channel_str}"}), 400
        
        service.set_channel(channel)
        return jsonify({
            "success": True,
            "channel": channel.value,
        })
    except Exception as e:
        return _internal_error(e)


@app.route("/api/updates/auto", methods=["GET"])
def api_update_auto_get():
    """Get auto-update setting."""
    try:
        from universalchess.services.update_service import get_update_service
        service = get_update_service()
        return jsonify({
            "auto_update": service.is_auto_update_enabled(),
        })
    except Exception as e:
        return _internal_error(e)


@app.route("/api/updates/auto", methods=["POST"])
@requires_auth
def api_update_auto_set():
    """Set auto-update setting.
    
    Body:
        {"enabled": true | false}
    """
    try:
        from universalchess.services.update_service import get_update_service
        service = get_update_service()
        
        data = request.get_json(force=True)
        enabled = data.get("enabled", False)
        
        service.set_auto_update(bool(enabled))
        return jsonify({
            "success": True,
            "auto_update": service.is_auto_update_enabled(),
        })
    except Exception as e:
        return _internal_error(e)


# -----------------------------------------------------------------------------
# TLS CA certificate endpoints
# -----------------------------------------------------------------------------

@app.route("/ca-install")
def ca_install_page():
    """Serve the CA certificate installation page.

    Served over HTTP so clients can bootstrap trust before HTTPS is available.
    This is the only substantive page served on the HTTP listener; nginx
    redirects all other HTTP paths to HTTPS.
    """
    return render_template("ca_install.html")


@app.route("/ca.pem")
def ca_download():
    """Serve the CA root certificate in various formats.

    Query parameters:
        format=mobileconfig  Apple .mobileconfig profile (iOS/iPadOS)
        format=der           DER-encoded .crt (Android)
        qr=1                 SVG QR code pointing to the PEM download URL
        (default)            PEM format

    Served over HTTP (unauthenticated) so clients can install the CA before
    they have access to the HTTPS app.
    """
    from universalchess.tls import get_ca_cert_path, generate_mobileconfig

    ca_path = get_ca_cert_path(pathlib.Path(CONFIG_DIR))

    if not ca_path.exists():
        abort(404, description="CA certificate not found. TLS may not be configured.")

    fmt = request.args.get("format", "")
    qr = request.args.get("qr", "")

    if qr == "1":
        try:
            import segno
            download_url = f"http://{request.host}/ca.pem"
            qr_code = segno.make(download_url)
            buffer = io.BytesIO()
            qr_code.save(buffer, kind="svg", scale=5, dark="#000000", light="#ffffff")
            buffer.seek(0)
            return Response(buffer.getvalue(), mimetype="image/svg+xml")
        except ImportError:
            abort(500, description="QR code generation requires the segno package.")

    if fmt == "mobileconfig":
        profile_data = generate_mobileconfig(ca_path)
        return Response(
            profile_data,
            mimetype="application/x-apple-aspen-config",
            headers={"Content-Disposition": "attachment; filename=UniversalChess-CA.mobileconfig"},
        )

    if fmt == "der":
        from cryptography.x509 import load_pem_x509_certificate
        from cryptography.hazmat.primitives.serialization import Encoding

        cert_pem = ca_path.read_bytes()
        cert = load_pem_x509_certificate(cert_pem)
        cert_der = cert.public_bytes(Encoding.DER)
        return Response(
            cert_der,
            mimetype="application/x-x509-ca-cert",
            headers={"Content-Disposition": "attachment; filename=UniversalChess-CA.crt"},
        )

    return Response(
        ca_path.read_bytes(),
        mimetype="application/x-pem-file",
        headers={"Content-Disposition": "attachment; filename=UniversalChess-CA.pem"},
    )


# -----------------------------------------------------------------------------
# Password change endpoint
# -----------------------------------------------------------------------------

_MIN_PASSWORD_LENGTH = 4
# Characters that act as record/field separators in chpasswd(8) stdin. Any of
# these in the username or password could inject an additional "user:password"
# record, so they are rejected before the value is passed to chpasswd.
_CHPASSWD_FORBIDDEN_CHARS = ("\n", "\r", "\0")


@app.route("/api/system/change-password", methods=["POST"])
@requires_auth
def api_change_password():
    """Change the authenticated user's system password.

    Requires HTTPS (checked via X-Forwarded-Proto from nginx) to prevent
    credentials from being sent in cleartext. Accepts JSON body:

        {"current_password": "...", "new_password": "..."}

    Uses chpasswd(8) to update the Linux user password.
    """
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "http")
    if forwarded_proto != "https":
        return jsonify({"error": "Password change requires HTTPS"}), 403

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")

    if not current_password:
        return jsonify({"error": "Current password is required"}), 400
    if not new_password:
        return jsonify({"error": "New password is required"}), 400
    if len(new_password) < _MIN_PASSWORD_LENGTH:
        return jsonify({
            "error": f"New password must be at least {_MIN_PASSWORD_LENGTH} characters"
        }), 400
    # chpasswd(8) reads newline-delimited "user:password" records from stdin and
    # offers no escaping. A newline/CR/NUL in the password would append a second
    # record, letting an authenticated caller set another account's password
    # (e.g. root) - privilege escalation. Reject these outright.
    if any(c in new_password for c in _CHPASSWD_FORBIDDEN_CHARS):
        return jsonify({"error": "New password contains invalid characters"}), 400

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return jsonify({"error": "Authentication required"}), 401

    try:
        decoded = base64.b64decode(auth_header[6:], validate=True).decode("utf-8")
        username = decoded.split(":", 1)[0].strip()
    except Exception:
        return jsonify({"error": "Invalid credentials"}), 401

    if not username:
        return jsonify({"error": "Invalid credentials"}), 401
    # The username is interpolated into the same chpasswd record, so a newline in
    # it is the same record-injection vector as the password above.
    if any(c in username for c in _CHPASSWD_FORBIDDEN_CHARS):
        return jsonify({"error": "Invalid credentials"}), 401

    proc = subprocess.run(  # nosec B603 B607 - argv list (no shell); 'sudo'/'chpasswd' are standard system binaries
        ["sudo", "-n", "chpasswd"],  # noqa: S607 - argv list (no shell); 'sudo'/'chpasswd' are standard system binaries
        input=f"{username}:{new_password}",
        capture_output=True,
        text=True,
        timeout=10,
    )

    if proc.returncode != 0:
        app.logger.error("chpasswd failed for user %s: %s", username, proc.stderr.strip())
        return jsonify({"error": "Failed to change password"}), 500

    return jsonify({"success": True})


# -----------------------------------------------------------------------------
# Server-Sent Events for real-time game state updates
# -----------------------------------------------------------------------------

import queue

# Thread-safe queue for SSE clients - each client gets its own queue
_sse_clients: list[queue.Queue] = []
_sse_clients_lock = threading.Lock()


def broadcast_sse_event(event_type: str, data: dict = None) -> None:
    """Broadcast a custom SSE event to all connected clients.
    
    Args:
        event_type: Type of event (e.g., 'settings_changed').
        data: Optional data payload (JSON-serializable).
    """
    message = json.dumps({
        "type": event_type,
        **(data or {})
    })
    with _sse_clients_lock:
        for client_queue in _sse_clients:
            try:
                client_queue.put_nowait(message)
            except queue.Full:  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
                pass


def _on_game_state_update(state: GameState) -> None:
    """Callback invoked when game state is received from main app.
    
    Broadcasts the state to all connected SSE clients.
    """
    message = state.to_json()
    with _sse_clients_lock:
        for client_queue in _sse_clients:
            try:
                # Non-blocking put - drop if client is slow
                client_queue.put_nowait(message)
            except queue.Full:  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
                pass  # Client is too slow, skip this update


def _on_raw_message(parsed: dict) -> None:
    """Callback for raw messages from main app (including generic events).
    
    Forwards non-game-state events to SSE clients.
    Game state events are handled by _on_game_state_update.
    """
    event_type = parsed.get("type")
    if event_type and event_type != "game_state":
        # Forward generic events (like settings_changed) to SSE clients
        message = json.dumps(parsed)
        with _sse_clients_lock:
            for client_queue in _sse_clients:
                try:
                    client_queue.put_nowait(message)
                except queue.Full:  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
                    pass


def _init_game_subscriber():
    """Initialize the game state subscriber (called once on app startup)."""
    try:
        subscriber = get_subscriber()
        subscriber.add_callback(_on_game_state_update)
        subscriber.add_raw_callback(_on_raw_message)
        subscriber.start()
    except Exception as e:
        # Log but don't crash - SSE is optional enhancement
        print(f"[SSE] Failed to start game subscriber: {e}")


# Start subscriber when module loads (Flask is already running)
_init_game_subscriber()


@app.route("/events")
def sse_events():
    """Server-Sent Events endpoint for real-time game state updates.
    
    Clients connect here to receive push updates when moves are made.
    Each update contains the full game state (FEN, PGN, player names, etc).
    
    Usage (JavaScript):
        const eventSource = new EventSource('/events');
        eventSource.onmessage = (event) => {
            const state = JSON.parse(event.data);
            console.log('New position:', state.fen);
            console.log('PGN:', state.pgn);
        };
    """
    def generate():
        # Create a queue for this client
        client_queue = queue.Queue(maxsize=10)
        
        with _sse_clients_lock:
            _sse_clients.append(client_queue)
        
        try:
            # Send immediate comment to trigger browser onopen event.
            # Without this, onopen only fires after the first real data arrives,
            # causing the connection status to remain "Reconnecting..." for up to 30s.
            yield ": connected\n\n"
            
            # Send initial state if available
            subscriber = get_subscriber()
            last_state = subscriber.get_last_state()
            if last_state:
                yield f"data: {last_state.to_json()}\n\n"
            else:
                # No cached state (e.g. fresh web-service start): the game->web
                # broadcast is one-way with no replay, so ask the board to
                # re-broadcast now. This client_queue is already registered, so
                # the response arrives on it below within milliseconds.
                from universalchess.services.game_broadcast import request_game_state_broadcast
                request_game_state_broadcast()
            
            # Stream updates as they arrive
            while True:
                try:
                    # Wait for next update (with timeout to detect disconnects)
                    message = client_queue.get(timeout=30)
                    yield f"data: {message}\n\n"
                except queue.Empty:
                    # Send keepalive comment to detect broken connections
                    yield ": keepalive\n\n"
        finally:
            # Clean up when client disconnects
            with _sse_clients_lock:
                if client_queue in _sse_clients:
                    _sse_clients.remove(client_queue)
    
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


# React Router catch-all - must be last to avoid interfering with API routes
# This handles client-side routing in the React app (e.g., /live, /games, /settings)
@app.route("/<path:path>")
def react_catch_all(path):
    """Serve React app for client-side routing.
    
    This catches any path not matched by other routes and serves the React
    index.html, allowing React Router to handle the route.
    """
    react_dir = get_react_app_dir()
    if react_dir:
        # Serve a real build file if the path maps to one; send_from_directory
        # safely contains the user-supplied path within react_dir.
        try:
            return send_from_directory(react_dir, path)
        except NotFound:  # noqa: S110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
        # Otherwise serve index.html for client-side routing
        return send_file(react_dir / "index.html")
    # No React app, 404
    abort(404)