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
from universalchess.utils.timeutils import to_utc_iso
from universalchess.db import models
from universalchess.paths import get_current_fen, get_current_placement, get_resource_path
from universalchess.services.game_broadcast import get_subscriber, GameState
from universalchess.paths import EPAPER_STATIC_JPG, CONFIG_DIR
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine, MetaData
from sqlalchemy.sql import func
from sqlalchemy import select
from sqlalchemy import delete
import os
import re
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
from xml.sax.saxutils import escape  # nosec B406  # nosemgrep: python.lang.security.use-defused-xml.use-defused-xml  # saxutils.escape performs output encoding (escaping), not XML parsing

from universalchess.web.piece_svg import (
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

# Origin the opt-in deep-analysis engine is fetched from. Reached only when the
# game.deep_analysis setting is on, and only by fetch() -- never as a script
# source; see build_content_security_policy.
DEEP_ANALYSIS_CDN_ORIGIN = "https://cdn.jsdelivr.net"


def build_content_security_policy(deep_analysis: bool) -> str:
    """Build the Content-Security-Policy header for this install.

    Baseline relaxations, each required by an existing trusted feature:
      - ``script-src 'unsafe-inline'``: the ca_install.html certificate-install
        page (served over plain HTTP before the CA is trusted) embeds inline
        ``<script>`` and onclick handlers. The React build uses external bundles
        and does not rely on this.
      - ``connect-src 'self'``: SSE (/events) and the JSON API are same-origin.

    ``object-src 'none'``, ``base-uri 'self'`` and ``frame-ancestors 'self'`` are
    the meaningful hardening (no plugins, no <base> hijack, no framing).

    The default install executes no WebAssembly and creates no worker, so it
    grants neither ``'wasm-unsafe-eval'`` nor ``worker-src blob:``. Both existed
    for the bundled Stockfish WASM that has been removed.

    Args:
        deep_analysis: When True, permit the opt-in CDN engine: WebAssembly, a
            Blob worker, and jsDelivr on ``connect-src`` *only*. Keeping the CDN
            off ``script-src`` is deliberate -- the page fetches all three assets
            and verifies each SHA-256 before creating the worker, and an origin
            on ``script-src`` would let the CDN serve executable script that
            bypasses that check entirely. ``blob:`` is granted on ``connect-src``
            for the same reason: the worker reads its WebAssembly and its neural
            net back from object URLs holding the already-verified bytes, so it
            makes no network request of its own.

    Returns:
        The header value.
    """
    script_src = ["'self'", "'unsafe-inline'"]
    connect_src = ["'self'"]
    worker_src = ["'self'"]
    if deep_analysis:
        script_src.append("'wasm-unsafe-eval'")
        connect_src.extend(["blob:", DEEP_ANALYSIS_CDN_ORIGIN])
        worker_src.append("blob:")

    return "; ".join([
        "default-src 'self'",
        f"script-src {' '.join(script_src)}",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        f"connect-src {' '.join(connect_src)}",
        f"worker-src {' '.join(worker_src)}",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'self'",
    ])


# Cached (config mtime, flag) for deep_analysis. The CSP is emitted on every
# response, so the setting cannot be re-parsed each time; keying the cache on the
# file's mtime keeps it correct no matter which process wrote the change (the
# board's menus write the same file).
_deep_analysis_cache = (None, False)


def reset_deep_analysis_cache() -> None:
    """Discard the cached deep-analysis flag, forcing a re-read."""
    global _deep_analysis_cache
    _deep_analysis_cache = (None, False)


def deep_analysis_enabled() -> bool:
    """Whether opt-in CDN deep analysis is turned on for this install.

    Fails closed: an unreadable or malformed config yields False, so a broken
    centaur.ini cannot quietly hand every install the widened policy.
    """
    global _deep_analysis_cache
    import configparser
    import os

    from universalchess.board.settings import Settings

    try:
        mtime = os.stat(Settings.configfile).st_mtime
    except OSError:
        return False

    cached_mtime, cached_value = _deep_analysis_cache
    if cached_mtime == mtime:
        return cached_value

    config = configparser.ConfigParser()
    try:
        config.read(Settings.configfile)
        value = config.getboolean("game", "deep_analysis", fallback=False)
    except configparser.Error:
        return False

    _deep_analysis_cache = (mtime, value)
    return value


def apply_security_headers(response):
    """Attach baseline security headers to a response.

    Sets nosniff, anti-clickjacking, a strict referrer policy and the CSP.
    Applied to all responses so API/SSE/media paths are covered too; the CSP
    only constrains document/script execution contexts.
    """
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault(
        'Content-Security-Policy',
        build_content_security_policy(deep_analysis_enabled()),
    )
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
    '.wasm': CACHE_LONG,
}

# Path prefixes that serve immutable, content-addressed build assets (the Vite
# bundle and icons). Only responses under these prefixes are eligible for long
# browser caching; everything else defaults to no-store (see add_cache_headers)
# so dynamic data is never served stale.
STATIC_ASSET_PREFIXES = ('/static/', '/assets/', '/icons/')


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

    # Immutable, content-addressed build assets: cache by extension. Gated on a
    # successful response so a transient 404/500 for an asset path is never
    # cached. Only the known static-asset prefixes qualify; a dynamic endpoint
    # can never accidentally match (e.g. a service worker at /sw.js is a .js but
    # is NOT under a prefix, so it stays uncached and picks up new builds).
    if response.status_code == 200 and path.startswith(STATIC_ASSET_PREFIXES):
        ext = os.path.splitext(path)[1].lower()
        max_age = CACHEABLE_EXTENSIONS.get(ext, CACHE_SHORT)
        response.headers['Cache-Control'] = f'public, max-age={max_age}'
        return response

    # HTML pages - always revalidate so a new build/SPA shell is picked up.
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    # Everything else is dynamic: the JSON API (including the legacy non-/api/
    # endpoints /getgames, /getpgn, ...), SSE, generated media, the service
    # worker and the web manifest. Never cache it.
    #
    # Defaulting to no-store -- rather than the previous blanket
    # `public, max-age=CACHE_SHORT` -- is deliberate and fixes a real bug: the
    # games list (/getgames) fell through to that default and was cached for an
    # hour, so after deleting a game the list re-fetch was served stale from the
    # browser cache and the deleted row remained on screen (while the game was
    # genuinely gone from the DB, so opening it 404'd). Caching is now opt-in for
    # static assets only; a newly added data endpoint is safe by default instead
    # of silently cacheable.
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


# ---------------------------------------------------------------------------
# Inactivity timer reset: a genuine user action in the web UI resets the board's
# sleep countdown.  Only state-changing requests (POST/PUT/PATCH/DELETE) to an
# API or legacy action endpoint count.
#
# Reads (GET) never reset the timer -- deliberately.  The frontend polls many
# GET status endpoints on timers regardless of user interaction (the
# always-mounted background-activity banner every ~4s, the update, engine,
# centaur and connectivity indicators, system stats, ...).  If any read reset
# the timer, an idle-but-open browser tab would pin the board awake forever and
# it would never reach its inactivity power-off (the reported "never times out").
# Enumerating those polls proved to be whack-a-mole (a newly added poll silently
# defeats the timer), so the rule keys off intent instead: a poll/view is a GET,
# a user action is a mutation.  Physical board use (keys, piece moves) resets the
# timer through its own path, and a purely-reading web session still gets the
# 2-minute on-board countdown warning before power-off.
#
# Best-effort: failures are silently ignored so a broken IPC socket never blocks
# the web response.
# ---------------------------------------------------------------------------
_INACTIVITY_RESET_PREFIXES = ("/api/",)
_INACTIVITY_RESET_EXACT = (
    "/deletegame/",
    "/uploadengine", "/delengine/",
)
_INACTIVITY_RESET_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.after_request
def reset_board_inactivity(response):
    """Signal user activity to the board so the sleep timer resets."""
    # Reads (GET/HEAD/OPTIONS) are polls or passive views, not user activity.
    if request.method not in _INACTIVITY_RESET_METHODS:
        return response
    path = request.path
    if any(path.startswith(p) for p in _INACTIVITY_RESET_PREFIXES) or \
            any(path.startswith(p) for p in _INACTIVITY_RESET_EXACT):
        try:
            from universalchess.services.game_broadcast import send_board_command
            send_board_command("reset_inactivity")
        except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
            pass
    return response


