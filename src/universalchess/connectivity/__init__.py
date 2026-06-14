"""UI-agnostic connectivity operations shared by the board and the web app.

Modules here wrap system networking tools (iwlist/nmcli/rfkill for WiFi, BlueZ
for Bluetooth) as pure functions returning plain data, with no e-paper/board or
Flask dependencies. The board menus (via thin wrappers in ``utils``) and the
Flask web API both call into these so there is a single implementation per
feature rather than one per surface.
"""
