#!/usr/bin/env python3
# Universal Chess entry point
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Start the board.

Usage:
    python3 -m universalchess.main

This module stays small on purpose. The bring-up sequence
(:func:`universalchess.app.bootstrap.boot`) has to finish before the application
module is imported: it puts a splash on the panel, so the user watches the board
wake rather than a blank screen while that module's own slow imports run, and it
has the controller ready by the time the application asks for it.

Keeping those steps out of module scope is also what makes the application
importable at all -- previously importing the entry point booted the product,
which is why so much of it could never be tested.
"""


def main() -> None:
    """Bring the board up, then run it."""
    from universalchess.app.bootstrap import boot

    boot()

    # Imported after boot(), not before: this is the slow, splash-reporting part.
    from universalchess.app import board_app

    board_app.main()


if __name__ == "__main__":
    main()
