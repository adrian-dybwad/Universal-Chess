"""Run the Centaur engine proxy as ``python -m universalchess.services.centaur_engine_proxy``.

The installed ``engines/stockfish_pi`` launcher invokes this with the UC venv
python so Centaur's engine path routes through the proxy.
"""

import sys

from universalchess.services.centaur_engine_proxy.proxy import main

if __name__ == "__main__":
    sys.exit(main())
