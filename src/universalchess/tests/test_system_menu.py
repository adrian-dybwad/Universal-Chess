"""Tests for the System tab's catalog-driven device-preference selects.

Background / why these tests exist
----------------------------------
Sleep Timer, Timezone, and Language existed twice: once as board nodes in the
``system`` menu (``system.inactivity``/``system.timezone``/``system.language``)
and again as a parallel web-only trio (``field.system.*``) the web hand-built.
That duplication is exactly the drift the shared catalog is meant to remove, so
the two were converged onto the single board node set:

- a web-only container (``group.system.device``) lists the *shared* nodes, giving
  ``<MenuContainer>`` a web layout without a second copy of the leaves;
- ``system.timezone``/``system.language`` became ``["board", "web"]`` so the web
  renders the same nodes the board does;
- Timezone keeps a per-platform option source -- the board's curated
  ``timezones_common`` and, via ``webProvider``, the full runtime list on the web
  (the one modelling difference that genuinely required a split);
- the ``field.system.*`` trio was deleted.

These tests pin that converged shape so the duplication cannot silently return.
"""

from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import applies_to_platform

_DEVICE_CONTAINER = "group.system.device"

# The shared nodes the web container lists, in tab order, paired with the
# ``system``-store key each binds. Sleep Timer keeps the board's ``sleep_seconds``
# key (the web store maps it to its form); Timezone/Language bind the keys whose
# web setter posts to the dedicated device endpoints.
_EXPECTED_FIELDS = [
    ("system.inactivity", "sleep_seconds"),
    ("system.timezone", "timezone"),
    ("system.language", "ui_language"),
]

# The old web-only duplicates, now deleted; asserting their absence guards the
# convergence from being undone by re-adding a parallel node set.
_DELETED_DUPLICATES = [
    "field.system.sleep_timer",
    "field.system.timezone",
    "field.system.language",
]


def _catalog():
    return load_catalog()


def test_device_container_lists_the_shared_nodes_in_order():
    """``group.system.device`` lists the shared ``system.*`` nodes in tab order.

    Why this test exists: ``<MenuContainer>`` renders whatever the container names,
    in order, and it must name the *shared* board nodes rather than a web-only
    copy. How a regression manifests: pointing children back at a ``field.system.*``
    duplicate reintroduces the drift, or a reorder shows Sleep Timer/Timezone/
    Language out of order.
    """
    catalog = _catalog()
    assert catalog.has_node(_DEVICE_CONTAINER)
    node = catalog.get_node(_DEVICE_CONTAINER)
    assert node["type"] == "group"
    assert catalog.child_ids(_DEVICE_CONTAINER) == [nid for nid, _ in _EXPECTED_FIELDS]


def test_shared_fields_bind_into_the_system_store():
    """Each shared node binds to its ``system``-store key.

    Why: the engine reads/writes a control through ``bind``; the web setter keys
    off these exact keys to route Timezone/Language to their device endpoints and
    Sleep Timer to the form. How a regression manifests: a changed key silently
    routes a write to the wrong setting (e.g. Timezone no longer reaching the
    timezone endpoint).
    """
    catalog = _catalog()
    for node_id, expected_key in _EXPECTED_FIELDS:
        bind = catalog.get_node(node_id).get("bind")
        assert bind == {"store": "system", "key": expected_key}, node_id


def test_shared_selects_render_on_both_platforms():
    """The three shared selects apply to both board and web.

    Why: convergence means one node set feeds both renderers; Timezone and
    Language were board-only before. How a regression manifests: reverting either
    to ``["board"]`` drops it from the web tab (the container yields two rows), or
    to ``["web"]`` drops it from the board's own System menu.
    """
    catalog = _catalog()
    for node_id, _ in _EXPECTED_FIELDS:
        node = catalog.get_node(node_id)
        assert applies_to_platform(node, "board") is True, node_id
        assert applies_to_platform(node, "web") is True, node_id


def test_timezone_option_source_is_per_platform():
    """Timezone offers the curated list on the board and the full list on the web.

    Why this test exists: the e-paper cannot scroll a full IANA list, so the board
    keeps the curated ``timezones_common`` while the web offers the complete
    runtime list -- the single divergence that justified a per-platform option
    source (``webProvider``) instead of two nodes. How a regression manifests:
    dropping ``webProvider`` makes the web fall back to the curated list (missing
    zones), or changing the board ``optionSet`` floods the e-paper with the full
    list.
    """
    node = _catalog().get_node("system.timezone")
    assert node.get("optionSet") == "timezones_common"
    assert node.get("webProvider") == "timezones"


def test_web_only_container_does_not_reach_the_board():
    """The container is web-only so the board never double-renders these rows.

    Why: the board already shows the shared nodes through its own ``system`` menu;
    this container exists only to give the web a layout. How a regression
    manifests: dropping the web-only ``platforms`` would let the board flatten this
    container's rows in addition to its native ones.
    """
    node = _catalog().get_node(_DEVICE_CONTAINER)
    assert applies_to_platform(node, "web") is True
    assert applies_to_platform(node, "board") is False


def test_duplicate_web_only_fields_are_removed():
    """The old ``field.system.*`` duplicates no longer exist.

    Why this test exists: the whole point of the convergence is to end the
    duplication; leaving the old nodes behind (even unused) invites a future edit
    to re-wire the web to them and re-open the drift. How a regression manifests:
    any of the three duplicate ids resolves again.
    """
    catalog = _catalog()
    for dup_id in _DELETED_DUPLICATES:
        assert not catalog.has_node(dup_id), dup_id