# System paths for conditional features
ENGINES_DIR = "/opt/universalchess/engines"

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
    Formats a Unix timestamp as an ISO 8601 UTC string (``...Z``).

    Uses ``time.gmtime`` (not ``localtime``) so the ``Z`` designator is
    truthful: the value is UTC. A ``localtime`` value labelled ``Z`` is local
    time mislabelled as UTC, which a WebDAV client would parse as an instant
    shifted by the device's offset.

    Args:
        timestamp: Unix timestamp (seconds since the epoch)

    Returns:
        ISO 8601 UTC date string, e.g. ``2026-07-10T01:22:33Z``
    """
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(timestamp))

def format_date_rfc(timestamp):
    """
    Formats a Unix timestamp as an RFC 1123 date string in GMT.

    HTTP dates must be GMT (RFC 7231); ``time.gmtime`` with a literal ``GMT``
    keeps the value and its label consistent. A ``localtime``/``%Z`` value emits
    a local zone abbreviation, which is both non-conformant and off by the
    device's offset.

    Args:
        timestamp: Unix timestamp (seconds since the epoch)

    Returns:
        RFC 1123 date string, e.g. ``Fri, 10 Jul 2026 01:22:33 GMT``
    """
    return time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(timestamp))

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
    
    # created_at is already an ISO-8601 UTC string (e.g. 2026-07-10T01:22:33+00:00),
    # which is a valid WebDAV creationdate; use it directly. Empty when absent.
    creation_date_iso = gameitem["created_at"]
    
    props = []
    props.append('<D:response>')
    props.append('<D:href>' + href_path + '</D:href>')
    props.append('<D:propstat>')
    props.append('<D:prop>')
    props.append('<D:getcontentlength>0</D:getcontentlength>')
    props.append('<D:resourcetype></D:resourcetype>')
    props.append('<D:creationdate>' + creation_date_iso + '</D:creationdate>')
    props.append('<D:lastmodified>' + creation_date_iso + '</D:lastmodified>')
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

def _game_status_from_result(result):
    """Classify a game's lifecycle status from its stored PGN result.

    NULL means the game is still in progress; "*" is the PGN code the app writes
    when a game is abandoned/interrupted; any other value ("1-0", "0-1",
    "1/2-1/2") is a finished game. Drives the web Games screen, which offers
    Resume only for in-progress and abandoned games.
    """
    if result is None:
        return "in_progress"
    if result == "*":
        return "abandoned"
    return "finished"


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
    # created_at is stored as naive UTC; serialize with an explicit +00:00 so the
    # browser parses it as UTC and renders it in the viewer's local timezone.
    # Empty string (not "None") when absent so the UI can omit it cleanly.
    gameitem["created_at"] = to_utc_iso(gamedata[0]) or ""
    src = os.path.basename(str(gamedata[1]))
    if src.endswith('.py'):
        src = src[:-3]
    gameitem["source"] = src
    gameitem["event"] = str(gamedata[2])
    gameitem["site"] = str(gamedata[3])
    gameitem["round"] = str(gamedata[4])
    gameitem["white"] = str(gamedata[5])
    gameitem["black"] = str(gamedata[6])
    # result may be NULL (in progress), "*" (abandoned), or a PGN result code for
    # a finished game. Serialize NULL as JSON null rather than the string "None"
    # so the UI can distinguish states, and expose a derived lifecycle status the
    # Games screen gates the Resume action on.
    raw_result = gamedata[7]
    gameitem["result"] = None if raw_result is None else str(raw_result)
    gameitem["status"] = _game_status_from_result(raw_result)
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
    
    # Set headers. created_at is a naive UTC datetime; emit standard PGN date/time
    # tags in UTC (Date is the PGN YYYY.MM.DD form, plus the explicit UTCDate/
    # UTCTime tags) instead of the raw datetime string. Unknown -> PGN placeholder.
    g.headers["Source"] = src
    created_at = gamedata[0]
    if isinstance(created_at, datetime.datetime):
        g.headers["Date"] = created_at.strftime("%Y.%m.%d")
        g.headers["UTCDate"] = created_at.strftime("%Y.%m.%d")
        g.headers["UTCTime"] = created_at.strftime("%H:%M:%S")
    else:
        g.headers["Date"] = "????.??.??"
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
                f.write(request.data)  # nosemgrep: python.django.security.injection.request-data-write.request-data-write  # WebDAV PUT body into path already contained by safe_under_base
            
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
                                    os.chmod(chmod_target, 0o0777)  # noqa: S103  # nosec B103  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # DGT Centaur WebDAV '777.txt' feature; target already contained by safe_under_base
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
        lock_response.append('<D:href>' + safe_path + '</D:href>')  # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format,python.flask.security.injection.raw-html-concat.raw-html-format  # safe_path is escape_xml(sanitized_path); WebDAV XML not HTML
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
        prop_response.append('<D:href>' + escape_xml(sanitized_path) + '</D:href>')  # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format,python.flask.security.injection.raw-html-concat.raw-html-format  # escape_xml applied; WebDAV XML not HTML
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
    """Serve the React app. The build is required; there is no legacy fallback."""
    react_dir = get_react_app_dir()
    if react_dir:
        return send_file(react_dir / "index.html")
    # The legacy Jinja UI was removed; the React build must be present.
    abort(503)


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


@app.route("/fen")
def fen():
    return get_current_placement()

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


@app.route("/api/games", methods=["GET"])
def api_games_list():
    """Return every stored game's summary, newest first.

    The web Games page groups games by month in a side-nav, which needs the full
    set of dates rather than one paginated slice. This returns the same per-game
    summary as /getgames (via build_gameitem_from_gamedata) for all games in one
    array so the client can bucket them. Read-only and unauthenticated like the
    existing /getgames; a game summary contains no secrets. Shape:
        {"games": [{"id", "created_at", "source", "event", "site", "round",
                     "white", "black", "result"}, ...]}
    """
    session = get_db_session()
    try:
        gamedata = session.execute(
            select(models.Game.created_at, models.Game.source, models.Game.event,
                   models.Game.site, models.Game.round, models.Game.white,
                   models.Game.black, models.Game.result, models.Game.id).
            order_by(models.Game.id.desc())
        ).all()
        games = [build_gameitem_from_gamedata(row) for row in gamedata]
        return jsonify({"games": games})
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
    os.chmod(str(target), 0o755)  # noqa: S103  # nosec B103  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # engine needs the exec bit; 0o755 is least-permissive; path contained by safe_under_base
    return "ok"

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


@app.route("/api/games/<int:gameid>/positions")
def api_game_positions(gameid):
    """Return authoritative per-ply positions for a stored game.

    Response: ``{"chess960": bool, "start_fen": str,
                 "positions": [{"fen", "san", "uci", "eval", "best_move"}]}``
    with the start first.

    ``eval`` is the stored centipawn evaluation from White's perspective for the
    position after that ply (+/-10000 for forced mate) and ``best_move`` the
    engine's UCI recommendation, both NULL when the ply was never analysed. They
    come from the board's own analysis: the browser no longer ships an engine,
    so this is the source for the review page's eval chart and best-move arrow.

    The web analysis view navigates and lists a game's history by these
    server-computed FENs instead of replaying the PGN in the browser; the web no
    longer uses chess.js at all. Using python-chess as the single source of truth
    keeps both variants correct (chess.js mis-computes Chess960 castling). The
    FENs are the ones python-chess recorded per move; SAN is recomputed on a
    variant-aware board so a 960 castle reads as ``O-O``/``O-O-O``. 404 when the
    game or its moves are absent.
    """
    session = get_db_session()
    try:
        game = session.query(models.Game).filter(models.Game.id == gameid).first()
        if game is None:
            return jsonify({"error": "not_found"}), 404

        chess960 = bool(getattr(game, "chess960", False))
        move_rows = session.execute(
            select(
                models.GameMove.move,
                models.GameMove.fen,
                models.GameMove.eval_score,
                models.GameMove.best_move,
            )
            .where(models.GameMove.gameid == gameid)
            .order_by(models.GameMove.id)
        ).all()
        if not move_rows:
            return jsonify({"error": "not_found"}), 404

        # The initial-position row has an empty move; its FEN is the start. Fall
        # back to the game's stored start_fen (960 games) or the first row's FEN.
        played = [r for r in move_rows if r[0]]
        initial_row = next((r for r in move_rows if not r[0]), None)
        start_fen = (
            getattr(game, "start_fen", None)
            or (initial_row[1] if initial_row is not None else None)
            or (played[0][1] if played else chess.STARTING_FEN)
        )

        board = chess.Board(start_fen, chess960=chess960)
        initial_eval = initial_row[2] if initial_row is not None else None
        initial_best = initial_row[3] if initial_row is not None else None
        positions = [{
            "fen": board.fen(), "san": None, "uci": None,
            "eval": initial_eval, "best_move": initial_best,
        }]
        for move_uci, stored_fen, eval_score, best_move in played:
            try:
                move = chess.Move.from_uci(move_uci)
            except ValueError:
                # A corrupt UCI can't be replayed; stop at the last good position
                # rather than fabricating one.
                break
            # SAN needs the pre-move board; the authoritative resulting FEN is the
            # stored one. If the move is illegal for this board (corrupt data),
            # fall back to the UCI text so the row still renders.
            try:
                san = board.san(move)
            except (ValueError, AssertionError):
                san = move_uci
            board.push(move)
            positions.append({
                "fen": stored_fen or board.fen(),
                "san": san,
                "uci": move_uci,
                # NULL stays NULL: an unanalysed ply must draw a gap in the
                # chart, not a point at 0.0 (a real "dead equal" evaluation).
                "eval": eval_score,
                "best_move": best_move,
            })

        return jsonify(
            {"chess960": chess960, "start_fen": start_fen, "positions": positions}
        )
    finally:
        session.close()


@app.route("/logo")
def logo_image():
    """Serve the knight logo for the web UI (navbar, About card).

    Prefers the transparent PNG head crop so the white-bodied knight reads on the
    purple navbar and light cards; falls back to the 1-bit board bitmap, then the
    bundled icon, if the PNG is not present (e.g. an older install).
    """
    png_path = get_resource_path("knight_logo.png")
    if os.path.exists(png_path):
        return send_file(png_path, mimetype='image/png')
    bmp_path = get_resource_path("knight_logo.bmp")
    if os.path.exists(bmp_path):
        return send_file(bmp_path, mimetype='image/bmp')
    return redirect(url_for('static', filename='icons/icon.svg'))


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

# Shape of a UCI move the interactive board-control page may play: two squares
# and an optional promotion piece (e.g. "e2e4" or "e7e8q"). This validates the
# form only; legality for the current position is decided authoritatively by the
# board's GameManager, which rejects out-of-turn or illegal moves.
_UCI_MOVE_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")


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


def _read_epaper_snapshot_bytes():
    """Return the e-paper snapshot JPEG bytes, or None if absent/mid-write.

    The board rewrites ``web/static/epaper.jpg`` in place on every panel refresh,
    so a read can catch a truncated file. A cheap SOI/EOI marker check rejects a
    partial read (the caller retries on the next poll) without the cost of a full
    image decode.
    """
    try:
        with open(EPAPER_STATIC_JPG, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if len(data) < 4 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        return None
    return data


@app.route('/screen.jpg')
def screen_snapshot():
    """Return the board's current e-paper snapshot as a single JPEG.

    Replaces the former ``/screen`` MJPEG (``multipart/x-mixed-replace``) stream,
    which does not render inside an ``<img>`` on iPad Safari. The board rewrites
    ``web/static/epaper.jpg`` on every panel refresh and pushes an
    ``epaper_changed`` SSE event; the board-control page reloads this endpoint
    (with a cache-busting ``?t=<mtime>``) on each event. That makes the live
    mirror a sequence of discrete, cache-safe image loads instead of a held-open
    stream.

    Returns 503 when the snapshot is absent (stopped board) or was caught
    mid-write, so the client simply retries on the next event rather than
    rendering a broken image.
    """
    data = _read_epaper_snapshot_bytes()
    if data is None:
        abort(503)
    return Response(
        data,
        mimetype='image/jpeg',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
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


@app.route("/api/board/move", methods=["POST"])
@requires_auth
def api_board_move():
    """Play a move from the interactive board-control page. Requires authentication.

    Forwards a ``make_move`` command to the main process, which applies it through
    the active game exactly like an on-board move, but as a "web move": the
    physical pieces stay put and the board is decoupled until a real piece is
    touched. The move shape is validated here; legality for the current position
    is decided by the board's GameManager (illegal/out-of-turn moves are dropped
    and the browser re-syncs from the live game state).

    Body: {"move": "e2e4"}  (5-char promotion form like "e7e8q" is accepted)
    """
    try:
        body = request.get_json(silent=True) or {}
        uci = (body.get("move") or "").strip().lower()
        if not _UCI_MOVE_RE.match(uci):
            return jsonify({"success": False, "error": "Invalid move"}), 400

        from universalchess.services.game_broadcast import send_board_command

        sent = send_board_command("make_move", {"uci": uci})
        if sent:
            return jsonify({"success": True, "message": f"Played {uci}"})
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

    # The device timezone is owned by the OS clock, not centaur.ini: the ini key
    # is only a fallback cache, and get_timezone() reads the live OS zone
    # (/etc/timezone). Surface that here so the web Settings selector matches the
    # board clock instead of showing the stale ini/default value (which is "UTC"
    # on any device whose zone was set outside our UI, e.g. during imaging).
    from universalchess.services.timezone_service import get_timezone
    result.setdefault("system", {})["timezone"] = get_timezone()

    # Surface the device UI locale from the language service (which normalises an
    # empty/unsupported ini value to a real, renderable locale) so the web app
    # initialises react-i18next to the same language the board renders, rather
    # than trusting the raw ini value.
    from universalchess.services.language_service import get_language
    result.setdefault("system", {})["ui_language"] = get_language()

    # Coach settings: expose the non-secret selection (coach_id/coach_provider) and
    # the per-agent model/base_url (namespaced keys pass through from the ini), but
    # never the stored API keys. Each coach API key is redacted to a boolean
    # ``<key>_set`` companion so the settings page (which is not behind auth) cannot
    # leak a secret. The Agents tab reads per-agent config from /api/agents.
    if "game" in result:
        game = result["game"]
        game.setdefault("coach_id", "auto")
        game.setdefault("coach_provider", "none")
        for key in list(game.keys()):
            if _is_coach_api_key(key):
                game[f"{key}_set"] = bool(game.get(key))
                game[key] = ""

    _redact_account_sections(result)

    return result


def _redact_account_sections(result):
    """Blank secret fields in ``account:<type>:<id>`` sections of a settings dict.

    /api/settings is a broad, unauthenticated read; account sections carry the
    stored credential (e.g. a Lichess ``api_token``). Each field marked ``secret``
    in the catalog's account-type definition is replaced by an empty value plus a
    boolean ``<key>_set`` companion, mirroring how coach API keys are redacted, so
    the secret never leaves the server while the UI can still show "token set".
    Non-account sections and unknown account types are left untouched.
    """
    from universalchess.menus.catalog import get_catalog
    from universalchess.services import account_store

    catalog = get_catalog()
    for section in list(result.keys()):
        parsed = account_store.parse_section(section)
        if parsed is None:
            continue
        type_id, _ = parsed
        if not catalog.has_account_type(type_id):
            continue
        secret_keys = {f["key"] for f in catalog.account_type(type_id)["fields"] if f.get("secret")}
        for key in list(result[section].keys()):
            if key in secret_keys:
                result[section][f"{key}_set"] = bool(result[section].get(key))
                result[section][key] = ""


def _is_coach_api_key(key: str) -> bool:
    """True for the effective or any per-agent coach API-key storage key.

    Matches ``coach_api_key`` and ``coach_api_key_<agent>`` (but not the ``_set``
    boolean companions, which are not themselves secrets). Used to redact secrets
    from GET responses and to treat a blank key on save as "leave unchanged".
    """
    if key.endswith("_set"):
        return False
    return key == "coach_api_key" or key.startswith("coach_api_key_")


def _drop_stale_lichess_username(config, values):
    """Clear the cached Lichess username when the stored token is changing.

    The username (surfaced as the default human player-name placeholder) belongs
    to the account the previous token authenticated as; a changed token may be a
    different account, so a stale name must not linger. The board's
    ``set_lichess_api`` clears it for on-device edits; the web writes the
    ``lichess`` section directly, so this mirrors that behaviour. A save that does
    not include ``api_token`` leaves the username untouched.

    Returns a new mapping; the input is not mutated.
    """
    result = dict(values)
    new_token = result.get("api_token")
    if new_token is None:
        return result
    current_token = config.get("lichess", "api_token", fallback="")
    if new_token != current_token:
        result["username"] = ""
    return result


def _translate_game_coach_writes(config, values):
    """Route effective coach key/model/base_url writes to the active provider's slot.

    The settings UI sends a single ``coach_api_key``/``coach_model``/
    ``coach_base_url`` (the active provider's value). Storage is per provider, so
    these are rewritten to the namespaced key for the provider named in this save
    (falling back to the persisted provider), and the flat keys are dropped so they
    never shadow the namespaced layout. Other keys pass through unchanged.

    Returns a new mapping; the input is not mutated.
    """
    from universalchess.managers.game import coach_settings

    result = dict(values)
    provider = result.get("coach_provider")
    if provider is None:
        provider = config.get("game", "coach_provider", fallback="none")

    for base in ("coach_api_key", "coach_model", "coach_base_url"):
        if base not in result:
            continue
        value = result.pop(base)
        for namespaced, namespaced_value in coach_settings.writes_for_effective(
            provider, base, value
        ).items():
            result[namespaced] = namespaced_value
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
        if section == "lichess":
            values = _drop_stale_lichess_username(config, values)
        if section == "game":
            # The UI edits a single effective coach key/model/base_url; route those
            # to the active provider's namespaced slot so switching providers keeps
            # every provider's saved credentials (matching GameSettings.set).
            values = _translate_game_coach_writes(config, values)
            # A blank coach API key means "leave unchanged": the GET never returns
            # the stored secret, so a blank field on save must not wipe it. Drop the
            # UI-only ``_set`` boolean companions as well (they are not stored).
            values = {
                key: value
                for key, value in values.items()
                if not (key.startswith("coach_api_key") and key.endswith("_set"))
                and not (_is_coach_api_key(key) and (value is None or value == ""))
            }
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


def _read_coach_config():
    """Build a CoachConfig from centaur.ini (server-side, key never sent to client).

    Shared by the coach endpoints so they all resolve the same provider/key/model
    from settings; the API key stays on the server and is only used to call the
    provider.
    """
    from universalchess.board.settings import Settings
    from universalchess.managers.game import coach_settings
    from universalchess.services.coach import CoachConfig

    from universalchess.coaches import registry as coaches

    # Coach credentials are stored per provider (namespaced keys); resolve the
    # active provider's effective key/model/base_url the same way the board does so
    # web and board agree on which provider is configured.
    storage = {"coach_provider": Settings.read("game", "coach_provider", "none")}
    for key in coach_settings.per_provider_keys():
        storage[key] = Settings.read("game", key, "")
    effective = coach_settings.resolve_effective(storage)

    # The Coach selector is the master switch: "Disabled" (coach id ``off``) turns
    # coaching off no matter how well the agent is configured, so is_configured()
    # returns False and the coach endpoints refuse to call the provider.
    enabled = Settings.read("game", "coach_id", coaches.AUTO) != coaches.OFF

    return CoachConfig(
        provider=effective["coach_provider"],
        api_key=effective["coach_api_key"],
        model=effective["coach_model"],
        base_url=effective["coach_base_url"],
        enabled=enabled,
    )


def _read_agent_config(agent_id):
    """Build a CoachConfig for a specific agent from its namespaced ini slots.

    Used by the Agents tab endpoints (models listing) so a user can test/list
    models for any agent, not only the one currently powering coaching. The API key
    is read server-side and never returned to the client.
    """
    from universalchess.board.settings import Settings
    from universalchess.managers.game import coach_settings
    from universalchess.services.coach import CoachConfig

    def _slot(base):
        return Settings.read("game", coach_settings.namespaced_key(base, agent_id), "")

    return CoachConfig(
        provider=agent_id,
        api_key=_slot(coach_settings.API_KEY_BASE),
        model=_slot(coach_settings.MODEL_BASE),
        base_url=_slot(coach_settings.BASE_URL_BASE),
    )


def _read_notation():
    """Return the user's move notation from centaur.ini, normalized.

    Shared by the coach endpoints so the coach refers to a move in the same
    notation the board/web move list uses. Unknown/empty values fall back to the
    product default.
    """
    from universalchess.board.settings import Settings
    from universalchess.utils.chess_notation import DEFAULT_NOTATION, normalize_notation

    return normalize_notation(Settings.read("game", "notation", DEFAULT_NOTATION))


def _read_coach_language():
    """Return the coach's response language name from the device UI locale.

    Derived from the single device-wide [system] ui_language (mapped to a
    plain-English language name) rather than a separate coach-language setting, so
    the coach writes in whatever language the device is set to. English yields
    "English", which adds no prompt directive.

    Shared by the coach endpoints so web and board ask the AI to respond in the
    same language. English (the default) adds no prompt instruction.
    """
    from universalchess.services import language_service

    return language_service.coach_language_name(language_service.get_language())


def _read_player_dicts():
    """Read both players' type/color/elo from centaur.ini for coach selection.

    Returns two mappings shaped like the board's player settings dicts so the
    shared coach helpers (:func:`resolve_human_color`, :func:`resolve_opponent_elo`)
    resolve identically on web and board.
    """
    from universalchess.board.settings import Settings

    player1 = {
        "type": Settings.read("PlayerOne", "type", "human"),
        "color": Settings.read("PlayerOne", "color", "white"),
        "elo": Settings.read("PlayerOne", "elo", "Default"),
    }
    player2 = {
        "type": Settings.read("PlayerTwo", "type", "engine"),
        "color": Settings.read("PlayerTwo", "color", "black"),
        "elo": Settings.read("PlayerTwo", "elo", "Default"),
    }
    return player1, player2


def _read_coach_persona(side_to_move: str, *, is_potential_move: bool):
    """Resolve the coaching persona for a move, mirroring the board's selection.

    Selects the coach (explicit ``coach_id`` or Elo-matched Auto) and returns its
    persona for the move context. Returns None when no coach is available so the
    service falls back to its default voice.
    """
    from universalchess.board.settings import Settings
    from universalchess.coaches import registry as coaches

    player1, player2 = _read_player_dicts()
    coach_id = Settings.read("game", "coach_id", coaches.AUTO)
    return coaches.resolve_persona(
        coach_id,
        coaches.resolve_opponent_elo(player1, player2),
        human_color=coaches.resolve_human_color(player1, player2),
        is_potential_move=is_potential_move,
        side_to_move=side_to_move,
    )


def _read_move_is_opponent(side_to_move: str) -> bool:
    """Whether a played move on ``side_to_move`` is the opponent's, not the player's.

    Uses the same move-context rule as persona selection so the prompt framing and
    the persona always agree: an opponent's move must be explained as the opponent's
    (not addressed as the player's own move). With no single human (engine-vs-engine
    or two humans) a played move is treated as the opponent's, matching
    :func:`select_move_context`.
    """
    from universalchess.board.settings import Settings
    from universalchess.coaches import registry as coaches
    from universalchess.coaches.base import MoveContext

    player1, player2 = _read_player_dicts()
    human_color = coaches.resolve_human_color(player1, player2)
    context = coaches.select_move_context(False, side_to_move, human_color)
    return context is MoveContext.OPPONENT_MOVE


def _resolved_coach_id():
    """Return the resolved coach's id (for tip cache keys), or "" when none."""
    from universalchess.board.settings import Settings
    from universalchess.coaches import registry as coaches

    player1, player2 = _read_player_dicts()
    coach_id = Settings.read("game", "coach_id", coaches.AUTO)
    coach = coaches.resolve_coach(coach_id, coaches.resolve_opponent_elo(player1, player2))
    return coach.id if coach is not None else ""


@app.route("/api/agents", methods=["GET"])
def api_agents():
    """List every registered AI agent with its (non-secret) configuration.

    Powers the Agents tab: one entry per built-in and user agent, each carrying its
    display metadata (name/description), its configurable field schema, the stored
    model and (for agents that require one) base URL, ``api_key_set`` -- a boolean
    flag rather than the key itself, so the secret never leaves the server -- and
    ``configured``, true when the agent has its key plus every required setting (so
    the Game > Agent selector can offer configured agents only).
    ``selected`` echoes the agent currently powering coaching (game.coach_provider).

    Response: ``{"agents": [agent], "selected": str}``.
    """
    from universalchess.agents import registry as agents_reg
    from universalchess.board.settings import Settings
    from universalchess.managers.game import coach_settings

    def _slot(agent_id, base):
        return Settings.read("game", coach_settings.namespaced_key(base, agent_id), "")

    agents_out = []
    for info in agents_reg.list_agents():
        agent_id = info["id"]
        api_key_set = bool(_slot(agent_id, coach_settings.API_KEY_BASE))
        base_url = (
            _slot(agent_id, coach_settings.BASE_URL_BASE)
            if info.get("requires_base_url")
            else ""
        )
        # An agent is offerable in the Game > Agent selector only once it can power
        # the coach: an API key plus a base URL for agents that require one. The
        # frontend uses this to list configured agents only.
        configured = api_key_set and (not info.get("requires_base_url") or bool(base_url))
        agents_out.append({
            **info,
            "api_key_set": api_key_set,
            "configured": configured,
            "model": _slot(agent_id, coach_settings.MODEL_BASE),
            "base_url": base_url,
        })
    return jsonify({
        "agents": agents_out,
        "selected": Settings.read("game", "coach_provider", "none"),
    })


# Uses POST, not DELETE: the app's WebDAV before_request (handle_preflight)
# intercepts every DELETE app-wide and demands WebDAV auth, so REST routes cannot
# use that verb (see the engine/profile endpoints for the same reason).
@app.route("/api/agents/<agent_id>/clear-key", methods=["POST"])
@requires_auth
def api_clear_agent_key(agent_id):
    """Clear the stored API key for one agent.

    The GET never returns the stored secret and a blank key on save means "leave
    unchanged", so the normal save path cannot remove a key. This explicit endpoint
    clears the agent's namespaced API-key slot (leaving its model and base URL
    intact) so a mistyped or rotated key can be deleted. ``agent_id`` is validated
    against the registry so the route cannot be used to write an arbitrary settings
    key. Broadcasts the same settings-changed notifications as a save so the board
    and other clients reload and drop the now-unconfigured agent.

    Response: ``{"ok": true}``, or ``{"error": "unknown_agent"}`` (404).
    """
    from universalchess.agents import registry as agents_reg
    from universalchess.board.settings import Settings
    from universalchess.managers.game import coach_settings

    if agents_reg.get_agent(agent_id) is None:
        return jsonify({"error": "unknown_agent"}), 404

    key = coach_settings.namespaced_key(coach_settings.API_KEY_BASE, agent_id)
    Settings.write("game", key, "")

    broadcast_sse_event("settings_changed")
    try:
        from universalchess.services.game_broadcast import notify_main_process_settings_changed
        notify_main_process_settings_changed()
    except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
        pass

    return jsonify({"ok": True})


def _account_resolver(type_id):
    """Return the identity resolver for an account type, or None.

    A ``resolved`` identity type (Lichess) needs to authenticate the submitted
    credential to learn its canonical identity (the username the account is keyed
    on). This maps a type id to that network-bound callable; ``entered`` identity
    types need no resolver and return None. Isolated as one function so tests can
    inject a fake resolver instead of contacting Lichess.
    """
    if type_id == "lichess":
        from universalchess.services.lichess_service import resolve_lichess_identity

        return lambda fields: resolve_lichess_identity(fields.get("api_token", ""), log=None)
    return None


def _redact_account(account, account_type):
    """Shape a stored :class:`account_store.Account` for the API, hiding secrets.

    Secret fields (per the catalog definition) are reported only as booleans in
    ``secretsSet``; every other stored field is returned in ``values``. ``identity``
    echoes the account's identity-field value (e.g. the Lichess username) for
    display. The raw secret is never included.
    """
    secret_keys = {f["key"] for f in account_type.get("fields", []) if f.get("secret")}
    values = {}
    secrets_set = {}
    for key, value in account.values.items():
        if key in secret_keys:
            secrets_set[key] = bool(value)
        else:
            values[key] = value
    return {
        "type": account.type,
        "id": account.id,
        "identity": account.get(account_type["identityField"], account.id),
        "values": values,
        "secretsSet": secrets_set,
    }


# HTTP status for each add-account failure code. Absent codes map to 400.
_ADD_ACCOUNT_STATUS = {"duplicate": 409, "unknown_type": 400}


@app.route("/api/accounts", methods=["GET"])
@requires_auth
def api_list_accounts():
    """List saved online accounts, with secrets redacted. Requires auth.

    Powers the Accounts page's list. Each entry carries its type, normalized id,
    display identity, non-secret values, and a ``secretsSet`` map; tokens are
    never returned. Response: ``{"accounts": [account]}``.
    """
    from universalchess.menus.catalog import get_catalog
    from universalchess.services import account_store

    # Promote a legacy single [lichess] credential into an account on first read
    # (idempotent). resolver=None keeps this off the network: it migrates only
    # when the username is already cached, which is the common case after any
    # prior successful Lichess authentication.
    account_store.ensure_lichess_migrated(resolver=None)

    catalog = get_catalog()
    accounts = []
    for account in account_store.list_accounts():
        if not catalog.has_account_type(account.type):
            continue
        accounts.append(_redact_account(account, catalog.account_type(account.type)))
    return jsonify({"accounts": accounts})


@app.route("/api/accounts", methods=["POST"])
@requires_auth
def api_add_account():
    """Add an online account from submitted fields. Requires auth.

    Body: ``{"type": "<account type id>", "fields": {<key>: <value>}}``. The type
    must be declared in the catalog. For a resolved-identity type the credential
    is authenticated to derive the account's unique identity; duplicates (same
    player name) are rejected. Returns the redacted account (201) or an error code
    with an appropriate status (400 validation/auth, 409 duplicate). Broadcasts a
    settings change so the board and other clients reload.
    """
    from universalchess.menus.catalog import get_catalog
    from universalchess.services import account_store

    body = request.get_json(silent=True) or {}
    type_id = body.get("type")
    fields = body.get("fields") or {}
    if not isinstance(fields, dict):
        return jsonify({"error": "invalid_fields"}), 400

    catalog = get_catalog()
    if not type_id or not catalog.has_account_type(type_id):
        return jsonify({"error": "unknown_type"}), 400
    account_type = catalog.account_type(type_id)

    result = account_store.add_account(
        account_type, fields, resolver=_account_resolver(type_id)
    )
    if result.error:
        status = _ADD_ACCOUNT_STATUS.get(result.error, 400)
        return jsonify({"error": result.error, "message": result.message}), status

    _broadcast_settings_changed()
    return jsonify({"account": _redact_account(result.account, account_type)}), 201


# POST (not DELETE): the app-wide WebDAV before_request intercepts DELETE, so REST
# delete routes use POST (see api_clear_agent_key for the same constraint).
@app.route("/api/accounts/<type_id>/<account_id>/delete", methods=["POST"])
@requires_auth
def api_delete_account(type_id, account_id):
    """Delete a saved account. Requires auth.

    Returns ``{"ok": true}`` when removed, or 404 when no such account exists.
    Broadcasts a settings change so any player bound to it can be reconciled.
    """
    from universalchess.services import account_store

    if account_store.delete_account(type_id, account_id):
        _broadcast_settings_changed()
        return jsonify({"ok": True})
    return jsonify({"error": "not_found"}), 404


def _broadcast_settings_changed():
    """Notify web clients and the board that persisted settings changed.

    Mirrors the notification a normal settings save emits so the board reloads
    account state and other browser tabs refresh. Best-effort: a failed
    cross-process notify must not fail the request.
    """
    broadcast_sse_event("settings_changed")
    try:
        from universalchess.services.game_broadcast import notify_main_process_settings_changed
        notify_main_process_settings_changed()
    except Exception:  # noqa: S110  # nosec B110 - best-effort; failure here is non-fatal and intentionally ignored
        pass


@app.route("/api/coaches", methods=["GET"])
def api_coaches():
    """List selectable coaches and the coach resolved for the current game.

    Powers the coach card's selector: ``coaches`` is the full roster (built-in +
    user, weakest-first) for the dropdown, ``selected`` is the persisted
    ``coach_id`` (``"auto"`` by default), and ``resolved`` is the coach that
    ``coach_id`` currently maps to given the opponent's Elo -- so the UI can show
    which coach Auto picked.

    Response: ``{"coaches": [info], "selected": str, "resolved": info | null}``.
    """
    from universalchess.board.settings import Settings
    from universalchess.coaches import registry as coaches

    player1, player2 = _read_player_dicts()
    selected = Settings.read("game", "coach_id", coaches.AUTO)
    resolved = coaches.resolve_coach_info(
        selected, coaches.resolve_opponent_elo(player1, player2)
    )
    return jsonify({
        "coaches": coaches.list_coaches(),
        "selected": selected,
        "resolved": resolved,
    })


@app.route("/api/coach/models", methods=["GET"])
def api_coach_models():
    """List available AI models for an agent (query ``agent``) or the active one.

    With ``?agent=<id>`` the models are listed for that specific agent (the Agents
    tab uses this to populate each agent's Model dropdown from its own key); without
    it, the agent currently powering coaching is used. Reads the key/base URL from
    centaur.ini server-side (the API key is never returned to the client) and
    queries the agent's list-models endpoint so the dropdown reflects the account's
    real, currently available models. On any failure (not configured, network, bad
    key) returns the curated fallback plus an ``error`` note so the dropdown still
    renders rather than breaking the settings page.

    Response: ``{"provider": str, "models": [str], "error": str | null}``.
    """
    from universalchess.services.coach import (
        CoachError,
        fallback_models,
        list_models,
    )

    agent_id = request.args.get("agent")
    config = _read_agent_config(agent_id) if agent_id else _read_coach_config()
    provider = config.provider

    if not config.is_configured():
        return jsonify({
            "provider": provider,
            "models": fallback_models(provider),
            "error": "not_configured",
        })

    try:
        models = list_models(config)
        return jsonify({"provider": provider, "models": models, "error": None})
    except CoachError as exc:
        # Log the detail server-side; return a fixed token so the response never
        # leaks internal error text (paths, URLs, library internals) to the client.
        app.logger.info(f"Coach model list failed for {provider}: {exc}")
        return jsonify({
            "provider": provider,
            "models": fallback_models(provider),
            "error": "unavailable",
        })


@app.route("/api/coach/statement/<int:gameid>/<int:ply>", methods=["GET"])
def api_coach_statement(gameid, ply):
    """Return the AI coach statement for a played ply, generating it if absent.

    Mirrors the board's per-ply coach flow for the web live-board and analysis
    views: a stored statement is returned instantly (and marked ``cached``);
    otherwise the move's coaching prompt is reconstructed from the stored rows
    (position before + move + eval swing), generated via the configured provider,
    persisted onto the ply, and returned so the same move is never billed twice.

    Response: ``{"statement": str | null, "cached": bool, "error": str | null}``.
    ``error`` is ``"not_configured"`` when no provider/key is set (the UI then
    hides the panel), ``"out_of_range"`` for an unknown ply, or ``"unavailable"``
    on a generation failure. A generation failure also carries ``reason`` (a safe
    category: ``quota``/``auth``/``rate_limited``/``unavailable``) and a user-facing
    ``message``, so the UI can explain a billing/key problem instead of offering a
    futile retry. The raw provider error is logged server-side, never returned.
    """
    from universalchess.managers.game.coach_persistence import (
        get_coach_statement,
        get_game_chess960,
        get_move_context,
        get_move_evals,
        save_coach_statement_if_absent,
    )
    from universalchess.managers.game.coach_generation import generate_validated_statement
    from universalchess.managers.game.coach_request_builder import build_coach_request
    from universalchess.services.coach import (
        CoachError,
        error_category,
        error_message,
    )

    stored = get_coach_statement(gameid, ply)
    if stored:
        return jsonify({"statement": stored, "cached": True, "error": None})

    config = _read_coach_config()
    if not config.is_configured():
        return jsonify({"statement": None, "cached": False, "error": "not_configured"})

    context = get_move_context(gameid, ply)
    if context is None:
        return jsonify({"statement": None, "cached": False, "error": "out_of_range"}), 404

    fen_before, move_uci = context
    eval_before_cp, eval_after_cp = get_move_evals(gameid, ply)
    chess960 = get_game_chess960(gameid)
    import chess

    side_to_move = "white" if chess.Board(fen_before).turn == chess.WHITE else "black"
    coach_request = build_coach_request(
        fen_before,
        move_uci,
        notation=_read_notation(),
        eval_before_cp=eval_before_cp,
        eval_after_cp=eval_after_cp,
        is_opponent_move=_read_move_is_opponent(side_to_move),
        persona=_read_coach_persona(side_to_move, is_potential_move=False),
        language=_read_coach_language(),
        chess960=chess960,
    )
    if coach_request is None:
        return jsonify({"statement": None, "cached": False, "error": "bad_move"}), 422

    try:
        statement = generate_validated_statement(config, coach_request)
    except CoachError as exc:
        # Log the full detail server-side. To the client, return the failure
        # *category* and a safe, user-facing sentence (never the raw provider text):
        # a quota/billing or key problem is permanent, so the UI must explain it
        # rather than offer a futile retry. ``error`` stays "unavailable" for
        # backward compatibility; ``reason``/``message`` carry the specifics.
        app.logger.info(f"Coach statement failed for game {gameid} ply {ply}: {exc}")
        # error_category returns a value from a closed set (quota/auth/rate_limited/
        # unavailable) and error_message returns a fixed sentence from a constant
        # table keyed by that category. Neither includes the exception's text (only
        # logged above), so this is not stack-trace exposure; suppress the taint
        # heuristic that assumes any exc-derived value returned is a leak.
        return jsonify({  # nosemgrep: semgrep.flask-stack-trace-exposure,semgrep.exception-text-returned
            "statement": None,
            "cached": False,
            "error": "unavailable",
            "reason": error_category(exc),
            "message": error_message(exc),
        }), 502

    # First-writer-wins: if the board (or a concurrent request) already stored a
    # statement for this move, adopt it so board and web show identical text rather
    # than two independent generations. Fall back to ours if nothing was stored.
    canonical = save_coach_statement_if_absent(gameid, ply, statement)
    return jsonify(
        {"statement": canonical or statement, "cached": False, "error": None}
    )


@app.route("/api/coach/tip", methods=["POST"])
def api_coach_tip():
    """Return a coaching remark for a *hinted* move (a tip), cached in memory.

    Body: ``{"fen": str, "move": str}`` where ``move`` is the recommended move in
    UCI. Repeating the same tip (same position + move) returns the in-memory
    statement without re-billing the AI; a new tip is generated. Tips are not
    persisted (the recommendation is not a stored ply).

    Response: ``{"statement": str | null, "error": str | null}``. ``error`` is
    ``"not_configured"`` when no provider/key is set, or ``"unavailable"`` when
    the move can't be coached or the AI call failed.
    """
    from universalchess.managers.game import coach_tips

    body = request.get_json(silent=True) or {}
    fen = (body.get("fen") or "").strip()
    move_uci = (body.get("move") or "").strip()
    chess960 = bool(body.get("chess960", False))
    if not fen or not move_uci:
        return jsonify({"statement": None, "error": "missing_fen_or_move"}), 400

    config = _read_coach_config()
    if not config.is_configured():
        return jsonify({"statement": None, "error": "not_configured"})

    # A tip is a move the player is considering, so it uses the player-move persona.
    # side_to_move is irrelevant for a hint but resolves cleanly from the FEN.
    import chess

    try:
        side_to_move = "white" if chess.Board(fen).turn == chess.WHITE else "black"
    except ValueError:
        return jsonify({"statement": None, "error": "unavailable"})
    persona = _read_coach_persona(side_to_move, is_potential_move=True)

    statement = coach_tips.get_tip_statement(
        config,
        fen,
        move_uci,
        notation=_read_notation(),
        persona=persona,
        persona_key=_resolved_coach_id(),
        language=_read_coach_language(),
        chess960=chess960,
    )
    if statement is None:
        return jsonify({"statement": None, "error": "unavailable"})
    return jsonify({"statement": statement, "error": None})


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

    The Time Control preset list is not authored in menu.json: the board fills it
    from the Python preset registry at runtime (a provider), so the web dropdown
    must too. The generated ``time_control_presets`` option set is injected here
    -- into a shallow copy so the shared cached catalog is not mutated -- keeping
    the registry the single source of truth for both platforms without a second
    fetch.

    The full IANA ``timezones`` list is injected the same way: the board menu uses
    a small curated set (``timezones_common``), but the web selector offers every
    zone, sourced at runtime from the stdlib so it stays current with the OS.
    """
    try:
        from universalchess.menus.catalog import get_localized_catalog
        from universalchess.menus.time_control_presets import preset_options
        from universalchess.services.language_service import get_language
        from universalchess.services.timezone_service import list_timezones

        # Serve the catalog localized to the device UI language so the web
        # Settings fields (labels, help, option labels) match the board. Read the
        # locale fresh here rather than via the board's cached active locale so a
        # language change is reflected on the next Settings load in this process.
        menu = dict(get_localized_catalog(get_language()).raw_menu())
        option_sets = dict(menu.get("optionSets", {}))
        # The identical list the board renders: a leading Basic entry (empty key
        # -> no preset -> the base-minutes control) and a trailing Custom entry
        # bracket the registered presets.
        option_sets["time_control_presets"] = preset_options()
        # Full IANA zone list for the web timezone selector (field.system.timezone
        # references it by the provider name "timezones").
        option_sets["timezones"] = [{"value": tz, "label": tz} for tz in list_timezones()]
        menu["optionSets"] = option_sets
        return jsonify(menu)
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


@app.route("/api/positions", methods=["POST"])
@requires_auth
def api_add_position():
    """Persist a user-entered position to the [custom] category. Requires auth.

    Writes to the custom overlay file (positions.custom.ini), which
    load_positions_config merges over the packaged defaults, so the saved
    position appears in the same catalog the board reads and can be set up like
    any other. Validation failures (bad FEN, empty name, illegal hint) return
    400 with a user-safe message; nothing is written on failure.

    Body: {"name": str, "fen": str, "hint"?: str}
    """
    try:
        from universalchess.utils.positions import (
            CUSTOM_POSITION_ERRORS,
            add_custom_position,
            validate_custom_position,
        )

        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        fen = (body.get("fen") or "").strip()
        hint = (body.get("hint") or "").strip() or None

        # Validate up front and answer from a constant message keyed by the
        # returned code, so no exception text flows into the response (CWE-209).
        error_code = validate_custom_position(name, fen, hint)
        if error_code is not None:
            return jsonify({"success": False, "error": CUSTOM_POSITION_ERRORS[error_code]}), 400

        key = add_custom_position(name, fen, hint, log=app.logger)
        return jsonify({"success": True, "name": key})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/board/setup-position", methods=["POST"])
@requires_auth
def api_board_setup_position():
    """Set up a predefined position on the board. Requires authentication.

    Validates the FEN, then asks the main process to abort any running game and
    set up the position. The web UI is responsible for confirming with the user
    when a game is in progress before calling this (the board records the
    interrupted game as abandoned, result = "*").

    ``record`` opts the resulting game into the normal database history (used by
    "Play Game from here" on the review page, where the user plays a real game
    from a reviewed position). It defaults to False so predefined-position setups
    stay practice games that are not recorded.

    ``moves`` carries the reviewed game's history (UCI, from ``start_fen`` up to
    the viewed ply) so a recorded "Play Game from here" continues with the full
    PGN instead of starting cold from ``fen``. When present with ``record``, the
    board persists a fresh in-progress game seeded with that history and resumes
    it; each move's legality is re-checked board-side. ``start_fen`` (defaulting
    to ``fen``) and ``chess960`` describe the board the history replays on;
    ``white``/``black`` name the transferred players. At the opening ply
    (``moves`` empty) this stays the plain ``fen`` setup.

    Body: {"fen": str, "name"?: str, "hint"?: str, "record"?: bool,
           "moves"?: [str], "start_fen"?: str, "chess960"?: bool,
           "white"?: str, "black"?: str}
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
        params = {"fen": fen, "name": name, "record": bool(body.get("record"))}
        if hint:
            params["hint"] = hint

        # Transferred history for "Play Game from here". Accept only a list of
        # UCI-shaped strings; the board re-validates each move's legality before
        # persisting, so this is a shape guard, not full validation.
        moves = body.get("moves")
        if moves is not None:
            if not isinstance(moves, list) or not all(
                isinstance(m, str) and 4 <= len(m) <= 5 for m in moves
            ):
                return jsonify({"success": False, "error": "Invalid moves"}), 400
            if moves:
                start_fen = (body.get("start_fen") or fen).strip()
                try:
                    chess.Board(start_fen, chess960=bool(body.get("chess960")))
                except ValueError:
                    return jsonify(
                        {"success": False, "error": "Invalid start_fen"}
                    ), 400
                params["moves"] = moves
                params["start_fen"] = start_fen
                params["chess960"] = bool(body.get("chess960"))
                white = body.get("white")
                black = body.get("black")
                if isinstance(white, str):
                    params["white"] = white
                if isinstance(black, str):
                    params["black"] = black

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


