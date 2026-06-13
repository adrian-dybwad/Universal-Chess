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

    @staticmethod
    def read(section, key, default = ''):
        """
        Read a value from the key in the section.
        
        Falls back to default config file, then to provided default if config
        directory is missing or key doesn't exist.
        """
        Settings.ensure_key_exists(section, key, default)
        config = Settings._safe_read()
        
        if config.has_section(section) and config.has_option(section, key):
            return config[section][key]
        
        # Config file missing or incomplete - try defaults file
        defconfig = configparser.ConfigParser()
        defconfig.read(Settings.defconfigfile)
        if defconfig.has_section(section) and defconfig.has_option(section, key):
            return defconfig[section][key]
        
        return default

    @staticmethod
    def write(section, key, value, default = ''):
        """ Write a value to the key in the section """
        Settings.ensure_key_exists(section, key, default)
        config = Settings._safe_read()
        # If config directory is missing, ensure_key_exists() can't persist the section.
        # Do not raise here; log and attempt a best-effort write.
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, key, str(value))
        Settings.write_config(config)

    @staticmethod
    def delete(section, key):
        Settings.ensure_key_exists(section, key, '')
        config = Settings._safe_read()
        if not config.has_section(section):
            # Nothing persisted, so nothing to delete. Keep behavior non-fatal.
            return
        config.remove_option(section, key)
        Settings.write_config(config)

    @staticmethod
    def ensure_key_exists(section, key, default = ''):
        """ Ensures that the key exists in config.ini """
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
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
