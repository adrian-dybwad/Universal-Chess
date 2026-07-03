# Built-in Coaches
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Built-in coach definitions shipped with the app.

Each module here defines one :class:`universalchess.coaches.base.Coach` subclass.
The registry discovers every module in this package automatically, so adding a
new built-in coach is just adding a module -- no registration list to update.
Users add their own coaches by dropping modules into the user coaches folder
instead of editing this package (see :mod:`universalchess.coaches.registry`).
"""