@app.route("/api/board/new-game", methods=["POST"])
@requires_auth
def api_board_new_game():
    """Start a fresh game on the board. Requires authentication.

    Asks the main process to record any in-progress game as abandoned
    (result = "*") and start a new game with the current player settings -- the
    same outcome as "New Game" in the on-board players menu. The web UI confirms
    with the user before calling this when a game is in progress.
    """
    try:
        from universalchess.services.game_broadcast import send_board_command

        sent = send_board_command("new_game")
        if sent:
            return jsonify({"success": True, "message": "New game started"})
        return jsonify({"success": False, "error": "Board not running"}), 503
    except Exception as e:
        return _internal_error(e)


@app.route("/api/games/<int:game_id>/resume", methods=["POST"])
@requires_auth
def api_game_resume(game_id):
    """Resume a stored game on the board by its id. Requires authentication.

    Asks the main process to load the given game (an abandoned "*" game or an
    in-progress NULL-result game) back onto the live board so play can continue.
    Any game currently running is first recorded as abandoned (result = "*") --
    it stays resumable itself, so nothing is lost. Finished games are rejected by
    the main process (they are review-only). The web UI confirms with the user
    before calling this when a game may be in progress.
    """
    try:
        from universalchess.services.game_broadcast import send_board_command

        sent = send_board_command("resume_game", {"game_id": game_id})
        if sent:
            return jsonify({"success": True, "message": "Game resume requested"})
        return jsonify({"success": False, "error": "Board not running"}), 503
    except Exception as e:
        return _internal_error(e)


