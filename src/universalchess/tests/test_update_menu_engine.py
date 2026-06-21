"""Tests for the data-driven Updates menu (the ``updates`` catalog container).

Background / why these tests exist
----------------------------------
The Updates menu was migrated off the bespoke ``handle_update_menu`` builder onto
the shared engine: the ``updates`` container declares the auto-update toggle, the
channel select, and the check/download/install rows, with the Download and
Install-Pending rows gated by ``visibleWhen``. main.py supplies an ``update``
store over the live UpdateService plus the action handlers. These tests build
from the *real* catalog with a fake store, pinning the row structure, the
mutually exclusive Download/Install-Pending gating, the toggle's icon/label, the
channel select wiring, and that each action row dispatches to its handler --
the guarantees the deleted builder used to provide.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch, resolve_icon


def _update_ctx(*, auto_update=False, channel="stable", available="", has_pending=False,
                local_deb_name=""):
    """Board context mirroring main._build_updates_context over a fake store.

    ``has_download`` is derived exactly as production does (available and not yet
    pending) so the visibility tests exercise the real gating rule. Actions are
    recorded so dispatch wiring can be asserted without running splash flows.
    ``local_deb_name`` backs the Install-Local confirmation's filename label.
    """
    state = {"auto_update": auto_update, "channel": channel, "available": available,
             "has_pending": has_pending, "local_deb_name": local_deb_name}

    def update_get(key):
        if key == "has_download":
            return (not state["has_pending"]) and bool(state["available"])
        return state[key]

    def update_set(key, value):
        state[key] = value

    ctx = BoardMenuContext()
    ctx.register_store("update", update_get, update_set)
    ctx.register_value("auto_update_state",
                       lambda node: "Enabled" if update_get("auto_update") else "Disabled")
    ctx.calls = []
    for action in ("check_updates", "download_update", "install_pending", "install_local",
                   "install_local_confirmed", "cancel"):
        ctx.register_action(action, (lambda a: (lambda: ctx.calls.append(a) or None))(action))
    ctx._state = state
    return ctx


def _rows(**kwargs):
    ctx = _update_ctx(**kwargs)
    return ctx, build_rows("updates", ctx, platform="board", catalog=load_catalog())


def test_up_to_date_hides_download_and_install_pending():
    """With no available and no pending update, only the steady-state rows show.

    Why this test exists: Download and Install-Pending are conditional; when the
    board is current neither should appear, matching the old builder that
    appended neither row. How a regression manifests: a broken ``visibleWhen``
    leaves a dead Download/Install row that does nothing.
    """
    _, rows = _rows(available="", has_pending=False)
    assert [r.key for r in rows] == ["AutoUpdate", "Channel", "CheckNow", "InstallLocal"]


def test_available_update_shows_download_with_version_label():
    """An available (not-yet-downloaded) update shows Download labelled with it.

    Why this test exists: the Download row is gated on ``has_download`` and its
    label binds the available version ("Download\\nv<ver>"); both must track the
    store. How a regression manifests: Download stays hidden when an update is
    available, or shows a bare "Download" without the version.
    """
    _, rows = _rows(available="2.5.0", has_pending=False)
    by_key = {r.key: r for r in rows}
    assert "DownloadUpdate" in by_key
    assert "InstallPending" not in by_key
    assert by_key["DownloadUpdate"].label == "Download\nv2.5.0"


def test_pending_update_replaces_download_with_install_pending():
    """Once an update is pending, Install-Pending shows and Download hides.

    Why this test exists: Download and Install-Pending are mutually exclusive
    (``has_download`` is false once pending); the user must see exactly one
    install path. How a regression manifests: both rows show at once, or neither,
    after a download completes.
    """
    _, rows = _rows(available="2.5.0", has_pending=True)
    keys = {r.key for r in rows}
    assert "InstallPending" in keys
    assert "DownloadUpdate" not in keys


def test_auto_update_toggle_icon_and_label_track_state():
    """The Auto-Update row's icon and label reflect the bound flag and toggling
    it flips the stored value.

    Why this test exists: the toggle must render checked/Enabled vs empty/Disabled
    consistently and actually mutate the setting, replacing the builder's manual
    icon/label/flip. How a regression manifests: the icon/label desync from the
    flag, or selecting the row no longer changes the setting.
    """
    catalog = load_catalog()
    node = catalog.get_node("updates.auto")

    on_ctx = _update_ctx(auto_update=True)
    on_row = {r.key: r for r in build_rows("updates", on_ctx, platform="board", catalog=catalog)}["AutoUpdate"]
    assert on_row.label == "Auto-Update\nEnabled"
    assert resolve_icon(node, on_ctx) == "checkbox_checked"

    off_ctx = _update_ctx(auto_update=False)
    off_row = {r.key: r for r in build_rows("updates", off_ctx, platform="board", catalog=catalog)}["AutoUpdate"]
    assert off_row.label == "Auto-Update\nDisabled"
    assert resolve_icon(node, off_ctx) == "checkbox_empty"

    dispatch(node, off_ctx)
    assert off_ctx._state["auto_update"] is True


def test_channel_row_is_a_select_over_update_channel_set():
    """The Channel row dispatches a select over the shared update_channel set,
    labelled with the active channel's catalog label.

    Why this test exists: the channel list and labels must come from the shared
    update_channel option set (one source with the web), and selecting routes
    through the engine select carrying the radio icons. How a regression
    manifests: a hardcoded channel list returns, or the select loses its store
    binding / icons.
    """
    catalog = load_catalog()
    ctx = _update_ctx(channel="nightly")
    row = {r.key: r for r in build_rows("updates", ctx, platform="board", catalog=catalog)}["Channel"]
    assert row.label == "Channel\nNightly (Development)"

    outcome = dispatch(catalog.get_node("updates.channel"), ctx)
    assert outcome.kind == "select"
    assert outcome.option_set == "update_channel"
    assert (outcome.store, outcome.key) == ("update", "channel")
    assert outcome.selected_icon == "checkbox_checked"
    assert outcome.unselected_icon == "checkbox_empty"


def test_install_local_confirm_container_is_a_yes_no_gate():
    """Install-Local opens a data-driven confirm container, not an inline dialog.

    Why this test exists: the local .deb install used to build its Install/Cancel
    confirmation as imperative IconMenuEntry rows inside update_menu.py. The
    migration moves that gate into the catalog (mirroring system.reset.confirm) so
    the structure is one source of truth. The Yes row must run the dedicated
    ``install_local_confirmed`` action (which installs the discovered package) and
    the No row the shared ``cancel`` (which backs out). How a regression manifests:
    a missing/renamed child or a Yes wired to the wrong action would install on
    cancel or fail to install on confirm.
    """
    catalog = load_catalog()
    assert catalog.child_ids("updates.install_local.confirm") == [
        "updates.install_local.confirm.yes",
        "updates.install_local.confirm.no",
    ]
    yes = catalog.get_node("updates.install_local.confirm.yes")
    no = catalog.get_node("updates.install_local.confirm.no")
    assert yes["type"] == "action" and yes["action"] == "install_local_confirmed"
    assert no["type"] == "action" and no["action"] == "cancel"


def test_install_local_confirm_yes_row_shows_discovered_filename():
    """The confirm's Yes row names the .deb that will be installed.

    Why this test exists: the user must see which package they are about to
    install before confirming a system-modifying action; the filename comes from
    the ``update.local_deb_name`` the discovery step stores. How a regression
    manifests: the label loses its ``{value}`` binding and shows a bare "Install?"
    so the user confirms an unidentified package.
    """
    catalog = load_catalog()
    ctx = _update_ctx(local_deb_name="universal-chess_2.5.0.deb")
    rows = {r.key: r for r in build_rows("updates.install_local.confirm", ctx,
                                         platform="board", catalog=catalog)}
    assert rows["confirm"].label == "Install\nuniversal-chess_2.5.0.deb?"
    assert rows["cancel"].label == "Cancel"


def test_install_local_confirm_rows_dispatch_to_their_handlers():
    """Selecting Yes installs the discovered package; No cancels.

    Why this test exists: the confirm gate is only correct if Yes routes to the
    install action and No to cancel; this pins that wiring through the engine so a
    swap (which would install on cancel) is caught. How a regression manifests:
    the recorded action list below changes.
    """
    catalog = load_catalog()
    yes_ctx = _update_ctx(local_deb_name="x.deb")
    yes_out = dispatch(catalog.get_node("updates.install_local.confirm.yes"), yes_ctx)
    assert yes_out.kind == "action" and yes_out.action == "install_local_confirmed"
    assert yes_ctx.calls == ["install_local_confirmed"]

    no_ctx = _update_ctx(local_deb_name="x.deb")
    no_out = dispatch(catalog.get_node("updates.install_local.confirm.no"), no_ctx)
    assert no_out.kind == "action" and no_out.action == "cancel"
    assert no_ctx.calls == ["cancel"]


def test_action_rows_dispatch_to_their_handlers():
    """Each actionable row routes to its registered action handler.

    Why this test exists: the check/download/install rows are pure action nodes;
    selecting one must invoke exactly its handler (the imperative splash flow in
    production), with no cross-wiring. How a regression manifests: an action key
    typo or swap silently runs the wrong flow or nothing.
    """
    catalog = load_catalog()
    cases = {
        "updates.check": "check_updates",
        "updates.download": "download_update",
        "updates.install_pending": "install_pending",
        "updates.install_local": "install_local",
    }
    for node_id, action in cases.items():
        ctx = _update_ctx(available="1.0.0", has_pending=True)
        outcome = dispatch(catalog.get_node(node_id), ctx)
        # dispatch runs the action through the context and reports it; selecting
        # the row must invoke exactly that handler once.
        assert outcome.kind == "action" and outcome.action == action, node_id
        assert ctx.calls == [action], node_id
