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
import sys
import io

# Force line-buffered stdout to prevent interleaved output from multiple threads
# This is particularly important on 64-bit systems where buffer behavior differs
if hasattr(sys.stdout, 'reconfigure'):
    # Python 3.7+ - reconfigure to line-buffered mode
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
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
    except Exception:
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


def setup_logging(log_file_path=None, log_level=logging.DEBUG):
    """Configure logging with colored console output and file output.
    
    Args:
        log_file_path: Path to the log file. Defaults to ~/debug.log.
                       If explicitly set to empty string, file logging is skipped.
        log_level: Logging level to set (default: logging.DEBUG).
    
    Returns:
        The configured logger instance.
    """
    log = logging.getLogger()
    log.setLevel(log_level)
    log.handlers = []

    if log_file_path is None:
        from pathlib import Path
        log_file_path = str(Path.home() / "debug.log")
    
    # File handler with plain formatter
    _fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s", "%Y-%m-%d %H:%M:%S")
    
    if log_file_path:
        try:
            _fh = logging.FileHandler(log_file_path, mode="w")
            _fh.setLevel(log_level)
            _fh.setFormatter(_fmt)
            log.addHandler(_fh)
        except Exception:
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

