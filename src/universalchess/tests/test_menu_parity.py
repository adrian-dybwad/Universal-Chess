"""Board/web parity guards for the shared menu catalog.

The catalog is the single source both the e-paper board and the React web app
render from. The failure this file guards against is a *parallel web tree*: a
web-only container that re-groups nodes the board reaches through a different
container, so the two platforms drift and every change must be made twice. That
is exactly the bug that hid ``show_board`` from the web (a web-only
``settings.display.web`` mirroring the board's ``settings.display``) and left the
System device selects reachable on the web only via a web-only group.

Invariant (directional, deliberately):
    A web-only container (one that does not apply to the board) must not contain
    any node that DOES apply to the board.

Rationale for the direction: board-only containers legitimately exist -- the
e-paper has device screens (Wi-Fi scan, power confirm, engine manager) with no
web analogue -- so a single-platform container is not itself wrong. The wrong
thing is the *web* inventing its own container around shared nodes instead of
rendering the shared container the board already uses. A shared node must always
be reachable on the web through a shared container, never trapped under a
web-only one. A web-only container over web-only fields (e.g. a card grouping
web-only Software Update fields) is fine and is not flagged.
"""

from universalchess.menus.catalog.loader import load_catalog


def _applies_to_board(node: dict) -> bool:
    """Whether a node renders on the board.

    An absent ``platforms`` means "both platforms" (the catalog default), so
    only an explicit list that omits ``"board"`` makes a node web-only.
    """
    platforms = node.get("platforms")
    return "board" in platforms if platforms else True


def _transitive_descendants(catalog, root_id: str):
    """Yield every node reachable from ``root_id`` via ``children``, once each.

    Guards against a node reached through more than one parent (the catalog is a
    DAG -- e.g. a field shared by two groups) so it is not visited repeatedly and
    a hypothetical cycle cannot loop forever.
    """
    seen: set[str] = set()
    stack = list(catalog.child_ids(root_id))
    while stack:
        child_id = stack.pop()
        if child_id in seen:
            continue
        seen.add(child_id)
        yield catalog.get_node(child_id)
        stack.extend(catalog.child_ids(child_id))


def test_no_web_only_container_holds_a_board_node():
    """A web-only container must not contain any board-renderable node.

    Why this test exists: it pins the board/web convergence and catches the
    "parallel web tree" regression class at the catalog level -- a new
    ``settings.*.web`` (or web-only group) that re-groups shared nodes would trip
    it immediately, before it can silently diverge the two UIs.

    How a regression manifests: a container marked web-only (``platforms``
    without ``"board"``) lists, directly or transitively, a node that still
    applies to the board. That node is then rendered on the web via this web-only
    path while the board reaches it through some other container -- two sources of
    truth for one setting. The failure message names each offending
    container -> node edge so the fix (make the container shared, or move the
    shared node into a shared container) is obvious.
    """
    catalog = load_catalog()
    nodes = catalog.raw_menu()["nodes"]

    web_only_containers = [
        node for node in nodes if node.get("children") and not _applies_to_board(node)
    ]

    violations = [
        f"{container['id']} -> {descendant['id']}"
        for container in web_only_containers
        for descendant in _transitive_descendants(catalog, container["id"])
        if _applies_to_board(descendant)
    ]

    assert not violations, (
        "web-only container(s) hold board-renderable nodes (parallel web tree); "
        "make the container shared or move the shared node into a shared "
        f"container: {violations}"
    )
