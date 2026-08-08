""" Handles config in the centaur.ini """

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

import configparser
import io
import logging
import os
import tempfile

class Settings:
    """ Class handling config.ini """

    configfile = '/opt/universalchess/config/centaur.ini'
    defconfigfile = '/opt/universalchess/defaults/config/centaur.ini'

    # Parsed defaults keyed by source path. The defaults file is shipped,
    # read-only data that cannot change while the process runs, so parsing it
    # once is safe -- unlike the live config, which the web process also writes
    # and which is therefore always re-read. Keyed by path (rather than a single
    # slot) so a caller that repoints defconfigfile gets its own entry.
    _defaults_cache = {}

    @staticmethod
    def read(section, key, default = ''):
        """
        Read a value from the key in the section.
        
        Falls back to default config file, then to provided default if config
        directory is missing or key doesn't exist.

        Reading never writes. An earlier version called ensure_key_exists() here
        to materialize the key with its default, which made every first-touch
        read perform an fsync'd atomic write and cost a second full parse of the
        file. That call could not change the value returned -- the fallback chain
        below already resolves it -- so it was pure cost. Use ensure_key_exists()
        explicitly if a key genuinely needs to be persisted.
        """
        config = Settings._safe_read()
        return Settings._resolve(config, section, key, default)

    @staticmethod
    def read_section(section, defaults):
        """Read many keys from one section with a single parse of the config.

        The per-key :meth:`read` re-parses the whole file for every key, so
        loading a settings dataclass cost one parse per field (roughly 250 for
        the app's settings, on a file under 2 KB). Callers that want a whole
        section should use this instead: it parses once and resolves each key
        through the same live -> defaults-file -> caller-default chain, so the
        values are identical to calling read() per key.

        Args:
            section: Section name in the config file.
            defaults: Mapping of key -> fallback value used when the key is
                absent from both the live config and the defaults file. Its keys
                determine which settings are read.

        Returns:
            Dict with the same keys as ``defaults`` and the resolved raw string
            values. Type coercion is the caller's concern.
        """
        config = Settings._safe_read()
        return {
            key: Settings._resolve(config, section, key, default)
            for key, default in defaults.items()
        }

    @staticmethod
    def _resolve(config, section, key, default):
        """Resolve one key against an already-parsed live config.

        The resolution order is live config, then the bundled defaults file,
        then the caller's default. Shared by read() and read_section() so the
        two can never disagree about what a setting resolves to. The defaults
        file is only consulted on a miss, so a fully-configured board never
        touches it.
        """
        if config.has_section(section) and config.has_option(section, key):
            return config[section][key]

        defconfig = Settings._read_defaults()
        if defconfig.has_section(section) and defconfig.has_option(section, key):
            return defconfig[section][key]

        return default

    @staticmethod
    def _read_defaults():
        """Return the parsed defaults file, parsing it at most once per path."""
        path = Settings.defconfigfile
        cached = Settings._defaults_cache.get(path)
        if cached is None:
            cached = configparser.ConfigParser()
            try:
                cached.read(path)
            except configparser.Error as exc:
                # Unparseable defaults are not recoverable from anywhere, so
                # keep the empty parser: every lookup then falls through to the
                # caller's default rather than raising out of a read.
                logging.error(f"Corrupt defaults {path} ({exc}); ignoring")
            Settings._defaults_cache[path] = cached
        return cached

    @staticmethod
    def write(section, key, value, default = ''):
        """ Write a value to the key in the section """
        config = Settings._safe_read()
        # If config directory is missing, ensure_key_exists() can't persist the section.
        # Do not raise here; log and attempt a best-effort write.
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, key, str(value))
        Settings.write_config(config)

    @staticmethod
    def delete(section, key):
        config = Settings._safe_read()
        if not config.has_section(section):
            # Nothing persisted, so nothing to delete. Keep behavior non-fatal.
            return
        config.remove_option(section, key)
        Settings.write_config(config)

    @staticmethod
    def ensure_key_exists(section, key, default = ''):
        """Materialize a key into the live config, writing it if absent.

        An explicit migration/repair step, deliberately NOT called by read() or
        write(): both resolve or set the key themselves, so calling this from
        them only added a parse and (on first touch) an fsync'd write to every
        operation. Call it directly when a key must be present on disk, e.g.
        rebuilding a section after the live config was restored from defaults.
        """
        config = Settings._safe_read()
        # First make sure the section is there
        if not config.has_section(section):
            config.add_section(section)
            Settings.write_config(config)
        # Then check if it has the key
        if not config.has_option(section, key):
            # If not then we want to get the value from defaults if we can
            defconfig = configparser.ConfigParser()
            defconfig.read(Settings.defconfigfile)
            value = ''
            if defconfig.has_section(section):                
                if defconfig.has_option(section, key):                    
                    value = defconfig[section][key]
            # If there's no default given then take the default in the parameter
            if value == '':
                value = default
            config.set(section, key, value)
            Settings.write_config(config)

    @staticmethod
    def get_config():
        return Settings._safe_read()

    @staticmethod
    def _safe_read():
        """Read the live config, recovering automatically from a corrupt file.

        configparser.read() raises ParsingError when the file is unparseable -
        e.g. zero-filled by an unclean shutdown (the SD-card null-byte
        corruption observed on hardware). Because the config is read at import
        time, letting that error propagate crash-loops the whole app on boot
        (blank screen). Instead the corrupt file is replaced from the bundled
        defaults (best effort) and re-read; if defaults are also unavailable an
        empty config is returned so the app still boots and ensure_key_exists()
        can rebuild keys from there.

        read() silently ignores a missing file, so a first-run absent config is
        not treated as corruption.
        """
        config = configparser.ConfigParser()
        try:
            config.read(Settings.configfile)
            return config
        except configparser.Error as exc:
            logging.error(
                f"Corrupt config {Settings.configfile} ({exc}); "
                "restoring from defaults")
        Settings._restore_from_defaults()
        config = configparser.ConfigParser()
        try:
            config.read(Settings.configfile)
        except configparser.Error as exc:
            # The restore wrote unparseable content (defaults themselves
            # corrupt). Fall back to an empty config so the app still boots.
            logging.error(
                f"Config still unreadable after restore ({exc}); starting empty")
            config = configparser.ConfigParser()
        return config

    @staticmethod
    def _restore_from_defaults():
        """Overwrite the live config with the bundled defaults.

        Recovery path for a corrupt live file. If the defaults are missing or
        unreadable, the live file is reset to empty (still parseable) so the next
        read succeeds and ensure_key_exists() repopulates it. Writes atomically
        so the recovery itself cannot leave another partial file.
        """
        config_dir = os.path.dirname(Settings.configfile)
        try:
            os.makedirs(config_dir, exist_ok=True)
        except OSError as exc:
            logging.error(f"Cannot create config dir {config_dir}: {exc}")
            return
        contents = ""
        if os.path.exists(Settings.defconfigfile):
            try:
                with open(Settings.defconfigfile, "r", encoding="utf-8") as src:
                    contents = src.read()
            except OSError as exc:
                logging.error(
                    f"Cannot read defaults {Settings.defconfigfile}: {exc}")
        Settings._atomic_write_text(contents)

    @staticmethod
    def write_config(config):
        """
        Writes the config.ini file.
        
        Logs an error if the config directory doesn't exist, indicating
        an installation problem.
        """
        config_dir = os.path.dirname(Settings.configfile)
        if not os.path.exists(config_dir):
            logging.error(
                f"Config directory does not exist: {config_dir}. "
                "This indicates an incomplete installation. "
                "Please reinstall DGTCentaurMods or create the directory manually."
            )
            return
        buffer = io.StringIO()
        config.write(buffer)
        Settings._atomic_write_text(buffer.getvalue())

    @staticmethod
    def _atomic_write_text(text):
        """Write text to the config file atomically.

        Writes to a temp file in the same directory, flushes + fsyncs it, then
        os.replace()s it over the target. os.replace is atomic on POSIX, so a
        reader (or a power loss) always sees either the complete old file or the
        complete new one - never the truncated/zero-filled file a plain in-place
        open(...,'w') leaves when interrupted mid-write. That truncation was the
        root cause of the boot-loop corruption this guards against.
        """
        config_dir = os.path.dirname(Settings.configfile)
        fd, tmp_path = tempfile.mkstemp(
            dir=config_dir, prefix=".centaur.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, Settings.configfile)
        except OSError as exc:
            logging.error(
                f"Atomic write to {Settings.configfile} failed: {exc}")
            # The partial temp file must not be left behind: it sits in the
            # config directory under a random name, so every failed write would
            # add another invisible file nothing later cleans up. A failure to
            # remove it is reported rather than swallowed, naming the path so it
            # can be removed by hand -- the write has already failed, so there
            # is no further recovery to attempt here.
            try:
                os.unlink(tmp_path)
            except OSError as cleanup_exc:
                logging.warning(
                    f"Could not remove temporary file {tmp_path} after a failed "
                    f"write: {cleanup_exc}")