@app.route("/api/games/<int:game_id>/analyze", methods=["POST"])
@requires_auth
def api_game_analyze(game_id):
    """Ask the board to evaluate a stored game's unanalysed plies.

    Backs the review page's gap-fill action. A game played with ``analysis_mode``
    off -- or recorded before evaluations were persisted -- has no eval chart and
    no best-move arrows; this fills them in.

    The work is done by the main process, never here: it owns the engine and the
    analysis queue, and the pooled UCI process cannot be driven from two
    processes at once on a board with 415 MiB of RAM. Results are written to the
    move rows and pushed to this page as ``position_analysed`` SSE events, so the
    chart fills in progressively; nothing is returned here but the hand-off
    status.

    404 when the game does not exist, so a stray id costs the board nothing.
    """
    try:
        session = get_db_session()
        try:
            exists = session.query(models.Game.id).filter(
                models.Game.id == game_id).first()
        finally:
            session.close()
        if exists is None:
            return jsonify({"success": False, "error": "not_found"}), 404

        from universalchess.services import game_broadcast

        sent = game_broadcast.send_board_command("analyze_game", {"game_id": game_id})
        if sent:
            return jsonify({"success": True, "message": "Analysis requested"})
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

    Serves ~/debug.log (appended and size-rotated by board.logging, so it
    spans restarts). Auth-gated because a full debug log can contain
    diagnostic detail about the system.
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


