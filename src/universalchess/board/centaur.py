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

from universalchess.board.settings import Settings
from subprocess import PIPE, Popen, check_output  # nosec B404 - used only for fixed internal commands
import subprocess  # nosec B404 - used only for fixed internal commands
import shlex
import pathlib
import os, sys
import time
import json
import urllib.request

from universalchess.board.logging import log

def get_lichess_api():
    """Return the active Lichess API token.

    Multi-account aware: prefers the first saved account's token (the migration
    target of the former single credential), falling back to the legacy
    ``[lichess]`` token for a board that predates migration or has no accounts
    yet. This is the single back-compat shim the older single-account consumers
    (accounts display, ``get_lichess_client``) route through.
    """
    from universalchess.players.lichess.accounts import default_lichess_credential

    account = default_lichess_credential()
    if account is not None:
        token = account.get("api_token", "")
        if token:
            return token
    return Settings.read('lichess','api_token','')

def get_lichess_username():
    """Return the Lichess username of the active account.

    Prefers the first saved account's resolved username; falls back to the
    legacy ``[lichess].username`` cached on the last successful authentication so
    the account can be shown without a network call on an unmigrated board.
    Empty when no username is known.
    """
    from universalchess.players.lichess.accounts import default_lichess_credential

    account = default_lichess_credential()
    if account is not None:
        username = account.get("username", "")
        if username:
            return username
    return Settings.read('lichess','username','')

def get_lichess_range():    
    return Settings.read('lichess','range','0-3000')

def get_menuEngines():
    return Settings.read('menu','showEngines', 'checked')

def get_menuHandBrain():
    return Settings.read('menu','showHandBrain', 'checked')

def get_menu1v1Analysis():
    return Settings.read('menu','show1v1Analysis','checked')

def get_menuEmulateEB():
    return Settings.read('menu','showEmulateEB','checked')

def get_menuCast():
    return Settings.read('menu','showCast','checked')

def get_menuSettings():
    return Settings.read('menu','showSettings','checked')

def get_menuAbout():
    return Settings.read('menu','showAbout','checked')

def get_sound():
    return Settings.read('sound','sound','on')

def set_lichess_api(key):
    # A different token may belong to a different account, so drop the cached
    # username; it is repopulated on the next successful authentication.
    if key != get_lichess_api():
        Settings.write('lichess','username','')
    return Settings.write('lichess','api_token', key)

def set_lichess_username(username):
    """Cache the authenticated Lichess username for the stored token."""
    return Settings.write('lichess','username', username or '')

def set_lichess_range(newrange):
    return Settings.write('lichess','range',newrange)

def set_sound(onoff):
    return Settings.write('sound','sound','on')

def set_menuEngines(val):
    return Settings.write('menu','showEngines',val)
        
def set_menuHandBrain(val):
    return Settings.write('menu','showHandBrain',val)

def set_menu1v1Analysis(val):
    return Settings.write('menu','show1v1Analysis',val)  
        
def set_menuEmulateEB(val):
    return Settings.write('menu','showEmulateEB',val)
        
def set_menuCast(val):
    return Settings.write('menu','showCast',val)
        
def set_menuSettings(val):
    return Settings.write('menu','showSettings',val)
        
def set_menuAbout(val):
    return Settings.write('menu','showAbout',val)                          

def dgtcm_path():
    return str(pathlib.Path(__file__).parent.resolve()) + "/.."

def shell_run(rcmd):
    cmd = shlex.split(rcmd)
    executable = cmd[0]
    executable_options=cmd[1:]
    # List-form invocation (no shell=True), so the split args cannot be
    # re-interpreted by a shell; callers pass fixed internal command strings.
    proc  = Popen(([executable] + executable_options), stdout=PIPE, stderr=PIPE)  # noqa: S603 # nosec B603
    response = proc.communicate()
    response_stdout, response_stderr = response[0], response[1]
    if response_stderr:
        log.debug(response_stderr)
        return -1
    else:
        log.debug(response_stdout)
        return response_stdout


config = Settings.get_config()
lichess_api = Settings.read('lichess','api_token','')
lichess_range = Settings.read('lichess','range','0-3000')
centaur_sound = Settings.read('sound','sound','on')


# Legacy UpdateSystem removed - use universalchess.services.update_service instead
