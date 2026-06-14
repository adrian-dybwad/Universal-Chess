"""Shared menu catalog package.

Holds the JSON menu catalog and icon registry plus the loader/validator that
makes them available to both the e-paper board renderer and the web UI.
"""

from universalchess.menus.catalog.loader import (
    CatalogError,
    MenuCatalog,
    get_catalog,
    load_catalog,
)

__all__ = [
    "CatalogError",
    "MenuCatalog",
    "get_catalog",
    "load_catalog",
]
