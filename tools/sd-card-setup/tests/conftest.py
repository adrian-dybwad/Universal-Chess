"""Make the tool's modules importable by the tests.

``tools/sd-card-setup`` is a script directory rather than an installed package,
so pytest's default import mode puts only this ``tests`` directory on the path.
Adding the parent here keeps the test module's own imports ordinary, so an
import sorter cannot reorder a path-setup statement below the import that
depends on it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