@app.route("/api/game/clock", methods=["GET"])
def api_game_clock():
    """Return the latest live clock snapshot for the LiveBoard countdown.

    Read-only and unauthenticated like the other GET probes. The clock counts
    down in the main process, which broadcasts every tick/state change over the
    game socket; the web subscriber caches the latest snapshot. Live updates
    reach the browser over SSE -- this endpoint just seeds the clock on load, and
    the browser interpolates the active side between events. When nothing is
    cached yet (fresh web start, before the board re-broadcasts), ask the board
    to re-broadcast so the next SSE push fills it, and return the untimed/unknown
    contract for now.

    Contract: {white_time: int|null, black_time: int|null,
    active_color: 'white'|'black'|null, is_running: bool, is_paused: bool,
    timed_mode: bool, synced_at: float|null}.
    """
    try:
        from universalchess.services.game_broadcast import (
            get_subscriber,
            request_clock_status_broadcast,
        )

        cached = get_subscriber().get_last_clock_status()
        if cached is None:
            request_clock_status_broadcast()
            return jsonify(
                {
                    "white_time": None,
                    "black_time": None,
                    "active_color": None,
                    "is_running": False,
                    "is_paused": False,
                    "timed_mode": False,
                    "synced_at": None,
                }
            )
        return jsonify(
            {
                "white_time": cached.get("white_time"),
                "black_time": cached.get("black_time"),
                "active_color": cached.get("active_color"),
                "is_running": bool(cached.get("is_running", False)),
                "is_paused": bool(cached.get("is_paused", False)),
                "timed_mode": bool(cached.get("timed_mode", False)),
                "synced_at": cached.get("synced_at"),
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


def _resolve_centaur_engine_options(engine_name, level):
    """Resolve a strength ``level`` to the UCI options the proxy should inject.

    The level is a section name in the engine's ``.uci`` (e.g. ``"1500 ELO"``,
    ``"Default"``) -- the same value a player's ``elo`` stores. Its section-local
    values are the setoptions that select that strength. Resolving server-side
    (rather than trusting a client-sent options map) keeps the injected options
    constrained to the engine's own profiles.

    Returns ``{}`` -- meaning "run at the engine's own default strength" -- when
    the level is empty or the engine cannot be probed/seeded, so a not-yet-probed
    engine still saves cleanly instead of failing.
    """
    if not level:
        return {}
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return {}
    try:
        uci_schema.seed_config(engine_name, config_path=config_path)
    except uci_schema.EngineProbeError:  # noqa: S110 - not-yet-probeable engine falls back to defaults, not fatal
        # Not installed/probeable yet: fall back to the engine default (no
        # options) rather than failing the save.
        pass
    profiles = engine_profiles.read_profiles(config_path)
    return next((p["values"] for p in profiles if p["name"] == level), {})


@app.route("/api/system/centaur-engine", methods=["GET"])
def api_get_centaur_engine():
    """Report the engine and strength level the Centaur proxy will use.

    Read-only and unauthenticated like the other GET probes. The Original Centaur
    tab uses this to populate the engine selector and pre-select the strength
    level. direct_mode aside, this only affects translate-mode play, where
    Centaur's engine path is the UC proxy. ``options`` is the resolved UCI map
    (parsed from the stored JSON) the proxy injects, returned for reference.
    """
    try:
        from universalchess.board.settings import Settings
        from universalchess.services.centaur_engine_proxy.config import (
            CONFIG_SECTION,
            DEFAULT_LEVEL,
            LEVEL_KEY,
            load_proxy_config,
        )

        config = load_proxy_config(Settings.read)
        level = str(Settings.read(CONFIG_SECTION, LEVEL_KEY, DEFAULT_LEVEL)) or DEFAULT_LEVEL
        return jsonify(
            {"engine": config.engine_name, "level": level, "options": config.options}
        )
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/centaur-engine", methods=["POST"])
@requires_auth
def api_set_centaur_engine():
    """Set the engine and strength level the Centaur proxy uses. Requires auth.

    Body: {"engine": str, "level": str}. The level is resolved server-side to the
    engine's profile options (see :func:`_resolve_centaur_engine_options`) and
    persisted -- the level for re-selection and the resolved options as a JSON
    string the proxy reads at its next launch. The proxy still clamps Hash/MultiPV
    to the memory floor, so nothing here can push the board into an OOM.
    """
    try:
        from universalchess.services.centaur_engine_proxy.config import (
            DEFAULT_ENGINE,
            DEFAULT_LEVEL,
            ENGINE_KEY,
            LEVEL_KEY,
            OPTIONS_KEY,
            CONFIG_SECTION,
        )

        body = request.get_json(silent=True) or {}
        engine = str(body.get("engine") or DEFAULT_ENGINE).strip() or DEFAULT_ENGINE
        level = body.get("level", DEFAULT_LEVEL)
        if not isinstance(level, str):
            return jsonify({"success": False, "error": "level must be a string"}), 400
        level = level.strip() or DEFAULT_LEVEL
        options = _resolve_centaur_engine_options(engine, level)
        save_all_settings({
            CONFIG_SECTION: {
                ENGINE_KEY: engine,
                LEVEL_KEY: level,
                OPTIONS_KEY: json.dumps(options),
            },
        })
        return jsonify({"success": True, "engine": engine, "level": level, "options": options})
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
        from universalchess.services.power import RESTART_UNIVERSAL_CHESS_CMD
        subprocess.run(["pkill", "-x", "centaur"], check=False, timeout=5)  # noqa: S607  # nosec B603 B607
        time.sleep(1)
        # Shared restart command so this web path and the two on-board handoffs
        # cannot drift (the drift that left the board dead: board used `stop`).
        subprocess.run(RESTART_UNIVERSAL_CHESS_CMD, check=False, timeout=30)  # noqa: S603 S607  # nosec B603 B607
        return jsonify({"success": True})
    except Exception as e:
        return _internal_error(e)


def _resolve_centaur_import_script(filename):
    """Locate a Centaur SD image-generator script to offer for download.

    Prefers the packaged copy under /opt/universalchess/tools (installed by the
    build) and falls back to the repo tools/ dir for development. ``filename`` is
    one of the allow-listed script names (never user-controlled path input), so
    this cannot be used to read arbitrary files. Returns the path, or None if
    neither location has it.
    """
    from universalchess.paths import BASE_DIR

    bases = [
        os.path.join(BASE_DIR, "tools", "centaur-import"),
        str(pathlib.Path(__file__).resolve().parents[3] / "tools" / "centaur-import"),
    ]
    for base in bases:
        # safe_under_base resolves filename under base and enforces containment,
        # returning None on any escape. filename comes from a fixed allow-list, so
        # this is defense in depth (and keeps the path provably contained).
        candidate = safe_under_base(base, filename)
        if candidate is not None and os.path.isfile(candidate):
            return candidate
    return None


@app.route("/api/system/centaur-import-script", methods=["GET"])
def api_system_centaur_import_script():
    """Serve a make-centaur-image helper as a download.

    The user runs this on the computer holding the original SD card to produce
    the uploadable image. ``?platform=unix`` (default, the shell script) or
    ``?platform=windows`` (the PowerShell script) selects which. The OS reads the
    raw ext4 partition differently (dd on macOS/Linux, raw PhysicalDrive on
    Windows) but both emit the same centaur-sd.img.gz. Read-only and
    unauthenticated like the other GET helpers: these are fixed, secret-free
    scripts. Served as an attachment so the browser saves rather than renders.

    The platform is resolved to a script name with explicit branches assigning
    constant literals (not a dict lookup keyed by the request value), so no
    request-derived data flows into the served path.
    """
    platform = request.args.get("platform", "unix")
    if platform == "windows":
        filename = "make-centaur-image.ps1"
        mimetype = "text/plain"
    elif platform == "unix":
        filename = "make-centaur-image.sh"
        mimetype = "text/x-shellscript"
    else:
        return jsonify({"success": False, "error": "Unknown platform."}), 400
    script_path = _resolve_centaur_import_script(filename)
    if script_path is None:
        return jsonify({"success": False, "error": "Import script not found."}), 404
    return send_file(
        script_path,
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename,
    )


def _run_centaur_import(image_path):
    """Background worker: install the Centaur app from the saved image.

    Runs on a daemon thread so the upload POST can return immediately and the UI
    can poll ``/api/system/centaur-import/status`` for live progress -- the whole
    reason this is async is that the post-upload work (decompress/mount/copy plus,
    on a 64-bit host, an armhf ``apt`` install) can take minutes, during which the
    old synchronous flow left the bar frozen at 100%. Each stage is forwarded into
    the shared import store via the callback; the terminal result is recorded with
    ``finish``. The uploaded ~200 MB image is always deleted afterwards.

    The CentaurImportError message is author-written and path-free, so it is safe
    to store for the client; an unexpected error is logged server-side and
    reported generically (no exception text leaks to the UI).
    """
    from universalchess.services.centaur_import import (
        CentaurImportError,
        install_from_image,
    )

    try:
        install_from_image(
            image_path,
            stage_callback=lambda stage, message: _centaur_import_store.update(stage, message),
        )
        _centaur_import_store.finish(success=True)
    except CentaurImportError as e:
        _centaur_import_store.finish(success=False, error=str(e))
    except Exception as e:
        app.logger.exception("Centaur import failed: %s", e)
        _centaur_import_store.finish(success=False, error="Import failed")
    finally:
        try:
            if os.path.exists(str(image_path)):
                os.remove(str(image_path))
        except OSError:  # noqa: S110  # nosec B110 - best-effort tmp cleanup; failure is non-fatal
            pass


def _start_centaur_import(image_path):
    """Initialize the persisted import state and spawn the install thread.

    Caller is responsible for validating the upload and that no import is already
    active (mirrors _start_engine_install).
    """
    _centaur_import_store.start()
    thread = threading.Thread(target=_run_centaur_import, args=(image_path,), daemon=True)
    thread.start()


@app.route("/api/system/import-centaur", methods=["POST"])
@requires_auth
def api_system_import_centaur():
    """Start installing the original Centaur software from an uploaded SD image.

    Accepts a multipart upload (field ``image``) of the gzip ext4 image produced
    by tools/centaur-import/make-centaur-image.sh. The file is streamed to the
    service tmp dir (the dir the mount helper allow-lists), then the import runs on
    a background thread (decompress -> loop-mount read-only -> extract to the
    managed CENTAUR_HOME with debug cruft stripped -> validate -> provision armhf
    support on 64-bit -> hook the engine proxy). The endpoint returns 202
    immediately; the client polls ``/api/system/centaur-import/status`` for stage,
    percent, and the terminal result. On success the Original Centaur
    Switch/Return controls become available.

    Returns 400 when the upload is missing/misnamed, 409 when an import is already
    running, and 202 once the background install has started. Failures inside the
    install (e.g. a missing-files image) surface through the status endpoint's
    result, not this response.
    """
    from universalchess.paths import TMP_DIR

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

    if _centaur_import_store.status_dict()["active"]:
        return jsonify({"success": False, "error": "A Centaur import is already in progress."}), 409

    os.makedirs(TMP_DIR, exist_ok=True)
    # Contain the save path inside TMP_DIR; this is also the dir the mount helper
    # restricts images to, so a path that escapes it would be refused downstream.
    target = safe_under_base(TMP_DIR, safe)
    if target is None:
        return jsonify({"success": False, "error": "Invalid image filename."}), 400

    try:
        # FileStorage.save streams the body to disk in chunks rather than holding
        # the ~200 MB image in memory. The background worker owns cleanup of the
        # saved file (it must outlive this request).
        upload.save(str(target))
    except OSError as e:
        return _internal_error(e)

    _start_centaur_import(target)
    return jsonify({"success": True, "status": "started"}), 202


@app.route("/api/system/centaur-import/status", methods=["GET"])
def api_system_centaur_import_status():
    """Get current Centaur SD-import progress.

    Returns the structured state (stage, message, derived percent, active,
    interrupted, result). Percent is computed at read time so the long armhf-apt
    stage creeps between polls without the backend ticking. Unauthenticated like
    the engine status poll -- it exposes only progress, no control.
    """
    return jsonify(_centaur_import_store.status_dict())


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
    For code-drawn sheets (SPLIT and COLORWAY), which store pieces separately
    from the board, a small board/piece preview is composed in code so the
    selector shows the composited result rather than the raw ink/mask/alpha
    data. The sheet name is validated against the discovered set, so it cannot
    be used for path traversal.
    """
    try:
        from universalchess.resources import ResourceLoader
        from universalchess.paths import RESOURCES_DIR, USER_RESOURCES_DIR
        from universalchess.epaper.chess_board import (
            detect_sheet_layout, image_has_alpha, compose_preview_strip,
        )

        loader = ResourceLoader(RESOURCES_DIR, USER_RESOURCES_DIR)
        if sheet not in loader.list_chess_sprite_sheets():
            abort(404)

        # get_chess_sprites resolves the .bmp/.png file and returns the renderer-
        # ready image (1-bit for LEGACY/SPLIT, RGBA for COLORWAY).
        img = loader.get_chess_sprites(sheet)
        if img is None:
            abort(404)

        layout = detect_sheet_layout(img.width, img.height, has_alpha=image_has_alpha(img))
        if layout.draws_squares:
            # Code-drawn layouts have no baked squares: compose a 12-col x 2-row
            # preview (each piece on a light row 0 and a dithered dark row 1) so
            # the selector shows what the board actually composites.
            img = compose_preview_strip(img, layout)

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
            engines_list.append({
                "name": name,
                "display_name": engine_def.display_name,
                "installed": engine_manager.is_available(name)
            })
        
        return jsonify(engines_list)
    except Exception as e:
        # Fallback if engine manager not available
        return jsonify([{"name": "stockfish", "display_name": "Stockfish", "installed": True}])


# Engine option profiles are editable for every installed engine. The schema is
# discovered by probing the binary's UCI options (services.uci_schema); the
# writable config/engines/<name>.uci -- generated on first use, never shipped --
# holds the selectable sections the engine player loads at game start. Editing
# reads/writes that file (the [DEFAULT] section is preserved), so edits take
# effect on the next game.
from universalchess.services import engine_bootstrap, engine_failure_record, engine_profiles, uci_schema
from universalchess.services.engine_registry import (
    LOAD_FAILURE_BINARY_MISSING,
    sanitize_detail,
    sanitize_reason_code,
)

# Durable per-engine record of the last install/initialize failure. Module-level
# like the install-state store so tests can swap in a temp-backed instance.
_engine_failure_store = engine_failure_record.STORE


def _failure_payload(engine_name):
    """Serialize an engine's last failure for the management card, or None.

    Only fixed tokens and a timestamp cross this boundary. This endpoint is not
    auth-gated, and the exception text the reason was derived from carries the
    engine's absolute path; the fuller message goes to the auth-gated event log.
    The record is re-checked against the published vocabulary on the way out
    rather than trusted for having been checked on the way in, since it is read
    back from a file that outlives the process that wrote it.
    """
    failure = _engine_failure_store.get(engine_name)
    if failure is None:
        return None
    return {
        "phase": failure.phase,
        "reason_code": sanitize_reason_code(failure.reason_code),
        "detail": sanitize_detail(failure.detail),
        "failed_at": failure.failed_at,
        "dismissed": failure.dismissed,
    }


def _config_uci_path(engine_name):
    """Resolve the writable config ``.uci`` path for an engine.

    Guards the engine name against path traversal via ``safe_under_base``;
    returns ``None`` if the name is empty or escapes the engines config dir.
    """
    return safe_under_base(os.path.join(CONFIG_DIR, "engines"), f"{engine_name}.uci")


def _seed_and_probe_schema(engine_name, config_path):
    """Seed the writable config (if absent) and probe the engine's schema.

    Deliberately uses ``seed_config`` (create-if-absent), NOT ``reconcile_config``:
    opening the editor is a read, not a net-change event, and an add-only
    reconcile here would re-add sections the user intentionally deleted (fighting
    the user). Reconciliation with the on-disk net set happens only when the nets
    actually change -- after a repair/top-up (see ``_run_engine_repair``).

    Returns the schema groups. Raises ``uci_schema.EngineProbeError`` when the
    binary is missing or cannot be launched, which callers translate into a
    "not editable"/"not installed" response.
    """
    uci_schema.seed_config(engine_name, config_path=config_path)
    return uci_schema.get_schema(engine_name)


@app.route("/api/engines/<engine_name>/uci-schema", methods=["GET"])
@app.route("/api/engines/<engine_name>/profiles", methods=["GET"])
def api_get_engine_uci_schema(engine_name):
    """Return the probed option schema and current sections for an engine.

    Probes the installed binary for its real UCI options and seeds the writable
    config on first use. Engines that cannot be probed return ``editable: false``
    with an ``unavailable_reason``: a missing binary and one that is present but
    will not launch both land here, and reporting both as "not installed" is what
    let an engine show an Installed badge and a "not installed" editor at once.
    Sections come from the writable config.
    """
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return jsonify({"success": False, "error": "Invalid engine"}), 400
    try:
        groups = _seed_and_probe_schema(engine_name, config_path)
    except uci_schema.EngineProbeError as probe_error:
        return jsonify({
            "engine": engine_name, "editable": False, "schema": [], "profiles": [],
            "case_collisions": [],
            "unavailable_reason": sanitize_reason_code(probe_error.reason_code),
        })
    return jsonify({
        "engine": engine_name,
        "editable": True,
        "unavailable_reason": None,
        "schema": engine_profiles.schema_to_json(groups),
        # Enrich with the same display labels the Elo picker uses
        # (Default (Unlimited) / Default (1500 ELO)) so the profile editor list
        # never drifts from Players/board strength rows.
        "profiles": _profiles_with_labels(config_path),
        "case_collisions": _case_collisions(config_path),
    })


def _profiles_with_labels(config_path):
    """Return read_profiles rows plus a ``label`` matching strength_level_choices."""
    labels = {
        row["value"]: row["label"]
        for row in engine_profiles.strength_level_choices(config_path)
    }
    return [
        {**profile, "label": labels.get(profile["name"], profile["name"])}
        for profile in engine_profiles.read_profiles(config_path)
    ]


def _case_collisions(config_path):
    """Case-only duplicate profile name groups for the reconcile UI."""
    return engine_profiles.case_collision_groups(
        engine_profiles.read_profile_names(config_path)
    )


# Profile mutations use POST, not PUT/DELETE: the app's WebDAV before_request
# (handle_preflight) intercepts every PUT/DELETE app-wide and demands WebDAV
# auth, so REST routes cannot use those verbs. All engine endpoints (install,
# uninstall, resume, cancel) already use POST for the same reason.
# ``profiles/reset`` is registered before ``profiles/<profile_name>`` so the
# literal path is not captured as a profile name.
@app.route("/api/engines/<engine_name>/profiles/reset", methods=["POST"])
@requires_auth
def api_reset_engine_profiles(engine_name):
    """Delete the writable ``.uci`` and re-seed strength profiles from a probe.

    Escape hatch for a stuck Default-only config (or discarded custom profiles).
    Returns the same shape as GET profiles so the editor can refresh in one
    round-trip.

    404 only when the binary is genuinely absent; an installed engine that will
    not launch answers 409 with its reason code. The distinction matters because
    the reported symptom was a user clicking Reset repeatedly against a 404
    saying the engine was not installed while its card said it was -- a response
    describing a state they could see was false.
    """
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return jsonify({"success": False, "error": "Invalid engine"}), 400
    try:
        engine_bootstrap.reset_profiles(
            engine_name, config_path=config_path, store=_engine_failure_store,
        )
        groups = uci_schema.get_schema(engine_name)
    except uci_schema.EngineProbeError as probe_error:
        if probe_error.reason_code == LOAD_FAILURE_BINARY_MISSING:
            return jsonify({"success": False, "error": "Engine is not installed"}), 404
        return jsonify({
            "success": False,
            "error": "Engine is installed but did not start",
            "reason_code": sanitize_reason_code(probe_error.reason_code),
            "detail": sanitize_detail(probe_error.detail),
        }), 409
    return jsonify({
        "success": True,
        "engine": engine_name,
        "editable": True,
        "schema": engine_profiles.schema_to_json(groups),
        "profiles": _profiles_with_labels(config_path),
        "case_collisions": _case_collisions(config_path),
    })


@app.route("/api/engines/<engine_name>/profiles/reconcile-case", methods=["POST"])
@requires_auth
def api_reconcile_engine_profile_case(engine_name):
    """Keep one spelling of a case-colliding profile and delete the twins.

    Body: ``{"keep": "<exact section name>"}``. Used when a ``.uci`` file has
    both e.g. ``[Attacker]`` and ``[attacker]`` -- saving either used to
    overwrite the other via case-insensitive match. 404 when the keep name is
    absent; 400 when there are no case twins or a twin is reserved.
    """
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return jsonify({"success": False, "error": "Invalid engine"}), 400
    body = request.get_json(silent=True) or {}
    keep = body.get("keep")
    if not isinstance(keep, str) or not keep:
        return jsonify({"success": False, "error": "keep is required"}), 400
    names = engine_profiles.read_profile_names(config_path)
    if keep not in names:
        return jsonify({"success": False, "error": "Profile not found"}), 404
    group = next(
        (g for g in engine_profiles.case_collision_groups(names) if keep in g),
        None,
    )
    if group is None:
        return jsonify({
            "success": False,
            "error": "No case-variant duplicates for that profile",
        }), 400
    for twin in group:
        if twin == keep:
            continue
        blocked = engine_profiles.delete_blocked_reason(twin)
        if blocked is not None:
            return jsonify({"success": False, "error": blocked}), 400
    removed = engine_profiles.reconcile_case_duplicate(config_path, keep)
    return jsonify({
        "success": True,
        "keep": keep,
        "removed": removed,
        "profiles": _profiles_with_labels(config_path),
        "case_collisions": _case_collisions(config_path),
    })


@app.route("/api/engines/<engine_name>/profiles/<profile_name>", methods=["POST"])
@requires_auth
def api_save_engine_profile(engine_name, profile_name):
    """Create or replace a profile. Body: ``{"values": {key: value}}``.

    Optional ``rename_to`` writes under a new section name and removes
    ``profile_name`` (Elo-rung rename when ``UCI_Elo`` drifts). Values are
    validated against the probed engine schema (unknown keys and out-of-range
    values are rejected, not clamped, because engines apply them verbatim)
    before the section is written atomically. The whole section is replaced, so
    the client must submit the complete set of keys to retain.
    """
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return jsonify({"success": False, "error": "Invalid engine"}), 400
    try:
        groups = _seed_and_probe_schema(engine_name, config_path)
    except uci_schema.EngineProbeError:
        return jsonify({"success": False, "error": "Engine is not installed"}), 404
    body = request.get_json(silent=True) or {}
    values = body.get("values", {})
    # Validate up front and surface the message as a returned value. Catching
    # ProfileValidationError and returning str(e) would route a caught
    # exception's text into the response (CodeQL py/stack-trace-exposure); the
    # value-based checks below carry the same user-facing messages without that.
    if not engine_profiles.is_valid_profile_name(profile_name):
        # Default is reserved (seed-owned); name it so the UI can prompt save-as.
        # Match case-insensitively so "default" / "DeFaUlT" cannot bypass the guard.
        if (
            isinstance(profile_name, str)
            and profile_name.casefold() == engine_profiles.SEEDED_DEFAULT_PROFILE.casefold()
        ):
            return jsonify({
                "success": False,
                "error": "Default is reserved; save under a new profile name",
            }), 400
        return jsonify({"success": False, "error": "Invalid profile name"}), 400
    value_error = engine_profiles.validation_error(groups, values)
    if value_error is not None:
        return jsonify({"success": False, "error": value_error}), 400
    rename_to = body.get("rename_to")
    names = engine_profiles.read_profile_names(config_path)

    if rename_to is not None:
        if not isinstance(rename_to, str) or not rename_to.strip():
            return jsonify({"success": False, "error": "rename_to must be a non-empty string"}), 400
        rename_to = rename_to.strip()
        if not engine_profiles.is_valid_profile_name(rename_to):
            if (
                rename_to.casefold()
                == engine_profiles.SEEDED_DEFAULT_PROFILE.casefold()
            ):
                return jsonify({
                    "success": False,
                    "error": "Default is reserved; save under a new profile name",
                }), 400
            return jsonify({"success": False, "error": "Invalid rename_to name"}), 400
        if profile_name not in names:
            matches = engine_profiles.casefold_matches(names, profile_name)
            if len(matches) > 1:
                return jsonify({
                    "success": False,
                    "error": (
                        f"Ambiguous profile name: case variants {matches} exist. "
                        "Keep one spelling first."
                    ),
                    "case_collisions": _case_collisions(config_path),
                }), 409
            if not matches:
                return jsonify({"success": False, "error": "Profile not found"}), 404
        if rename_to not in names:
            matches = engine_profiles.casefold_matches(names, rename_to)
            if len(matches) > 1:
                return jsonify({
                    "success": False,
                    "error": (
                        f"Ambiguous profile name: case variants {matches} exist. "
                        "Keep one spelling first."
                    ),
                    "case_collisions": _case_collisions(config_path),
                }), 409
        written = engine_profiles.rename_profile(
            config_path, profile_name, rename_to, values, groups,
        )
        return jsonify({"success": True, "name": written})

    # Ambiguous case twins: refuse rather than overwriting the wrong section.
    if profile_name not in names:
        matches = engine_profiles.casefold_matches(names, profile_name)
        if len(matches) > 1:
            return jsonify({
                "success": False,
                "error": (
                    f"Ambiguous profile name: case variants {matches} exist. "
                    "Keep one spelling first."
                ),
                "case_collisions": _case_collisions(config_path),
            }), 409
    engine_profiles.write_profile(config_path, profile_name, values, groups)
    return jsonify({"success": True})


@app.route("/api/engines/<engine_name>/profiles/<profile_name>/delete", methods=["POST"])
@requires_auth
def api_delete_engine_profile(engine_name, profile_name):
    """Delete a profile section.

    400 if the name is reserved (``DEFAULT``) or invalid; 404 if the profile does
    not exist. Deletion operates on the writable config directly and does not
    require probing the engine.
    """
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return jsonify({"success": False, "error": "Invalid engine"}), 400
    # Reject the reserved section by value rather than catching the exception
    # delete_profile would raise and echoing it back (CodeQL py/stack-trace-exposure).
    blocked = engine_profiles.delete_blocked_reason(profile_name)
    if blocked is not None:
        return jsonify({"success": False, "error": blocked}), 400
    names = engine_profiles.read_profile_names(config_path)
    if profile_name not in names:
        matches = engine_profiles.casefold_matches(names, profile_name)
        if len(matches) > 1:
            return jsonify({
                "success": False,
                "error": (
                    f"Ambiguous profile name: case variants {matches} exist. "
                    "Keep one spelling first."
                ),
                "case_collisions": _case_collisions(config_path),
            }), 409
        if not matches:
            return jsonify({"success": False, "error": "Profile not found"}), 404
    removed = engine_profiles.delete_profile(config_path, profile_name)
    if not removed:
        return jsonify({"success": False, "error": "Profile not found"}), 404
    return jsonify({"success": True})


@app.route("/api/engines/<engine_name>/levels", methods=["GET"])
def api_get_engine_levels(engine_name):
    """Return the selectable strength sections for an engine.

    Mirrors the on-device picker: seeds the writable config by probing the
    binary, then returns its sections as ``{"value", "label"}`` rows (via
    ``strength_level_choices``), always including ``Default``. ``value`` is the
    section name persisted as the player's ``elo``; ``label`` is the display text
    (an uncapped ``Default`` shows as ``"Default (Unlimited)"``). Falls back to a single
    ``Default`` row when the engine cannot be probed or the name is invalid.
    """
    default_only = [{"value": "Default", "label": "Default"}]
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return jsonify(default_only)
    try:
        uci_schema.seed_config(engine_name, config_path=config_path)
        levels = engine_profiles.strength_level_choices(config_path)
    except uci_schema.EngineProbeError:
        levels = default_only
    if not any(level["value"] == "Default" for level in levels):
        levels.insert(0, {"value": "Default", "label": "Default"})
    return jsonify(levels)


@app.route("/api/engines/<engine_name>/failure/dismiss", methods=["POST"])
@requires_auth
def api_dismiss_engine_failure(engine_name):
    """Acknowledge an engine's last failure so its card stops showing the notice.

    Dismissal silences the notice only. ``profiles_ready`` is derived from the
    engine's actual state on disk and is unaffected, because dismissing is not a
    fix -- letting it clear the badge would put the card back to claiming a
    broken engine is healthy. A later failure on the same engine reopens the
    notice.

    The name is validated against the catalog and the custom-engine store: it
    arrives from the URL, and accepting anything would let a caller grow the
    record store without bound and would hide a client sending the wrong name.
    """
    from universalchess.managers.engine_manager import ENGINES

    known = engine_name in ENGINES or any(
        custom.id == engine_name for custom in _custom_engine_store.list()
    )
    if not known:
        return jsonify({"success": False, "error": "Unknown engine"}), 404
    _engine_failure_store.dismiss(engine_name)
    return jsonify({"success": True})


@app.route("/api/engines/all", methods=["GET"])
def api_get_all_engines():
    """Get full details of all engines for management UI."""
    try:
        from universalchess.managers.engine_manager import (
            EngineManager, ENGINES, arch_unsupported_reason, get_current_arch,
            canonical_ref, documentation_url, host_has_neon,
        )

        engine_manager = EngineManager()
        # Resolve once: the device architecture is constant for this process, and
        # it determines which engines can be installed here. NEON is read
        # separately because one architecture token spans CPUs that have it and
        # CPUs that do not.
        arch = get_current_arch()
        has_neon = host_has_neon()
        engines_list = []

        for name, engine_def in ENGINES.items():
            # "Installed" for the management list means present enough to show as
            # installed rather than offer a fresh Install: a system package is
            # always present; any other engine counts once its binary exists. A
            # net-backed engine whose nets are missing (a Maia whose weight
            # download failed) is still "installed" here -- it surfaces a Repair
            # action, not Install -- but it is NOT usable/available for play.
            binary_present = engine_manager.is_installed(name)
            installed = engine_def.is_system_package or binary_present
            needs_repair = engine_manager.needs_repair(name)
            can_repair = engine_manager.can_repair(name)
            missing_net_count = len(engine_manager.missing_nets(name))
            unsupported_reason = arch_unsupported_reason(
                engine_def, arch, has_neon=has_neon
            )
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
                # Page describing the engine, for the card's "learn more" link;
                # empty when the engine has none (the bundled novelty engines,
                # which exist only inside this project). Resolved here so the
                # frontend keeps no table of engine URLs.
                "info_url": documentation_url(engine_def) or "",
                "installed": installed,
                # A net-backed engine missing its nets is installed but broken:
                # `needs_repair` drives the UI's Repair affordance and a "needs
                # repair" badge; `can_repair` is True only when an in-place repair
                # procedure exists. False for every ordinary engine.
                "needs_repair": needs_repair,
                "can_repair": can_repair,
                # How many expected companion nets are still missing. 0 for a
                # complete or non-net engine. Drives the quiet "download N missing
                # weights" top-up label for a usable-but-incomplete engine (one
                # that can_repair yet does NOT need_repair).
                "missing_net_count": missing_net_count,
                "is_system_package": engine_def.is_system_package,
                "can_uninstall": engine_def.can_uninstall,
                "estimated_install_minutes": engine_def.estimated_install_minutes,
                "has_prebuilt": engine_def.has_prebuilt,
                # Every installed engine is editable now: its option schema is
                # discovered by probing the binary, not gated by a curated list.
                # A net-backed engine that needs repair is excluded: with no nets
                # its schema has nothing to offer (only an empty Default), so the
                # Repair action is surfaced instead of a broken profile editor.
                "has_profiles": installed and not needs_repair,
                # Whether the engine actually produced a strength ladder. Read
                # from the seeded .uci on disk, never by launching anything: this
                # list renders every catalog engine at once. `installed` only
                # says a file exists and stays true forever once written, so an
                # engine that installs and then fails to start would otherwise
                # keep reporting itself healthy -- the contradiction between the
                # card's badge and the profile editor's error.
                "profiles_ready": installed and not needs_repair and uci_schema.has_seeded_profiles(
                    name, config_path=_config_uci_path(name),
                ),
                # The last install/initialize failure, or None. Fixed tokens only
                # (see _failure_payload).
                "last_failure": _failure_payload(name),
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
                # No documentation link: an operator-added engine has no catalog
                # entry, and ``custom.url`` (when present) is where the binary was
                # downloaded from -- not a page describing the engine. It is already
                # shown in the description above.
                "info_url": "",
                "installed": installed,
                # Custom engines declare no companion nets, so they are never a
                # repair candidate; keep the field shape identical to catalog rows.
                "needs_repair": False,
                "can_repair": False,
                "missing_net_count": 0,
                "is_system_package": False,
                "can_uninstall": True,
                "estimated_install_minutes": 0,
                "has_prebuilt": False,
                # Custom engines are probed for their schema just like catalog
                # engines, so they are editable whenever their binary is present.
                "has_profiles": installed,
                "profiles_ready": installed and uci_schema.has_seeded_profiles(
                    custom.id, config_path=_config_uci_path(custom.id),
                ),
                "last_failure": _failure_payload(custom.id),
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

# Centaur SD-import progress is owned by centaur_import.import_state.STORE, the
# same pattern as engine installs: the import runs on a background thread after
# the upload, writing structured stage/percent state the UI polls. On startup an
# import left `active` by a killed process is reconciled to `interrupted` so the
# banner/panel stop waiting on a dead install (there is no resume; the operator
# re-imports).
from universalchess.services.centaur_import import import_state as centaur_import_state
_centaur_import_store = centaur_import_state.STORE
_centaur_import_store.reconcile_interrupted()


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
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  # https enforced by validate_download_url upstream
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
        _seed_uci_after_install(engine_id)
        _engine_install_store.finish(success=True, error=None)
    except Exception as e:
        app.logger.exception("Custom URL engine install failed: %s", e)
        _engine_install_store.finish(success=False, error="Installation failed")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _seed_uci_after_install(engine_name: str, display_name: Optional[str] = None) -> None:
    """Seed ``config/engines/<name>.uci`` after an install outside EngineManager.

    Covers the custom upload / URL paths; catalog installs run the same step from
    ``EngineManager.install_engine``. Never raises: the binary is already
    installed, and first-use seeding still applies. A failure is recorded and
    event-logged by ``engine_bootstrap`` so the card can report it.
    """
    config_path = _config_uci_path(engine_name)
    if config_path is None:
        return
    engine_bootstrap.initialize_profiles(
        engine_name,
        config_path=config_path,
        display_name=display_name,
        store=_engine_failure_store,
    )


def _run_engine_install(engine_name: str, ref: Optional[str] = None):
    """Background thread to install an engine, persisting structured progress.

    The stage_callback writes each update to the store so a concurrent
    GET /api/engines/status (and any fresh page load) sees the live stage and
    percent. ``ref`` is the optional git ref the user chose in the tag picker
    (None for the canonical ref).
    """
    from universalchess.managers.engine_manager import EngineManager

    def on_stage(stage, message, fraction, **measurements):
        # Measurements are passed straight through: naming them here meant this
        # signature had to be updated in step with every new reading the installer
        # produces, and missing one raised a TypeError that killed the install.
        _engine_install_store.update(stage, message, fraction, **measurements)

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


def _run_engine_repair(engine_name: str):
    """Background thread to repair an engine in place, persisting progress.

    Mirrors :func:`_run_engine_install` but calls ``repair_engine`` (fetch the
    missing companion nets without a rebuild). It writes to the SAME install-state
    store, so the existing status poll, progress bar, and activity banner reflect
    a repair with no extra UI wiring.
    """
    from universalchess.managers.engine_manager import EngineManager

    def on_stage(stage, message, fraction, **measurements):
        # Measurements are passed straight through: naming them here meant this
        # signature had to be updated in step with every new reading the installer
        # produces, and missing one raised a TypeError that killed the install.
        _engine_install_store.update(stage, message, fraction, **measurements)

    try:
        engine_manager = EngineManager()
        success = engine_manager.repair_engine(engine_name, stage_callback=on_stage)
        error = None if success else (engine_manager.get_install_error() or "Repair failed")
        if success:
            # The nets on disk just changed. The writable config was seeded before
            # they arrived (net-less: no per-net profiles, possibly an empty
            # Default), and seed_config is one-shot, so without this the fetched
            # nets never become selectable profiles -- the "weights not listed
            # after repair" bug. Reconcile ADDS the missing sections/keys and
            # preserves user edits. A reconcile failure does not undo a successful
            # net fetch, so it is logged, not promoted to a repair failure.
            try:
                config_path = _config_uci_path(engine_name)
                if config_path is not None:
                    uci_schema.reconcile_config(engine_name, config_path=config_path)
            except uci_schema.EngineProbeError as reconcile_error:
                app.logger.warning(
                    "Repair of %s succeeded but profile reconcile failed: %s",
                    engine_name, reconcile_error,
                )
        _engine_install_store.finish(success=success, error=error)
    except Exception as e:
        app.logger.exception("Engine repair failed: %s", e)
        _engine_install_store.finish(success=False, error="Repair failed")


def _start_engine_repair(engine_name: str):
    """Initialize the persisted state and spawn the repair thread.

    Caller validates the engine name, that it is repairable, and that no install
    is already active. The estimate paces the progress creep; a weights-only
    repair is a few minutes of downloads, not a full build.
    """
    from universalchess.managers.engine_manager import ENGINES
    engine = ENGINES[engine_name]
    _engine_install_store.start(
        engine_name,
        engine.display_name,
        estimated_seconds=5 * 60,
    )
    thread = threading.Thread(target=_run_engine_repair, args=(engine_name,), daemon=True)
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


@app.route("/api/engines/repair", methods=["POST"])
@requires_auth
def api_repair_engine():
    """Repair an installed-but-incomplete engine in place (e.g. Maia's nets).

    Requires authentication: repair runs a privileged helper (downloads nets into
    the managed install dir), so it is gated like install. Only an engine the
    manager reports as ``can_repair`` (installed, missing its required nets, and
    with a repair procedure) is accepted; anything else is a 400 so the client
    cannot start a meaningless repair. Runs asynchronously through the shared
    install-state store (one operation at a time), so the existing status poll
    shows progress exactly like an install.
    """
    try:
        data = request.get_json(silent=True) or {}
        engine_name = data.get("engine")

        if not engine_name:
            return jsonify({"success": False, "error": "No engine specified"}), 400

        from universalchess.managers.engine_manager import ENGINES, EngineManager
        if engine_name not in ENGINES:
            return jsonify({"success": False, "error": f"Unknown engine: {engine_name}"}), 400

        if not EngineManager().can_repair(engine_name):
            return jsonify({
                "success": False,
                "error": f"{engine_name} has nothing to repair.",
            }), 400

        if _engine_install_store.status_dict()["active"]:
            return jsonify({
                "success": False,
                "error": f"Already installing {_engine_install_store.status_dict()['engine']}",
            }), 409

        _start_engine_repair(engine_name)
        return jsonify({"success": True, "message": f"Repairing {engine_name}"})
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
        _seed_uci_after_install(engine_id)
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

    Combines the structured engine-install state, the Centaur SD-import state, and
    the BlueZ self-heal progress record into one uniform list (see
    services/background_activity) so the banner can render any background work
    generically. ``read_progress`` and ``status_dict`` never raise, so a missing
    marker/state degrades to "idle" rather than erroring the poll. Returns
    ``{"active": bool, "activities": []}``.
    """
    from universalchess.managers.bluez_patch_status import read_progress
    from universalchess.services.background_activity import activity_snapshot

    return jsonify(activity_snapshot(
        _engine_install_store.status_dict(),
        read_progress(),
        _centaur_import_store.status_dict(),
    ))


@app.route("/api/engines/resume", methods=["POST"])
def api_resume_engine_install():
    """Resume an install that was interrupted by a process/board restart.

    Valid only when the persisted state is `interrupted`. Relaunches the install
    for that engine; the cached git clone is reused (git pull), so engine source
    is not re-downloaded. True mid-build continuation is not possible -- "resume"
    re-runs the install operation, which is idempotent.

    Deliberately not ``@requires_auth``, unlike every other engine mutation: it
    can only re-run an install a user already authenticated to start, and the
    engine name comes from the persisted state rather than the request, so a
    caller cannot choose what gets built. Any other precondition (no interrupted
    install, one already active) is rejected below. The exception is pinned in
    tests/test_engine_endpoint_auth.py; do not add endpoints to that list without
    an equivalent argument.
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
@requires_auth
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


@app.route("/api/system/timezone", methods=["GET"])
def api_timezone_get():
    """Return the device's configured timezone (IANA name, defaults to UTC)."""
    try:
        from universalchess.services.timezone_service import get_timezone
        return jsonify({"timezone": get_timezone()})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/timezone", methods=["POST"])
@requires_auth
def api_timezone_set():
    """Set the device's OS timezone.

    Body: {"timezone": "Area/Location"}

    Persists the IANA zone and applies it to the OS clock via the pinned
    uc-set-timezone helper. An unknown zone is a 400. If the OS apply step fails
    (e.g. missing sudo grant), the choice is still saved and the response marks
    ``applied: false`` so the UI can reflect the selection and surface that it is
    not yet active. The main process is notified so its clock display refreshes.
    """
    try:
        from universalchess.services.timezone_service import set_timezone
        data = request.get_json(force=True)
        tz = (data or {}).get("timezone", "")
        try:
            applied = set_timezone(tz)
        except ValueError:
            return jsonify({"error": f"Invalid timezone: {tz}"}), 400
        from universalchess.services.game_broadcast import notify_main_process_settings_changed
        notify_main_process_settings_changed()
        return jsonify({"success": True, "timezone": tz, "applied": applied})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/language", methods=["GET"])
def api_language_get():
    """Return the device's current UI language (locale code, defaults to en)."""
    try:
        from universalchess.services.language_service import get_language
        return jsonify({"language": get_language()})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/system/language", methods=["POST"])
@requires_auth
def api_language_set():
    """Set the device UI language.

    Body: {"language": "en" | "es"}

    Persists the locale in [system] ui_language. An unsupported code is a 400.
    Unlike timezone there is no OS apply step -- the locale only selects
    translations -- but the main process is still notified so the e-paper menu
    re-renders in the new language, and the SSE ``settings_changed`` fan-out
    (via the notify path) lets the web app switch its own locale.
    """
    try:
        from universalchess.services.language_service import set_language
        data = request.get_json(force=True)
        code = (data or {}).get("language", "")
        try:
            set_language(code)
        except ValueError:
            return jsonify({"error": f"Invalid language: {code}"}), 400
        from universalchess.services.game_broadcast import notify_main_process_settings_changed
        notify_main_process_settings_changed()
        return jsonify({"success": True, "language": code})
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
            download_url = f"http://{request.host}/ca.pem"  # nosemgrep: python.flask.security.injection.tainted-url-host.tainted-url-host  # QR encodes this board's own Host so phones fetch /ca.pem from the same device
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