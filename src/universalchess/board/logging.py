# Logging Configuration
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import logging
import os
import sys
import io
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Keep the on-disk debug log bounded so a long-running board never fills the
# disk, while retaining recent history across restarts. The old handler opened
# the file with mode="w", so every process start wiped the log (nothing survived
# a restart) and, being a systemd-restarted service, the only "protection"
# against unbounded growth was that truncation. Appending + rotation gives both
# properties the deployment needs: history persists across restarts AND total
# size is capped at (BACKUP_COUNT + 1) * MAX_BYTES.
DEBUG_LOG_MAX_BYTES = 5_000_000
DEBUG_LOG_BACKUP_COUNT = 3

# Force line-buffered stdout to prevent interleaved output from multiple threads
# This is particularly important on 64-bit systems where buffer behavior differs
if hasattr(sys.stdout, 'reconfigure'):
    # Python 3.7+ - reconfigure to line-buffered mode
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: S110  # nosec B110 - best-effort stdout reconfigure; failure is non-fatal and intentionally ignored
        pass
elif not isinstance(sys.stdout, io.TextIOWrapper):
    # Fallback for older Python - wrap stdout with line buffering
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, 
            encoding=sys.stdout.encoding,
            errors=sys.stdout.errors,
            line_buffering=True
        )
    except Exception:  # noqa: S110  # nosec B110 - best-effort stdout wrap; failure is non-fatal and intentionally ignored
        pass


class ColoredFormatter(logging.Formatter):
    """Formatter that adds colors to log levels for console output."""
    
    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
    }
    RESET = '\033[0m'
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_colors = sys.stdout.isatty()
    
    def format(self, record):
        if self.use_colors:
            original_levelname = record.levelname
            # Pad to 8 characters before adding color codes
            padded_levelname = f"{original_levelname:>8}"
            color = self.COLORS.get(original_levelname, '')
            record.levelname = f"{color}{padded_levelname}{self.RESET}"
            result = super().format(record)
            record.levelname = original_levelname
            return result
        return super().format(record)


def _default_log_path():
    """Resolve the per-process debug-log path under the service user's home.

    The app runs as two long-lived processes (``universalchess.main`` and the
    Flask web service). They must not share one file: a single
    ``RotatingFileHandler`` opened by two processes races on rollover -- one
    process renames the file the other still holds open, so lines are lost and
    the size cap is defeated. The main entrypoint keeps ``~/debug.log`` because
    the authenticated debug-download endpoint serves that exact path; the web
    service, identified by the ``FLASK_RUN_PORT`` its unit sets, writes
    ``~/debug-web.log``. Both remain bounded and survive restarts.
    """
    home = Path.home()
    if os.environ.get("FLASK_RUN_PORT"):
        return str(home / "debug-web.log")
    return str(home / "debug.log")


def setup_logging(log_file_path=None, log_level=logging.DEBUG):
    """Configure logging with colored console output and file output.
    
    Args:
        log_file_path: Path to the log file. Defaults to the per-process path
                       from :func:`_default_log_path` (``~/debug.log`` for the
                       main process). If explicitly set to empty string, file
                       logging is skipped.
        log_level: Logging level to set (default: logging.DEBUG).
    
    Returns:
        The configured logger instance.
    """
    log = logging.getLogger()
    log.setLevel(log_level)
    log.handlers = []

    if log_file_path is None:
        log_file_path = _default_log_path()
    
    # File handler with plain formatter
    _fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s", "%Y-%m-%d %H:%M:%S")
    
    if log_file_path:
        try:
            # Append + rotate (not mode="w"): the log survives restarts and is
            # capped at (DEBUG_LOG_BACKUP_COUNT + 1) * DEBUG_LOG_MAX_BYTES.
            _fh = RotatingFileHandler(
                log_file_path,
                maxBytes=DEBUG_LOG_MAX_BYTES,
                backupCount=DEBUG_LOG_BACKUP_COUNT,
            )
            _fh.setLevel(log_level)
            _fh.setFormatter(_fmt)
            log.addHandler(_fh)
        except Exception:  # noqa: S110  # nosec B110 - best-effort file logging; a log-file failure must not prevent startup
            pass
    
    # Console handler with colored formatter
    # Use '\r\n' terminator to ensure cursor returns to column 0 on all terminals.
    # Plain '\n' can cause staircase output when D-Bus/GLib callbacks log from
    # their mainloop thread, as some terminals only interpret '\n' as line feed
    # without carriage return.
    _ch = logging.StreamHandler(sys.stdout)
    _ch.terminator = '\r\n'
    _ch.setLevel(log_level)
    _ch.setFormatter(ColoredFormatter("%(asctime)s.%(msecs)03d %(levelname)s [%(filename)s:%(lineno)d] %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_ch)
    
    return log


# Automatically configure and export log on module import
log = setup_logging()

