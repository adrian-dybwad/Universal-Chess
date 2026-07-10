"""Shared menu catalog package.

Holds the JSON menu catalog and icon registry plus the loader/validator that
makes them available to both the e-paper board renderer and the web UI.
"""

from universalchess.menus.catalog.loader import (
    CatalogError,
    MenuCatalog,
    get_catalog,
    get_localized_catalog,
    load_catalog,
    localize_catalog,
    refresh_active_language,
)

__all__ = [
    "CatalogError",
    "MenuCatalog",
    "get_catalog",
    "get_localized_catalog",
    "load_catalog",
    "localize_catalog",
    "refresh_active_language",
]
