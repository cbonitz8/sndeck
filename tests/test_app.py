import json
import httpx
import pytest
from pathlib import Path
from rich.text import Text as RichText
from textual.widgets import Tree, Static, TextArea
from sndeck.app import SndeckApp, LegendScreen
from sndeck.records import pull_record, set_workspace
from sndeck.rest import TableClient
from sndeck.config import Instance
from sndeck.theme import MACCHIATO, LATTE
from sndeck.watcher import ScratchChanged

# Canonical 32-hex set sys_id used by route factories that need a workspace dir.
_SET1 = "1" * 32

INST = Instance("dev", "https://x.service-now.com", "cid",
                "https://x.service-now.com/oauth_token.do", "cbonitz@x")


class FakeToken:
    def access_token(self): return "AT"
    def invalidate(self): pass


def _client(routes):
    def handler(req):
        table = str(req.url.path).rsplit("/", 1)[-1]
        return httpx.Response(200, json={"result": routes(table, dict(req.url.params))})
    return TableClient(INST, FakeToken(), http=httpx.Client(transport=httpx.MockTransport(handler)))


def _make_client_for_app(routes=None):
    if routes is None:
        def routes(table, params):
            if table == "sys_user":
                return [{"sys_id": "U1", "user_name": "cbonitz"}]
            if table == "sys_user_preference":
                return [{"value": "SET1"}]
            if table == "sys_update_set":
                return [{"sys_id": {"value": "SET1"}, "name": {"value": "S"},
                         "state": {"value": "in progress", "display_value": "In progress"},
                         "application": {"display_value": "Global"}}]
            return []
    return _client(routes)


def _routes_factory(scratch):
    a = "a" * 32
    # pull one BR locally inside the set's workspace dir so scan_workspace finds it
    ws = set_workspace(scratch, _SET1, "sn setup")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sys_script" / f"MyBR__{a}"
    folder.mkdir(parents=True)
    folder.joinpath("record.json").write_text(json.dumps(
        {"_meta": {"table": "sys_script", "sys_id": a, "name": "MyBR"}, "script": "x"}))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "x"}))
    folder.joinpath("script.js").write_text("x")

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "sn setup"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [{"name": {"value": f"sys_script_{a}"},
                     "target_name": {"display_value": "MyBR"}, "type": {"display_value": "Update"}}]
        return []
    return routes


@pytest.mark.asyncio
async def test_tree_renders_scope_set_table_file(tmp_path, sn_client):
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        labels = []
        def walk(node):
            labels.append(str(node.label))
            for c in node.children:
                walk(c)
        walk(tree.root)
        joined = " | ".join(labels)
        assert "Global" in joined
        assert "sn setup" in joined
        assert "Business Rules" in joined
        assert "MyBR" in joined


@pytest.mark.asyncio
async def test_app_renders_info_bar(tmp_path):
    app = SndeckApp(_make_client_for_app(), tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#info-bar") is not None
        assert app.query_one("#tree", Tree) is not None
        assert app.theme == MACCHIATO


@pytest.mark.asyncio
async def test_toggle_theme_switches(tmp_path):
    app = SndeckApp(_make_client_for_app(), tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        first = app.theme
        await pilot.press("t")
        await pilot.pause()
        assert app.theme != first


@pytest.mark.asyncio
async def test_toggle_theme_no_refetch(tmp_path):
    """Theme toggle must recolor from _last_model cache — no network call."""
    app = SndeckApp(_make_client_for_app(), tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # After initial fetch, _last_model is populated. Replace client with one
        # that raises on any query to prove toggle doesn't hit the network.
        class BombClient:
            def query(self, *a, **kw):
                raise AssertionError("network hit during theme toggle")
        app._client = BombClient()
        first_theme = app.theme
        # Press 't' — should recolor from cache, not call _client.query
        await pilot.press("t")
        await pilot.pause()
        assert app.theme != first_theme
        assert app._last_model is not None


@pytest.mark.asyncio
async def test_legend_shows_content(tmp_path):
    app = SndeckApp(_make_client_for_app(), tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        # After pressing ?, the modal is the active screen
        legend = app.screen
        assert isinstance(legend, LegendScreen)
        title = legend.query_one("#legend-title", Static)
        assert "Legend" in str(title.content)
        assert legend.query_one(".legend-ok", Static) is not None


@pytest.mark.asyncio
async def test_selecting_file_loads_preview(tmp_path, sn_client):
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        # Find the file leaf node
        target = None
        def walk(n):
            nonlocal target
            if n.id in app._node_files:
                target = n
            for c in n.children:
                walk(c)
        walk(tree.root)
        assert target is not None, "No file node found in tree"
        # Drive the preview directly (NodeHighlighted may not fire in test context)
        await app._show_preview(app._node_files[target.id])
        await pilot.pause()
        body = app.query_one("#preview-body", TextArea)
        assert body.text == "x", f"Expected 'x', got {body.text!r}"
        header = app.query_one("#preview-header", Static)
        assert "MyBR" in str(header.content)


@pytest.mark.asyncio
async def test_apply_ratio_clamps_and_persists(tmp_path, sn_client, monkeypatch):
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._apply_ratio(0.95, persist=True)   # over max -> clamp to 0.85
        from sndeck import state as st
        assert st.load_state().split_ratio == 0.85
        app._apply_ratio(0.05, persist=True)   # under min -> clamp to 0.15
        assert st.load_state().split_ratio == 0.15


def _iter_nodes(node):
    yield node
    for c in node.children:
        yield from _iter_nodes(c)


@pytest.mark.asyncio
async def test_open_browser_builds_record_url(tmp_path, sn_client, monkeypatch):
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda u: opened.setdefault("url", u))
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        # highlight the file node
        tree = app.query_one("#tree", Tree)
        target = next(n for n in _iter_nodes(tree.root) if n.id in app._node_files)
        tree.select_node(target)
        await pilot.pause()
        await pilot.press("o")
        assert opened["url"].endswith(f"/sys_script.do?sys_id={'a'*32}")


@pytest.mark.asyncio
async def test_set_picker_pins_and_persists(tmp_path, sn_client, monkeypatch):
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    def routes(table, params):
        if table == "sys_user": return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference": return [{"value": "SET1"}]
        if table == "sys_update_set":
            if "state=in progress" in params.get("sysparm_query", ""):
                return [{"sys_id": {"value": "SET2"}, "name": {"value": "hotfix"},
                         "state": {"value": "in progress", "display_value": "In progress"},
                         "application": {"display_value": "Global"}}]
            return [{"sys_id": {"value": "SET1"}, "name": {"value": "cur"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml": return []
        return []
    client = sn_client(routes)
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")            # open picker
        await pilot.pause()
        await pilot.press("space")        # pin the highlighted set (SET2)
        await pilot.press("escape")       # close
        await pilot.pause()
        from sndeck import state as st
        assert "SET2" in st.load_state().tracked_sets


def _pinned_routes(current="SET1"):
    """Client routes covering current-user, current-set, and batched states."""
    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": current}]
        if table == "sys_update_set":
            q = params.get("sysparm_query", "")
            # batched states query from the modal
            if "sys_idIN" in q:
                rows = []
                if "SET1" in q:
                    rows.append({"sys_id": {"value": "SET1"}, "name": {"display_value": "In-Prog Set"},
                                 "state": {"value": "in progress", "display_value": "In progress"},
                                 "application": {"display_value": "Global"}})
                if "SET2" in q:
                    rows.append({"sys_id": {"value": "SET2"}, "name": {"display_value": "Done Set"},
                                 "state": {"value": "complete", "display_value": "Complete"},
                                 "application": {"display_value": "Global"}})
                return rows
            # single-set lookups (build_tree / set_current_update_set)
            return [{"sys_id": {"value": "SET1"}, "name": {"value": "In-Prog Set"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"value": "global", "display_value": "Global"}}]
        return []
    return routes


@pytest.mark.asyncio
async def test_switch_set_writes_only_on_confirm(tmp_path, sn_client, monkeypatch):
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    from sndeck import state as st
    st.pin("SET1", name="In-Prog Set")
    writes = []
    def routes(table, params):
        if table == "sys_user_preference" and params.get("sysparm_query", "").startswith("name=sys_update_set^user"):
            return [{"sys_id": "PREF1", "value": "SET1"}]
        r = _pinned_routes()(table, params)
        if table == "sys_update_set" and not r:
            return [{"sys_id": "PREF1", "value": "SET1"}]
        return r
    def on_write(method, table, sys_id, body):
        writes.append((method, table, sys_id, body)); return {"sys_id": "PREF1"}
    client = sn_client(routes, on_write)
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")            # open PinnedSetsScreen
        await pilot.pause()
        from sndeck.app import PinnedSetsScreen, SwitchConfirmScreen
        assert isinstance(app.screen, PinnedSetsScreen)
        assert writes == []               # nothing written yet
        await pilot.press("enter")        # pick highlighted (in-progress) row
        await pilot.pause()
        assert isinstance(app.screen, SwitchConfirmScreen)
        await pilot.press("y")            # confirm -> write
        await pilot.pause()
        assert writes and writes[0][0] == "PATCH"


@pytest.mark.asyncio
async def test_pinned_modal_switches_in_progress_set(tmp_path, monkeypatch):
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    from sndeck import state as st
    st.pin("SET1", name="In-Prog Set")
    writes = []
    def routes(table, params):
        if table == "sys_user_preference" and params.get("sysparm_query", "").startswith("name=sys_update_set^user"):
            return [{"sys_id": "PREF1", "value": "SET1"}]
        return _pinned_routes()(table, params)
    app = SndeckApp(_client(routes), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")            # open PinnedSetsScreen
        await pilot.pause()
        from sndeck.app import PinnedSetsScreen, SwitchConfirmScreen
        assert isinstance(app.screen, PinnedSetsScreen)
        await pilot.press("enter")        # pick highlighted (in-progress) set
        await pilot.pause()
        assert isinstance(app.screen, SwitchConfirmScreen)
        await pilot.press("y")            # confirm -> write
        await pilot.pause()


@pytest.mark.asyncio
async def test_pinned_modal_blocks_completed_set(tmp_path, monkeypatch):
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    from sndeck import state as st
    st.pin("SET2", name="Done Set")
    app = SndeckApp(_client(_pinned_routes()), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        from sndeck.app import PinnedSetsScreen
        await pilot.press("enter")        # completed set -> no switch
        await pilot.pause()
        assert isinstance(app.screen, PinnedSetsScreen)   # stayed on modal, no confirm


@pytest.mark.asyncio
async def test_show_preview_double_call_same_record_no_blank(tmp_path, sn_client):
    """Calling _show_preview twice for the same node (simulating post-push ScratchChanged race)
    must leave field tabs present and correct — no blank tabs area, no crash."""
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        target = None
        def walk(n):
            nonlocal target
            if n.id in app._node_files:
                target = n
            for c in n.children:
                walk(c)
        walk(tree.root)
        assert target is not None, "No file node found in tree"
        node = app._node_files[target.id]

        # First call — mounts tabs
        await app._show_preview(node)
        await pilot.pause()
        tabs_after_first = app.query(".field-tab")
        assert len(tabs_after_first) > 0, "No field tabs after first _show_preview call"

        # Second call with the same node — must not blank the tabs
        await app._show_preview(node)
        await pilot.pause()
        tabs_after_second = app.query(".field-tab")
        assert len(tabs_after_second) == len(tabs_after_first), (
            f"Tab count changed on same-record re-show: "
            f"{len(tabs_after_first)} -> {len(tabs_after_second)}"
        )
        # Verify the expected tab-<key> widget is still present
        key = node.table  # field key for sys_script is 'script'
        expected_tab_id = "tab-script"
        assert app.query_one(f"#{expected_tab_id}") is not None, (
            f"#{expected_tab_id} missing after double _show_preview call"
        )


@pytest.mark.asyncio
async def test_push_writes_only_on_confirm(tmp_path, sn_client):
    a = "a"*32
    ws = set_workspace(tmp_path, _SET1, "cur")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sys_script" / f"BR__{a}"
    folder.mkdir(parents=True)
    folder.joinpath("record.json").write_text(json.dumps(
        {"_meta": {"table": "sys_script", "sys_id": a, "name": "BR"}, "script": "orig"}))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "orig"}))
    folder.joinpath("script.js").write_text("EDITED")   # dirty
    writes = []
    def routes(table, params):
        if table == "sys_user": return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference": return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "cur"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [{"name": {"value": f"sys_script_{a}"},
                     "target_name": {"display_value": "BR"}, "type": {"display_value": "Update"}}]
        if table == "sys_script": return [{"sys_id": a, "script": "orig"}]  # instance == snapshot
        return []
    def on_write(method, table, sys_id, body):
        writes.append((method, table, sys_id, body)); return {"sys_id": a}
    client = sn_client(routes, on_write)
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        leaf = next(n for n in _iter_nodes(tree.root) if n.id in app._node_files)
        tree.select_node(leaf)
        await pilot.pause()
        await pilot.press("P")
        await pilot.pause()
        assert writes == []
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()
        assert writes and writes[0][0] == "PUT" and writes[0][3] == {"script": "EDITED"}


# ── Finding I1: local-only refresh on ScratchChanged — no network ────────────

class BombClient:
    """Raises if any query/get_record is called — used to assert no network."""
    def query(self, *a, **kw):
        raise AssertionError("network hit during local-only refresh")
    def get_record(self, *a, **kw):
        raise AssertionError("network hit during local-only refresh")


@pytest.mark.asyncio
async def test_scratch_changed_no_network(tmp_path, sn_client):
    """on_scratch_changed/_refresh_local must NOT query the instance."""
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Confirm tree was populated from the initial fetch.
        assert app._last_model is not None
        assert app._node_files, "Expected at least one file node after initial load"

        # Swap client for a bomb — any network call will raise.
        app._client = BombClient()

        # Fire ScratchChanged; must not raise and tree must still show records.
        event = ScratchChanged({str(tmp_path / "sys_script" / "x" / "script.js")})
        await app.on_scratch_changed(event)
        await pilot.pause()

        # Tree still has nodes — local refresh preserved structure.
        assert app._node_files, "File nodes disappeared after local refresh"


@pytest.mark.asyncio
async def test_scratch_changed_dirty_badge_appears(tmp_path, sn_client):
    """After editing a local field file, _refresh_local shows the dirty badge."""
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app._node_files, "Expected at least one file node"

        # Find the file node for MyBR and confirm not dirty initially.
        f_node_before = next(iter(app._node_files.values()))
        assert not f_node_before.dirty, "Expected clean node initially"

        # Edit the local script file (in the workspace dir) to make it dirty.
        a = "a" * 32
        ws = set_workspace(tmp_path, _SET1, "sn setup")
        script_path = ws / "sys_script" / f"MyBR__{a}" / "script.js"
        script_path.write_text("EDITED")

        # Swap client, trigger local refresh.
        app._client = BombClient()
        app._refresh_local()
        await pilot.pause()

        # The file node should now show as dirty.
        f_node_after = next(iter(app._node_files.values()))
        assert f_node_after.dirty, "Expected dirty node after local edit + _refresh_local"


# ── Finding I2: preserve selected field across same-record re-show ────────────

@pytest.mark.asyncio
async def test_show_preview_preserves_selected_field(tmp_path, sn_client):
    """Calling _show_preview again for the same record must reload the previously
    selected field, not snap back to the default."""
    # Build a widget with multiple fields so default != client_script.
    a = "a" * 32
    ws = set_workspace(tmp_path, _SET1, "sn setup")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sp_widget" / f"MyWidget__{a}"
    folder.mkdir(parents=True)
    meta = {"_meta": {"table": "sp_widget", "sys_id": a, "name": "MyWidget"},
            "script": "server", "client_script": "client"}
    folder.joinpath("record.json").write_text(json.dumps(meta))
    folder.joinpath(".snapshot.json").write_text(json.dumps(
        {"script": "server", "client_script": "client"}))
    folder.joinpath("script.js").write_text("server")
    folder.joinpath("client_script.js").write_text("client")

    def routes(table, params):
        if table == "sys_user": return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference": return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "sn setup"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [{"name": {"value": f"sp_widget_{a}"},
                     "target_name": {"display_value": "MyWidget"},
                     "type": {"display_value": "Update"}}]
        return []

    client = sn_client(routes)
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app._node_files, "Expected at least one file node"
        node = next(iter(app._node_files.values()))

        # First show — loads default field (script for sp_widget).
        await app._show_preview(node)
        await pilot.pause()
        assert app._selected_field_key == "script"

        # Manually switch to client_script tab.
        app._load_field("client_script")
        await pilot.pause()
        assert app._selected_field_key == "client_script"

        # Re-show same record — must preserve client_script, not snap to script.
        await app._show_preview(node)
        await pilot.pause()
        assert app._selected_field_key == "client_script", (
            f"Expected 'client_script' to be preserved; got {app._selected_field_key!r}"
        )
        body = app.query_one("#preview-body", TextArea)
        assert body.text == "client", f"Expected 'client' in body; got {body.text!r}"


# ── Regression: DuplicateIds on back-to-back record switch ───────────────────

@pytest.mark.asyncio
async def test_show_preview_different_records_same_tab_key_no_duplicate_ids(tmp_path, sn_client):
    """Navigating between two different non-code records (both get tab-record) must
    not raise DuplicateIds — the old tab must be fully removed before the new one mounts."""
    a = "a" * 32
    b = "b" * 32

    ws = set_workspace(tmp_path, _SET1, "sn setup")
    ws.mkdir(parents=True, exist_ok=True)
    for sys_id, name in [(a, "PropA"), (b, "PropB")]:
        folder = ws / "sys_properties" / f"{name}__{sys_id}"
        folder.mkdir(parents=True)
        meta = {"_meta": {"table": "sys_properties", "sys_id": sys_id, "name": name},
                "value": f"val_{name}"}
        folder.joinpath("record.json").write_text(json.dumps(meta))
        folder.joinpath(".snapshot.json").write_text(json.dumps({"value": f"val_{name}"}))

    def routes(table, params):
        if table == "sys_user": return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference": return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "sn setup"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [
                {"name": {"value": f"sys_properties_{a}"},
                 "target_name": {"display_value": "PropA"}, "type": {"display_value": "Update"}},
                {"name": {"value": f"sys_properties_{b}"},
                 "target_name": {"display_value": "PropB"}, "type": {"display_value": "Update"}},
            ]
        return []

    client = sn_client(routes)
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        nodes = list(app._node_files.values())
        assert len(nodes) >= 2, f"Expected >=2 file nodes, got {len(nodes)}"
        node_a = next(n for n in nodes if n.sys_id == a)
        node_b = next(n for n in nodes if n.sys_id == b)

        # Back-to-back show without pause — reproduces the DuplicateIds crash
        await app._show_preview(node_a)
        await app._show_preview(node_b)

        tab_record_widgets = list(app.query("#tab-record"))
        assert len(tab_record_widgets) == 1, (
            f"Expected exactly 1 #tab-record widget, got {len(tab_record_widgets)}"
        )
        header = app.query_one("#preview-header", Static)
        assert "PropB" in str(header.content), (
            f"Expected header to reflect PropB; got {header.content!r}"
        )


# ── Feature 1: Syntax highlighting — _lang_for wiring ────────────────────────

@pytest.mark.asyncio
async def test_lang_for_js_returns_javascript(tmp_path, sn_client):
    """After loading a .js field, body.language must be 'javascript'."""
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        target = None
        def walk(n):
            nonlocal target
            if n.id in app._node_files:
                target = n
            for c in n.children:
                walk(c)
        walk(tree.root)
        assert target is not None, "No file node found in tree"
        node = app._node_files[target.id]
        await app._show_preview(node)
        await pilot.pause()
        body = app.query_one("#preview-body", TextArea)
        assert body.language == "javascript", (
            f"Expected 'javascript', got {body.language!r}"
        )


@pytest.mark.asyncio
async def test_lang_for_record_json_returns_json(tmp_path, sn_client):
    """Loading a record.json non-code record must set body.language to 'json'."""
    a = "a" * 32
    ws = set_workspace(tmp_path, _SET1, "sn setup")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sys_properties" / f"MyProp__{a}"
    folder.mkdir(parents=True)
    meta = {"_meta": {"table": "sys_properties", "sys_id": a, "name": "MyProp"},
            "value": "somevalue"}
    folder.joinpath("record.json").write_text(json.dumps(meta))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"value": "somevalue"}))

    def routes(table, params):
        if table == "sys_user": return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference": return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "sn setup"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [{"name": {"value": f"sys_properties_{a}"},
                     "target_name": {"display_value": "MyProp"},
                     "type": {"display_value": "Update"}}]
        return []

    client = sn_client(routes)
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        node = next(iter(app._node_files.values()))
        await app._show_preview(node)
        await pilot.pause()
        body = app.query_one("#preview-body", TextArea)
        assert body.language == "json", (
            f"Expected 'json', got {body.language!r}"
        )


# ── Feature 2: Keyboard field-tab switching ───────────────────────────────────

def _widget_routes(scratch):
    """Set up a sp_widget with script + client_script on disk. Returns the routes fn."""
    a = "a" * 32
    ws = set_workspace(scratch, _SET1, "sn setup")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sp_widget" / f"MyWidget__{a}"
    folder.mkdir(parents=True)
    meta = {"_meta": {"table": "sp_widget", "sys_id": a, "name": "MyWidget"},
            "script": "server", "client_script": "client"}
    folder.joinpath("record.json").write_text(json.dumps(meta))
    folder.joinpath(".snapshot.json").write_text(json.dumps(
        {"script": "server", "client_script": "client"}))
    folder.joinpath("script.js").write_text("server")
    folder.joinpath("client_script.js").write_text("client")

    def routes(table, params):
        if table == "sys_user": return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference": return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "sn setup"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [{"name": {"value": f"sp_widget_{a}"},
                     "target_name": {"display_value": "MyWidget"},
                     "type": {"display_value": "Update"}}]
        return []
    return routes


@pytest.mark.asyncio
async def test_next_field_key_advances_and_wraps(tmp_path, sn_client):
    """Pressing ] advances the selected field; pressing [ goes back; wrap-around works."""
    client = sn_client(_widget_routes(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        node = next(iter(app._node_files.values()))
        await app._show_preview(node)
        await pilot.pause()

        # Default field for sp_widget is 'script'
        assert app._selected_field_key == "script"
        original_text = app.query_one("#preview-body", TextArea).text

        # Press ] — should move to client_script
        await pilot.press("]")
        await pilot.pause()
        assert app._selected_field_key == "client_script", (
            f"Expected 'client_script' after ]; got {app._selected_field_key!r}"
        )
        after_text = app.query_one("#preview-body", TextArea).text
        assert after_text != original_text, "Body text did not change after ]"

        # Press [ — should return to script
        await pilot.press("[")
        await pilot.pause()
        assert app._selected_field_key == "script", (
            f"Expected 'script' after [; got {app._selected_field_key!r}"
        )
        back_text = app.query_one("#preview-body", TextArea).text
        assert back_text == original_text, "Body did not return to original after ["


@pytest.mark.asyncio
async def test_field_switch_noop_when_no_preview(tmp_path):
    """Pressing [ or ] when no preview is shown must not crash."""
    app = SndeckApp(_make_client_for_app(), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("]")
        await pilot.press("[")
        # No exception = pass


# ── Feature 3: Tree hierarchy level coloring ─────────────────────────────────

@pytest.mark.asyncio
async def test_render_tree_scope_and_table_labels_are_rich_text(tmp_path, sn_client):
    """_render_tree must produce Rich Text labels (not plain str) for scope/table nodes."""
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        tree = app.query_one("#tree", Tree)

        scope_nodes = []
        table_nodes = []
        file_nodes = []

        def walk(node, depth=0):
            if depth == 1:
                scope_nodes.append(node)
            elif depth == 3:
                table_nodes.append(node)
            elif depth == 4:
                file_nodes.append(node)
            for c in node.children:
                walk(c, depth + 1)

        walk(tree.root)

        assert scope_nodes, "Expected at least one scope node"
        assert table_nodes, "Expected at least one table node"
        assert file_nodes, "Expected at least one file leaf node"

        # Scope labels should be Rich Text (styled)
        for sn in scope_nodes:
            assert isinstance(sn.label, RichText), (
                f"Scope label is {type(sn.label).__name__}, expected RichText"
            )

        # Table labels should be Rich Text (styled)
        for tn in table_nodes:
            assert isinstance(tn.label, RichText), (
                f"Table label is {type(tn.label).__name__}, expected RichText"
            )

        # File labels should be Rich Text (styled badges)
        for fn in file_nodes:
            assert isinstance(fn.label, RichText), (
                f"File label is {type(fn.label).__name__}, expected RichText"
            )


# ── Regression: ScratchChanged must NOT wipe preview tabs ─────────────────────

def _widget_with_3_fields(scratch):
    """sp_widget with script + client_script + template — guarantees >=3 tabs."""
    a = "a" * 32
    ws = set_workspace(scratch, _SET1, "sn setup")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sp_widget" / f"MyWidget__{a}"
    folder.mkdir(parents=True)
    meta = {
        "_meta": {"table": "sp_widget", "sys_id": a, "name": "MyWidget"},
        "script": "server",
        "client_script": "client",
        "template": "<div></div>",
    }
    folder.joinpath("record.json").write_text(json.dumps(meta))
    folder.joinpath(".snapshot.json").write_text(json.dumps(
        {"script": "server", "client_script": "client", "template": "<div></div>"}))
    folder.joinpath("script.js").write_text("server")
    folder.joinpath("client_script.js").write_text("client")
    folder.joinpath("template.html").write_text("<div></div>")

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "sn setup"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [{"name": {"value": f"sp_widget_{a}"},
                     "target_name": {"display_value": "MyWidget"},
                     "type": {"display_value": "Update"}}]
        return []
    return routes


@pytest.mark.asyncio
async def test_scratch_changed_does_not_wipe_preview(tmp_path, sn_client):
    """ScratchChanged must update badges in-place; tabs and cursor must survive."""
    client = sn_client(_widget_with_3_fields(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app._node_files, "Expected at least one file node"

        # Select the file node and load its preview.
        tree = app.query_one("#tree", Tree)
        target = next(n for n in _iter_nodes(tree.root) if n.id in app._node_files)
        tree.select_node(target)
        await pilot.pause()

        node = app._node_files[target.id]
        await app._show_preview(node)
        await pilot.pause()

        tabs_before = list(app.query(".field-tab"))
        assert len(tabs_before) >= 2, (
            f"Expected >=2 field tabs before ScratchChanged, got {len(tabs_before)}"
        )
        key_before = app._selected_field_key
        assert key_before is not None, "Expected a selected field key before ScratchChanged"
        cursor_before = tree.cursor_node

        # Fire ScratchChanged — must NOT wipe tabs or move cursor.
        widget_folder = "MyWidget__" + "a" * 32
        event = ScratchChanged({str(tmp_path / "sp_widget" / widget_folder / "script.js")})
        await app.on_scratch_changed(event)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        tabs_after = list(app.query(".field-tab"))
        assert len(tabs_after) == len(tabs_before), (
            f"Tab count changed after ScratchChanged: {len(tabs_before)} -> {len(tabs_after)} "
            "(preview was wiped)"
        )
        assert app._selected_field_key == key_before, (
            f"_selected_field_key changed: {key_before!r} -> {app._selected_field_key!r}"
        )
        assert tree.cursor_node is cursor_before, (
            "Tree cursor moved after ScratchChanged"
        )


# ── Fix: field tabs are content-width and all on-screen ──────────────────────

@pytest.mark.asyncio
async def test_field_tabs_are_content_width_and_on_screen(tmp_path, sn_client):
    """After selecting a multi-field widget, all field tabs must be visible on-screen
    (x + width <= terminal_width) and sit side-by-side (each tab's x > previous tab's x)."""
    client = sn_client(_widget_with_3_fields(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app._node_files, "Expected at least one file node"
        node = next(iter(app._node_files.values()))
        await app._show_preview(node)
        await pilot.pause()

        tabs = list(app.query(".field-tab"))
        assert len(tabs) >= 2, f"Expected >=2 .field-tab widgets, got {len(tabs)}"

        regions = [tab.region for tab in tabs]
        # All tabs must fit within the 100-col terminal
        for i, r in enumerate(regions):
            assert r.x + r.width <= 100, (
                f"Tab {i} ({tabs[i].id!r}) overflows screen: x={r.x} width={r.width}"
            )
        # Tabs must be side-by-side, not stacked at the same x
        for i in range(1, len(regions)):
            assert regions[i].x > regions[i - 1].x, (
                f"Tab {i} (x={regions[i].x}) not to the right of tab {i-1} (x={regions[i-1].x})"
            )


# ── Fix: editor theme matches app theme ───────────────────────────────────────

@pytest.mark.asyncio
async def test_editor_theme_matches_app_theme(tmp_path, sn_client):
    """#preview-body TextArea.theme must be 'github_light' in Latte and 'vscode_dark'
    in Macchiato, and must update live when the theme is toggled."""
    client = sn_client(_widget_routes(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        node = next(iter(app._node_files.values()))
        await app._show_preview(node)
        await pilot.pause()

        body = app.query_one("#preview-body", TextArea)

        # In Macchiato (dark) — must use vscode_dark
        assert app.theme == MACCHIATO
        assert body.theme == "vscode_dark", (
            f"In Macchiato expected 'vscode_dark', got {body.theme!r}"
        )

        # Toggle to Latte (light) — must switch to github_light
        await pilot.press("t")
        await pilot.pause()
        assert app.theme == LATTE
        assert body.theme == "github_light", (
            f"In Latte expected 'github_light', got {body.theme!r}"
        )

        # Toggle back to Macchiato — must return to vscode_dark
        await pilot.press("t")
        await pilot.pause()
        assert app.theme == MACCHIATO
        assert body.theme == "vscode_dark", (
            f"After toggle back expected 'vscode_dark', got {body.theme!r}"
        )


# ── Staging pane + push-all (§17) ────────────────────────────────────────────

from textual.widgets import DataTable


def _dirty_br_routes(scratch, *, sys_id, name, instance_script):
    """One business rule pulled locally with an EDITED script (dirty). The instance
    returns instance_script for the drift check."""
    ws = set_workspace(scratch, _SET1, "cur")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sys_script" / f"{name}__{sys_id}"
    folder.mkdir(parents=True)
    folder.joinpath("record.json").write_text(json.dumps(
        {"_meta": {"table": "sys_script", "sys_id": sys_id, "name": name}, "script": "orig"}))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "orig"}))
    folder.joinpath("script.js").write_text("EDITED")  # dirty vs snapshot

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "cur"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [{"name": {"value": f"sys_script_{sys_id}"},
                     "target_name": {"display_value": name}, "type": {"display_value": "Update"}}]
        if table == "sys_script":
            return [{"sys_id": sys_id, "script": instance_script}]
        return []
    return routes


@pytest.mark.asyncio
async def test_staging_pane_lists_dirty_record(tmp_path, sn_client):
    a = "a" * 32
    client = sn_client(_dirty_br_routes(tmp_path, sys_id=a, name="MyBR", instance_script="orig"))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        header = app.query_one("#staging-header", Static)
        tbl = app.query_one("#staging-table", DataTable)
        assert "1 changed" in str(header.render())
        assert tbl.row_count == 1
        assert f"sys_script:{a}" in app._staging_files


@pytest.mark.asyncio
async def test_staging_row_highlight_loads_preview(tmp_path, sn_client):
    a = "a" * 32
    client = sn_client(_dirty_br_routes(tmp_path, sys_id=a, name="MyBR", instance_script="orig"))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        tbl = app.query_one("#staging-table", DataTable)
        tbl.focus()
        await pilot.pause()
        tbl.move_cursor(row=0)
        await pilot.pause()
        assert app._preview_key == ("sys_script", a)
        body = app.query_one("#preview-body", TextArea)
        assert body.text == "EDITED"


@pytest.mark.asyncio
async def test_push_all_pushes_every_staged_record(tmp_path, sn_client):
    a, b = "a" * 32, "b" * 32
    ws = set_workspace(tmp_path, _SET1, "cur")
    ws.mkdir(parents=True, exist_ok=True)
    for sid, nm in ((a, "BR_A"), (b, "BR_B")):
        folder = ws / "sys_script" / f"{nm}__{sid}"
        folder.mkdir(parents=True)
        folder.joinpath("record.json").write_text(json.dumps(
            {"_meta": {"table": "sys_script", "sys_id": sid, "name": nm}, "script": "orig"}))
        folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "orig"}))
        folder.joinpath("script.js").write_text("EDITED")
    writes = []

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "cur"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [{"name": {"value": f"sys_script_{a}"},
                     "target_name": {"display_value": "BR_A"}, "type": {"display_value": "Update"}},
                    {"name": {"value": f"sys_script_{b}"},
                     "target_name": {"display_value": "BR_B"}, "type": {"display_value": "Update"}}]
        if table == "sys_script":
            return [{"sys_id": a, "script": "orig"}]  # instance == snapshot for both
        return []

    def on_write(method, table, sys_id, body):
        writes.append((method, table, sys_id, body)); return {"sys_id": sys_id}
    client = sn_client(routes, on_write)
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#staging-table", DataTable).row_count == 2
        await pilot.press("P")
        await pilot.pause()
        assert writes == []          # nothing until confirm
        await pilot.press("y")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        patched = {w[2] for w in writes if w[0] == "PUT"}
        assert patched == {a, b}


@pytest.mark.asyncio
async def test_pinned_modal_unpin_removes_tracking_and_clean_files(tmp_path, monkeypatch):
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    from sndeck import state as st
    st.pin("SET2", name="Done Set")     # completed set -> unpin-only
    a = "a" * 32
    # a clean local record that belongs to SET2, in the per-set workspace dir
    ws = set_workspace(tmp_path, "SET2", "Done Set")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sys_script" / f"MyBR__{a}"
    folder.mkdir(parents=True)
    folder.joinpath("record.json").write_text(json.dumps(
        {"_meta": {"table": "sys_script", "sys_id": a, "name": "MyBR"}, "script": "x"}))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "x"}))
    folder.joinpath("script.js").write_text("x")

    app = SndeckApp(_client(_pinned_routes()), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("x")            # unpin the clean completed set
        await pilot.pause()
    assert st.load_state().tracked_sets == []     # untracked
    assert not ws.exists()                        # whole workspace dir deleted


@pytest.mark.asyncio
async def test_pinned_modal_unpin_dirty_prompts_and_keeps_on_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    from sndeck import state as st
    st.pin("SET2", name="Done Set")
    a = "a" * 32
    ws = set_workspace(tmp_path, "SET2", "Done Set")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sys_script" / f"MyBR__{a}"
    folder.mkdir(parents=True)
    folder.joinpath("record.json").write_text(json.dumps(
        {"_meta": {"table": "sys_script", "sys_id": a, "name": "MyBR"}, "script": "EDITED"}))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "x"}))
    folder.joinpath("script.js").write_text("EDITED")   # dirty vs snapshot

    app = SndeckApp(_client(_pinned_routes()), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("x")            # unpin dirty set -> confirm appears
        await pilot.pause()
        from sndeck.app import DeleteFilesConfirmScreen
        assert isinstance(app.screen, DeleteFilesConfirmScreen)
        await pilot.press("escape")       # cancel deletion
        await pilot.pause()
    assert st.load_state().tracked_sets == []     # tracking still removed
    assert ws.exists()                            # dirty workspace preserved on cancel


@pytest.mark.asyncio
async def test_pinned_modal_unpin_dirty_prompts_and_deletes_on_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    from sndeck import state as st
    st.pin("SET2", name="Done Set")
    a = "a" * 32
    ws = set_workspace(tmp_path, "SET2", "Done Set")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sys_script" / f"MyBR__{a}"
    folder.mkdir(parents=True)
    folder.joinpath("record.json").write_text(json.dumps(
        {"_meta": {"table": "sys_script", "sys_id": a, "name": "MyBR"}, "script": "EDITED"}))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "x"}))
    folder.joinpath("script.js").write_text("EDITED")   # dirty vs snapshot

    app = SndeckApp(_client(_pinned_routes()), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("x")            # unpin dirty set -> confirm appears
        await pilot.pause()
        from sndeck.app import DeleteFilesConfirmScreen
        assert isinstance(app.screen, DeleteFilesConfirmScreen)
        await pilot.press("y")            # confirm deletion
        await pilot.pause()
    assert st.load_state().tracked_sets == []     # tracking removed
    assert not ws.exists()                        # whole workspace dir deleted on confirm


@pytest.mark.asyncio
async def test_pinned_modal_unpin_per_set_workspace_dir_deleted_on_clean(tmp_path, monkeypatch):
    """Unpin with a per-set workspace dir (3-level layout) removes the whole workspace dir."""
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    from sndeck import state as st
    a = "a" * 32
    st.pin("SET2", name="Done Set")
    # Create a per-set workspace dir with a clean record inside
    ws = set_workspace(tmp_path, "SET2", "Done Set")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sys_script" / f"MyBR__{a}"
    folder.mkdir(parents=True)
    folder.joinpath("record.json").write_text(json.dumps(
        {"_meta": {"table": "sys_script", "sys_id": a, "name": "MyBR"}, "script": "x"}))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "x"}))
    folder.joinpath("script.js").write_text("x")

    app = SndeckApp(_client(_pinned_routes()), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("x")            # unpin the clean set
        await pilot.pause()
    assert st.load_state().tracked_sets == []     # untracked
    assert not ws.exists()                        # whole workspace dir removed


@pytest.mark.asyncio
async def test_pinned_modal_unpin_per_set_workspace_dir_dirty_warns_cancel_keeps(tmp_path, monkeypatch):
    """Unpin with a dirty per-set workspace dir shows confirm screen; cancel preserves workspace."""
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))
    from sndeck import state as st
    a = "a" * 32
    st.pin("SET2", name="Done Set")
    # Create a per-set workspace dir with a dirty record inside
    ws = set_workspace(tmp_path, "SET2", "Done Set")
    ws.mkdir(parents=True, exist_ok=True)
    folder = ws / "sys_script" / f"MyBR__{a}"
    folder.mkdir(parents=True)
    folder.joinpath("record.json").write_text(json.dumps(
        {"_meta": {"table": "sys_script", "sys_id": a, "name": "MyBR"}, "script": "EDITED"}))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "x"}))
    folder.joinpath("script.js").write_text("EDITED")   # dirty vs snapshot

    app = SndeckApp(_client(_pinned_routes()), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("x")            # unpin dirty set -> confirm appears
        await pilot.pause()
        from sndeck.app import DeleteFilesConfirmScreen
        assert isinstance(app.screen, DeleteFilesConfirmScreen)
        await pilot.press("escape")       # cancel deletion
        await pilot.pause()
    assert st.load_state().tracked_sets == []     # tracking removed
    assert ws.exists()                            # workspace dir preserved on cancel


@pytest.mark.asyncio
async def test_pin_downloads_only_new_set_not_existing_tracked(tmp_path, monkeypatch):
    """Pinning SET9 must pull SET9's records only — NOT the already-tracked SETOLD's records.

    The discriminating assertion is the second one: under the old pull-all-tracked
    behaviour _pull_sets would iterate every tracked set (SETOLD + SET9) and SETOLD's
    record (sys_script_<b>) would appear in ``pulled``.  Under the current code only
    SET9 is passed to _pull_sets, so only <a> is ever fetched.
    """
    monkeypatch.setenv("SNDECK_STATE", str(tmp_path / "state.json"))

    # Pre-seed SETOLD as an already-tracked set BEFORE the app starts.
    from sndeck import state as st
    st.pin("SETOLD", name="Old Set")

    pulled = []
    a = "a" * 32   # sys_id for SET9's record
    b = "b" * 32   # sys_id for SETOLD's record

    def routes(table, params):
        q = params.get("sysparm_query", "")
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": ""}]
        if table == "sys_update_set":
            if "sys_idIN" in q:
                # batched state lookup for tracked sets (build_tree / pinned modal)
                rows = []
                if "SETOLD" in q:
                    rows.append({"sys_id": {"value": "SETOLD"}, "name": {"display_value": "Old Set"},
                                 "state": {"value": "in progress", "display_value": "In progress"},
                                 "application": {"display_value": "Global"}})
                if "SET9" in q:
                    rows.append({"sys_id": {"value": "SET9"}, "name": {"display_value": "New Set"},
                                 "state": {"value": "in progress", "display_value": "In progress"},
                                 "application": {"display_value": "Global"}})
                return rows
            if "state=in progress" in q:
                # list_update_sets in SetPickerScreen: offer SET9 to pin
                return [{"sys_id": {"value": "SET9"}, "name": {"value": "New Set"},
                         "state": {"value": "in progress", "display_value": "In progress"},
                         "application": {"value": "", "display_value": "Global"}}]
            # single-set lookup (build_tree current set, etc.)
            return [{"sys_id": {"value": "SETOLD"}, "name": {"value": "Old Set"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            # Route by update_set query param so each set returns its own record.
            # update_set_records queries with display_value=false, so name is a plain string.
            if "update_set=SET9" in q:
                return [{"name": "sys_script_" + a}]
            if "update_set=SETOLD" in q:
                return [{"name": "sys_script_" + b}]
            return []
        if table == "sys_script":
            # Record which sys_ids were pulled so we can assert selectivity.
            pulled.append(q)
            if b in q:
                return [{"sys_id": b, "name": "OldBR", "script": "y", "sys_name": "OldBR"}]
            return [{"sys_id": a, "name": "NewBR", "script": "x", "sys_name": "NewBR"}]
        return []

    app = SndeckApp(_client(routes), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")            # open SetPickerScreen
        await pilot.pause()
        await pilot.press("space")        # pin SET9
        await pilot.press("escape")       # close -> triggers pull of newly-pinned SET9 only
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert "SET9" in st.load_state().tracked_sets
    # SET9's record must have been fetched.
    assert any(a in q for q in pulled), f"SET9 record ({a!r}) was never pulled; pulled={pulled}"
    # SETOLD's record must NOT have been fetched — this is the discriminating assertion.
    # Under the old pull-all-tracked behaviour SETOLD would also be iterated, fetching <b>.
    assert not any(b in q for q in pulled), (
        f"SETOLD record ({b!r}) was pulled, but only newly-pinned sets should be fetched; "
        f"pulled={pulled}"
    )


@pytest.mark.asyncio
async def test_push_all_skips_drifted_but_pushes_clean(tmp_path, sn_client):
    a, b = "a" * 32, "b" * 32  # a is clean-on-instance, b drifted on instance
    ws = set_workspace(tmp_path, _SET1, "cur")
    ws.mkdir(parents=True, exist_ok=True)
    for sid, nm in ((a, "BR_A"), (b, "BR_B")):
        folder = ws / "sys_script" / f"{nm}__{sid}"
        folder.mkdir(parents=True)
        folder.joinpath("record.json").write_text(json.dumps(
            {"_meta": {"table": "sys_script", "sys_id": sid, "name": nm}, "script": "orig"}))
        folder.joinpath(".snapshot.json").write_text(json.dumps({"script": "orig"}))
        folder.joinpath("script.js").write_text("EDITED")
    writes = []

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": _SET1}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": _SET1}, "name": {"value": "cur"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_update_xml":
            return [{"name": {"value": f"sys_script_{a}"},
                     "target_name": {"display_value": "BR_A"}, "type": {"display_value": "Update"}},
                    {"name": {"value": f"sys_script_{b}"},
                     "target_name": {"display_value": "BR_B"}, "type": {"display_value": "Update"}}]
        if table == "sys_script":
            sid = params.get("sysparm_query", "").split("sys_id=", 1)[-1].split("^", 1)[0]
            # a matches snapshot ("orig"); b drifted ("changed remotely")
            return [{"sys_id": a, "script": "orig"}] if sid == a \
                else [{"sys_id": b, "script": "changed remotely"}]
        return []

    def on_write(method, table, sys_id, body):
        writes.append((method, table, sys_id, body)); return {"sys_id": sys_id}
    client = sn_client(routes, on_write)
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("P")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        patched = {w[2] for w in writes if w[0] == "PUT"}
        assert patched == {a}        # clean one pushed, drifted one skipped


# ── Regression I1: _refresh_local preserves batch SetNode fields ──────────────

from sndeck.tree import FileNode, TableNode, SetNode, ScopeNode, TreeModel


# ── Task 6: tracked/local-only badge tests ─────────────────────────────────────

@pytest.fixture
def app(tmp_path):
    """Minimal sync SndeckApp for badge/unit tests — no async run needed."""
    return SndeckApp(_make_client_for_app(), tmp_path)


def _fn(**kw):
    base = dict(table="sys_script_include", sys_id="a"*32, name="X",
                in_current_set=True, tracked=True, local=True, dirty=False, record_path=None)
    base.update(kw); return FileNode(**base)


def test_badges_local_only(app):
    b = app._badges(_fn(tracked=False, local=True))
    assert "◌" in b            # local only — not in set yet

def test_badges_tracked_not_pulled(app):
    b = app._badges(_fn(tracked=True, local=False))
    assert "◇" in b

def test_badges_tracked_local(app):
    b = app._badges(_fn(tracked=True, local=True))
    assert "✓" in b


def test_styled_badges_local_only_leading_glyph(app):
    t = app._styled_badges(_fn(tracked=False, local=True))
    assert isinstance(t, RichText)
    assert t.plain.startswith("◌")    # staged, not yet a set member

def test_styled_badges_tracked_not_pulled_leading_glyph(app):
    t = app._styled_badges(_fn(tracked=True, local=False))
    assert isinstance(t, RichText)
    assert t.plain.startswith("◇")    # in set, not pulled

def test_styled_badges_tracked_local_leading_glyph(app):
    t = app._styled_badges(_fn(tracked=True, local=True))
    assert isinstance(t, RichText)
    assert t.plain.startswith("✓")    # tracked · local


# ── Switching (relationship-blind) + push-path scope routing ─────────────────

def test_switch_is_relationship_blind(app, monkeypatch):
    """Switching to ANY set — base, member, or standalone — just points the chosen
    set's prefs at it (set_current_update_set) and aligns its scope. There is no
    'activate the batch' step: batch membership is a commit-time grouping, not a
    current-set concept, so switching never touches other sets' pointers."""
    calls = {}
    monkeypatch.setattr("sndeck.app.set_current_update_set",
                        lambda c, u, sid: calls.setdefault("switched", sid))
    scope_calls = []
    monkeypatch.setattr("sndeck.app.set_current_application",
                        lambda c, u, scope: scope_calls.append(scope))
    app._activate_or_switch(set_sys_id="6"*32, set_name="scaffold", scope="x_scope")
    assert calls.get("switched") == "6"*32
    assert scope_calls == ["x_scope"], "must align the active scope to the chosen set's scope"
    # activate_batch no longer exists — switching is a two-call operation, nothing more.
    assert not hasattr(__import__("sndeck.app", fromlist=["x"]), "activate_batch")


def test_scope_for_set_resolves_nested_member(app):
    """_do_switch resolves scope via _scope_for_set, which must find a set at ANY
    depth (top-level or nested batch member) so a scoped member switches into its
    own scope, not a global fallback."""
    member = SetNode(sys_id="C"*32, name="child", state="in progress", is_current=False,
                     tables=[], scope="x_child", is_base=False, members=[])
    base = SetNode(sys_id="B"*32, name="base", state="in progress", is_current=True,
                   tables=[], scope="global", is_base=True, members=[member])
    app._last_model = TreeModel([ScopeNode("G", [base])], current_set=None)
    assert app._scope_for_set("C"*32) == "x_child"
    assert app._scope_for_set("B"*32) == "global"
    assert app._scope_for_set("Z"*32) is None


@pytest.mark.asyncio
async def test_batch_member_renders_above_base_files_and_is_marked(tmp_path):
    """A batch base can hold both its own files AND nested member update sets
    (e.g. 'my app phase 1 scaffold'). The nested member must render at the
    VERY TOP of the base node — above the base's own tables/files — and be visibly
    marked as an update set so it doesn't read as just another table row."""
    a = "a" * 32
    base_file = FileNode("sys_script", a, "BaseBR", in_current_set=True, tracked=True,
                         local=False, dirty=False, record_path=None)
    member = SetNode(sys_id="C"*32, name="child scaffold", state="in progress",
                     is_current=False, tables=[], scope="x_scope", is_base=False, members=[])
    base = SetNode(sys_id="B"*32, name="phase 1 scaffold", state="in progress",
                   is_current=True,
                   tables=[TableNode("sys_script", "Business Rules", [base_file])],
                   scope="global", is_base=True, members=[member])
    model = TreeModel([ScopeNode("Global", [base])], current_set=None)

    app = SndeckApp(_make_client_for_app(), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._render_tree(model)
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        scope_node = tree.root.children[0]
        base_node = scope_node.children[0]
        child_labels = [str(c.label) for c in base_node.children]

        member_idx = next((i for i, l in enumerate(child_labels) if "child scaffold" in l), None)
        table_idx = next((i for i, l in enumerate(child_labels) if "Business Rules" in l), None)
        assert member_idx is not None, f"member update set not rendered; got {child_labels}"
        assert table_idx is not None, f"base table not rendered; got {child_labels}"
        assert member_idx < table_idx, (
            f"member update set must render above the base's own files; got {child_labels}")
        assert "UPDATE SET" in child_labels[member_idx], (
            f"member must be visibly marked as an update set; got {child_labels[member_idx]!r}")


@pytest.mark.asyncio
async def test_do_push_all_aligns_scope_per_distinct_scope(tmp_path, sn_client, monkeypatch):
    """_do_push_all must call set_current_application once per distinct scope that
    differs from the starting aligned_scope, and must NOT re-call it for records
    already in the aligned scope.

    Setup: two staged records — BR_A in 'global', BR_B in 'scopeX'.
    Starting apps.current_app pref = 'global' (i.e. aligned to scope_a from the start).
    Expected: set_current_application called exactly once — for scope_b only.
    """
    from sndeck.sync import PushPlan
    a, b = "a" * 32, "b" * 32
    scope_a, scope_b = "global", "scopeX"

    # Build a model so scope_for_record can resolve each record's scope.
    f_a = FileNode("sys_script", a, "BR_A", in_current_set=True, tracked=True,
                   local=True, dirty=True, record_path=tmp_path / "fake_a")
    f_b = FileNode("sys_script", b, "BR_B", in_current_set=True, tracked=True,
                   local=True, dirty=True, record_path=tmp_path / "fake_b")
    set_a = SetNode(sys_id="S"*32, name="Set A", state="in progress", is_current=True,
                    tables=[TableNode("sys_script", "Business Rules", [f_a])],
                    scope=scope_a, is_base=False, members=[])
    set_b = SetNode(sys_id="T"*32, name="Set B", state="in progress", is_current=False,
                    tables=[TableNode("sys_script", "Business Rules", [f_b])],
                    scope=scope_b, is_base=False, members=[])
    model = TreeModel([ScopeNode("All", [set_a, set_b])], current_set=None)

    # Routes: user pref returns scope_a as starting apps.current_app.
    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": scope_a}]
        return []

    client = sn_client(routes)
    app = SndeckApp(client, tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._last_model = model

        # Monkeypatch build_push_plan to return a stub plan per sys_id.
        def fake_build_push_plan(client, path):
            path = str(path)
            if "fake_a" in path:
                return PushPlan("sys_script", a, "BR_A", [], [], False)
            return PushPlan("sys_script", b, "BR_B", [], [], False)

        monkeypatch.setattr("sndeck.push.build_push_plan", fake_build_push_plan)
        monkeypatch.setattr("sndeck.push.apply_push", lambda client, plan: None)
        monkeypatch.setattr("sndeck.push.pull_record", lambda *a, **kw: None)

        scope_align_calls = []
        monkeypatch.setattr("sndeck.push.set_current_application",
                            lambda c, u, scope: scope_align_calls.append(scope))
        pointer_calls = []
        monkeypatch.setattr("sndeck.push.set_scope_pointer",
                            lambda c, u, scope, sid: pointer_calls.append((scope, sid)))

        items = [
            ("BR_A", "sys_script", str(tmp_path / "fake_a")),
            ("BR_B", "sys_script", str(tmp_path / "fake_b")),
        ]
        app._do_push_all(items)
        await app.workers.wait_for_complete()
        await pilot.pause()

    # set_current_application called exactly once — only for scope_b (scope_a already aligned).
    assert scope_align_calls == [scope_b], (
        f"Expected set_current_application called once with {scope_b!r}; "
        f"got {scope_align_calls!r}"
    )
    # Each record is routed to its OWNING set's scope pointer, per record.
    assert pointer_calls == [(scope_a, "S"*32), (scope_b, "T"*32)], (
        f"Expected per-record scope-pointer routing to each owning set; got {pointer_calls!r}"
    )



@pytest.mark.asyncio
async def test_do_push_all_routes_same_scope_members_to_own_sets(tmp_path, sn_client, monkeypatch):
    """Regression: a batch with TWO Global members. Pushing a record
    from each must point the single updateSetForScopeglobal pointer at that record's
    OWN set right before its push — so each lands in its own member, not whichever
    member happened to win a one-shot 'activate the batch'."""
    from sndeck.sync import PushPlan
    a, b = "a"*32, "b"*32
    base_id, member_id = "P"*32, "M"*32

    fa = FileNode("sys_script", a, "A", in_current_set=True, tracked=True,
                  local=True, dirty=True, record_path=tmp_path / "fa")
    fb = FileNode("sys_script", b, "B", in_current_set=False, tracked=True,
                  local=True, dirty=True, record_path=tmp_path / "fb")
    member = SetNode(sys_id=member_id, name="form inbox refresh", state="in progress",
                     is_current=False, tables=[TableNode("sys_script", "Business Rules", [fb])],
                     scope="global", is_base=False, members=[])
    base = SetNode(sys_id=base_id, name="phase 1 scaffold", state="in progress",
                   is_current=True, tables=[TableNode("sys_script", "Business Rules", [fa])],
                   scope="global", is_base=True, members=[member])
    model = TreeModel([ScopeNode("Global", [base])], current_set=None)

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": "global"}]   # already aligned to global
        return []

    client = sn_client(routes)
    app = SndeckApp(client, tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._last_model = model

        def fake_build_push_plan(client, path):
            if "fa" in str(path):
                return PushPlan("sys_script", a, "A", [], [], False)
            return PushPlan("sys_script", b, "B", [], [], False)

        monkeypatch.setattr("sndeck.push.build_push_plan", fake_build_push_plan)
        monkeypatch.setattr("sndeck.push.apply_push", lambda client, plan: None)
        monkeypatch.setattr("sndeck.push.pull_record", lambda *a, **kw: None)
        align_calls = []
        monkeypatch.setattr("sndeck.push.set_current_application",
                            lambda c, u, scope: align_calls.append(scope))
        pointer_calls = []
        monkeypatch.setattr("sndeck.push.set_scope_pointer",
                            lambda c, u, scope, sid: pointer_calls.append((scope, sid)))

        app._do_push_all([
            ("A", "sys_script", str(tmp_path / "fa")),
            ("B", "sys_script", str(tmp_path / "fb")),
        ])
        await app.workers.wait_for_complete()
        await pilot.pause()

    # Both records are global, so the scope pointer must be re-aimed per record.
    assert pointer_calls == [("global", base_id), ("global", member_id)], (
        f"each same-scope record must be routed to its own set; got {pointer_calls!r}")
    # Already aligned to global -> no scope switch needed.
    assert align_calls == [], f"no scope realignment expected; got {align_calls!r}"


@pytest.mark.asyncio
async def test_refresh_local_preserves_batch_setnode_fields(tmp_path):
    """_refresh_local must not wipe is_base / members on a batch base SetNode.

    Reproduces I1: the old code reconstructed SetNode positionally, silently
    defaulting scope='global', is_base=False, members=[].
    """
    _BASE = "b" * 32
    _CHILD = "c" * 32

    child_node = SetNode(
        sys_id=_CHILD, name="child set", state="in progress",
        is_current=False, tables=[], scope="global", is_base=False, members=[],
    )
    f = FileNode("sys_script", "a" * 32, "MyBR",
                 in_current_set=True, tracked=True, local=False,
                 dirty=False, record_path=None)
    base_node = SetNode(
        sys_id=_BASE, name="base set", state="in progress",
        is_current=True, tables=[TableNode("sys_script", "Business Rules", [f])],
        scope="global", is_base=True, members=[child_node],
    )
    model = TreeModel([ScopeNode("Global", [base_node])], current_set=None)

    app = SndeckApp(_make_client_for_app(), tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Inject the batch model and trigger a local refresh (no network call)
        app._last_model = model
        app._refresh_local()
        await pilot.pause()

        rebuilt = app._last_model.scopes[0].sets[0]
        assert rebuilt.is_base is True, (
            f"is_base was erased by _refresh_local; got {rebuilt.is_base!r}"
        )
        assert rebuilt.members == [child_node], (
            f"members were erased by _refresh_local; got {rebuilt.members!r}"
        )


# ── Task 9: pull-arbitrary-record (_pull_one) ─────────────────────────────────

def test_pull_one_lands_in_current_set_workspace(app, tmp_path, monkeypatch):
    from sndeck import app as appmod
    monkeypatch.setattr(appmod, "current_user", lambda c: type("U", (), {"sys_id": "u", "user_name": "cb"})())
    monkeypatch.setattr(appmod, "current_update_set", lambda c, u: type("S", (), {"sys_id": "5"*32, "name": "talent"})())
    pulled = {}
    monkeypatch.setattr(appmod, "pull_record",
                        lambda c, t, s, d: pulled.update(dir=str(d), table=t, sys_id=s))
    app._scratch = tmp_path
    app._pull_one("sys_script_include", "a"*32)
    assert pulled["dir"].endswith("talent__" + "5"*32)


# ── Task 2: Staging pane reads from disk ──────────────────────────────────────

@pytest.mark.asyncio
async def test_staging_shows_disk_record_without_refresh(tmp_path, monkeypatch):
    import json
    from sndeck.records import set_workspace
    import sndeck.records as records
    from sndeck.watcher import ScratchChanged
    from textual.widgets import DataTable, Static

    app = SndeckApp(_make_client_for_app(), str(tmp_path), theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Write a NEW dirty record straight to disk — never seen by the network model.
        ws = set_workspace(tmp_path, "d" * 32, "Live Set")
        folder = ws / "sys_script_include" / "NewGuy__f1"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "record.json").write_text(json.dumps(
            {"_meta": {"table": "sys_script_include", "sys_id": "f1", "name": "NewGuy"}}),
            encoding="utf-8")
        # Force is_dirty True for this folder only.
        monkeypatch.setattr(records, "is_dirty", lambda p: str(p) == str(folder))
        # Simulate a watcher tick.
        app.post_message(ScratchChanged({str(folder)}))
        await pilot.pause()
        header = app.query_one("#staging-header", Static)
        tbl = app.query_one("#staging-table", DataTable)
        assert tbl.row_count >= 1
        assert "1 changed" in str(header.render())


# ── Task 3: Reconcile preserves expansion and cursor ─────────────────────────

@pytest.mark.asyncio
async def test_reconcile_preserves_expansion_and_cursor_same_structure(tmp_path, sn_client):
    from textual.widgets import Tree
    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        # Collapse a set node, move cursor to a specific leaf.
        first_leaf = next((n for n in SndeckApp._iter_tree_nodes(tree.root)
                           if not n.children and n.data and n.data[0] == "file"), None)
        assert first_leaf is not None
        tree.select_node(first_leaf)
        await pilot.pause()
        cursor_before = tree.cursor_node.data[1].sys_id
        node_ids_before = [n.id for n in SndeckApp._iter_tree_nodes(tree.root)]

        # Re-render the SAME model (no structural change).
        app._render_tree(app._last_model)
        await pilot.pause()

        node_ids_after = [n.id for n in SndeckApp._iter_tree_nodes(tree.root)]
        # Same node objects reused → same ids, not a rebuilt tree.
        assert node_ids_after == node_ids_before
        assert tree.cursor_node is not None
        assert tree.cursor_node.data[1].sys_id == cursor_before


# ── Task 3: Push-all sources from disk ───────────────────────────────────────

@pytest.mark.asyncio
async def test_push_confirm_lists_disk_records(tmp_path, monkeypatch):
    import sndeck.records as records
    from sndeck.app import PushAllConfirmScreen

    app = SndeckApp(_make_client_for_app(), str(tmp_path), theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Write a NEW dirty record straight to disk — never seen by the network model.
        ws = set_workspace(tmp_path, "d" * 32, "Live Set")
        folder = ws / "sys_script" / "Pushme__p1"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "record.json").write_text(json.dumps(
            {"_meta": {"table": "sys_script", "sys_id": "p1", "name": "Pushme"}}),
            encoding="utf-8")
        # Force is_dirty True for this folder only.
        monkeypatch.setattr(records, "is_dirty", lambda p: str(p) == str(folder))

        captured = {}
        monkeypatch.setattr(app, "push_screen",
                            lambda screen, cb=None: captured.setdefault("screen", screen))
        app.action_push()
        await pilot.pause()
        assert "screen" in captured, "action_push did not open confirm screen (hit 'Nothing to push')"
        assert isinstance(captured["screen"], PushAllConfirmScreen)
        # The disk-only record must be in the confirm list (_records holds [(name, table), ...]).
        assert any("Pushme" in str(r) for r in captured["screen"]._records)


@pytest.mark.asyncio
async def test_reconcile_adds_and_removes_file_leaf(tmp_path, sn_client):
    from textual.widgets import Tree
    from sndeck.tree import TreeModel, ScopeNode, SetNode, TableNode, FileNode

    client = sn_client(_routes_factory(tmp_path))
    app = SndeckApp(client, tmp_path, theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        base = app._last_model
        assert base is not None and base.scopes

        # Build a model identical to base but with one extra file leaf appended to the
        # first table of the first set of the first scope.
        scope0 = base.scopes[0]; set0 = scope0.sets[0]; tbl0 = set0.tables[0]
        new_file = FileNode(tbl0.table, "zzz_new", "ZZZ New", in_current_set=True,
                            tracked=True, local=False, dirty=False, record_path=None)
        new_tbl = TableNode(tbl0.table, tbl0.label, list(tbl0.files) + [new_file])
        new_set = SetNode(set0.sys_id, set0.name, set0.state, set0.is_current,
                          [new_tbl] + list(set0.tables[1:]), scope=set0.scope,
                          is_base=set0.is_base, members=set0.members)
        new_scope = ScopeNode(scope0.name, [new_set] + list(scope0.sets[1:]))
        added_model = TreeModel([new_scope] + list(base.scopes[1:]), base.current_set)

        leaves_before = sum(1 for n in SndeckApp._iter_tree_nodes(tree.root)
                            if n.data and n.data[0] == "file")
        # Capture the actual TreeNode OBJECT of the first set. A full rebuild calls
        # tree.clear() (which resets the id counter to 0, so node .id would be reused
        # and NOT prove anything) and constructs brand-new TreeNode objects. A surgical
        # reconcile keeps the same object, so object identity is the real discriminator.
        set_node_before = next(n for n in SndeckApp._iter_tree_nodes(tree.root)
                               if n.data and n.data[0] == "set")
        set_obj_id_before = id(set_node_before)

        app._render_tree(added_model)
        await pilot.pause()

        leaves_after = sum(1 for n in SndeckApp._iter_tree_nodes(tree.root)
                           if n.data and n.data[0] == "file")
        assert leaves_after == leaves_before + 1
        assert any(n.data[1].sys_id == "zzz_new" for n in SndeckApp._iter_tree_nodes(tree.root)
                   if n.data and n.data[0] == "file")
        set_node_after = next(n for n in SndeckApp._iter_tree_nodes(tree.root)
                              if n.data and n.data[0] == "set")
        assert id(set_node_after) == set_obj_id_before   # same node object reused, not rebuilt

        # Now remove it again — back to base.
        app._render_tree(base)
        await pilot.pause()
        leaves_final = sum(1 for n in SndeckApp._iter_tree_nodes(tree.root)
                           if n.data and n.data[0] == "file")
        assert leaves_final == leaves_before


# ── Task 5: _reconcile_set relabel (stale CURRENT marker) ────────────────────

from contextlib import asynccontextmanager


@pytest.fixture
def make_model():
    """Factory: returns a TreeModel with one scope containing TWO sets (set A current,
    set B not), each with one table and one file. Pure in-memory, no network."""
    from sndeck.tree import TreeModel, ScopeNode, SetNode, TableNode, FileNode
    _SID_A = "a" * 32
    _SID_B = "b" * 32

    def _make():
        f_a = FileNode("sys_script", "fa", "BRA",
                       in_current_set=True, tracked=True, local=True,
                       dirty=False, record_path=None)
        f_b = FileNode("sys_script", "fb", "BRB",
                       in_current_set=False, tracked=True, local=True,
                       dirty=False, record_path=None)
        set_a = SetNode(sys_id=_SID_A, name="Set A", state="in progress",
                        is_current=True,
                        tables=[TableNode("sys_script", "Business Rules", [f_a])],
                        scope="global", is_base=False, members=[])
        set_b = SetNode(sys_id=_SID_B, name="Set B", state="in progress",
                        is_current=False,
                        tables=[TableNode("sys_script", "Business Rules", [f_b])],
                        scope="global", is_base=False, members=[])
        return TreeModel(scopes=[ScopeNode("Global", [set_a, set_b])],
                         current_set=set_a)
    return _make


@pytest.fixture
def app_with_model(tmp_path):
    """Context-manager factory: starts SndeckApp, force-renders the given model
    via a full rebuild (clears _last_model so reconcile falls back), then yields pilot."""
    @asynccontextmanager
    async def _cm(model):
        app = SndeckApp(_make_client_for_app(), tmp_path)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            # Force full rebuild for the initial render so tree state is clean.
            app._last_model = None
            app._render_tree(model)
            await pilot.pause()
            yield pilot
    return _cm


@pytest.mark.asyncio
async def test_reconcile_moves_current_marker(make_model, app_with_model):
    """Switching is_current from one set to another relabels nodes in place."""
    from sndeck.tree import SetNode, ScopeNode, TreeModel

    base = make_model()
    scope = base.scopes[0]
    if len(scope.sets) < 2:
        pytest.skip("Need at least 2 sets to test current-marker move")

    old_current_idx = next(i for i, s in enumerate(scope.sets) if s.is_current)
    new_current_idx = (old_current_idx + 1) % len(scope.sets)

    async with app_with_model(base) as pilot:
        app = pilot.app
        tree = app.query_one(Tree)

        set_nodes_before = {
            n.data[1].sys_id: n
            for n in SndeckApp._iter_tree_nodes(tree.root)
            if n.data and n.data[0] == "set"
        }

        old_set = scope.sets[old_current_idx]
        new_set = scope.sets[new_current_idx]
        new_old_set = SetNode(
            sys_id=old_set.sys_id, name=old_set.name, state=old_set.state,
            is_current=False, tables=old_set.tables, scope=old_set.scope,
            is_base=old_set.is_base, members=old_set.members,
        )
        new_new_set = SetNode(
            sys_id=new_set.sys_id, name=new_set.name, state=new_set.state,
            is_current=True, tables=new_set.tables, scope=new_set.scope,
            is_base=new_set.is_base, members=new_set.members,
        )
        new_sets = list(scope.sets)
        new_sets[old_current_idx] = new_old_set
        new_sets[new_current_idx] = new_new_set
        new_scope = ScopeNode(name=scope.name, sets=new_sets)
        new_scopes = list(base.scopes)
        new_scopes[0] = new_scope
        new_model = TreeModel(scopes=new_scopes, current_set=new_new_set)

        app._render_tree(new_model)
        await pilot.pause()

        set_nodes_after = {
            n.data[1].sys_id: n
            for n in SndeckApp._iter_tree_nodes(tree.root)
            if n.data and n.data[0] == "set"
        }

        # Same node objects → reconcile path, not full rebuild
        assert set_nodes_before[old_set.sys_id] is set_nodes_after[old_set.sys_id], \
            "old-current set node should be the same object (reconcile path)"
        assert set_nodes_before[new_set.sys_id] is set_nodes_after[new_set.sys_id], \
            "new-current set node should be the same object (reconcile path)"

        old_label = str(set_nodes_after[old_set.sys_id].label)
        new_label = str(set_nodes_after[new_set.sys_id].label)
        assert "CURRENT" not in old_label, \
            f"Old-current set should no longer show CURRENT, got: {old_label!r}"
        assert "CURRENT" in new_label, \
            f"New-current set should show CURRENT, got: {new_label!r}"


@pytest.mark.asyncio
async def test_reconcile_add_remove_table(make_model, app_with_model):
    """Adding then removing a TableNode reconciles the tree correctly."""
    from sndeck.tree import TableNode, FileNode, SetNode, ScopeNode, TreeModel

    base = make_model()
    scope = base.scopes[0]
    first_set = scope.sets[0]

    extra_table = TableNode(
        table="sys_ui_action",
        label="UI Actions",
        files=[FileNode(table="sys_ui_action", sys_id="file_extra", name="Extra Action",
                        in_current_set=True, tracked=True, local=False,
                        dirty=False, record_path=None)],
    )

    new_set = SetNode(
        sys_id=first_set.sys_id, name=first_set.name, state=first_set.state,
        is_current=first_set.is_current,
        tables=list(first_set.tables) + [extra_table],
        scope=first_set.scope, is_base=first_set.is_base, members=first_set.members,
    )
    new_sets = list(scope.sets)
    new_sets[0] = new_set
    new_scope = ScopeNode(name=scope.name, sets=new_sets)
    new_scopes = list(base.scopes)
    new_scopes[0] = new_scope
    new_model = TreeModel(scopes=new_scopes, current_set=base.current_set)

    async with app_with_model(base) as pilot:
        app = pilot.app
        tree = app.query_one(Tree)

        app._render_tree(new_model)
        await pilot.pause()

        # Table nodes have no data tuple; match by label text.
        ui_action_nodes = [
            n for n in SndeckApp._iter_tree_nodes(tree.root)
            if "UI Actions" in str(n.label)
        ]
        assert len(ui_action_nodes) == 1, \
            "Expected exactly one UI Actions table node after add"
        children = list(ui_action_nodes[0].children)
        assert any("Extra Action" in str(c.label) for c in children), \
            "Extra file leaf should be under the new table node"

        app._render_tree(base)
        await pilot.pause()

        ui_action_nodes_after = [
            n for n in SndeckApp._iter_tree_nodes(tree.root)
            if "UI Actions" in str(n.label)
        ]
        assert len(ui_action_nodes_after) == 0, \
            "UI Actions table node should be gone after reverting to base model"


# ── Fix 1 regression: staging updates on watcher tick even with error model ───

@pytest.mark.asyncio
async def test_staging_updates_on_tick_even_with_error_model(tmp_path, monkeypatch):
    """_render_staging must fire on every ScratchChanged tick even when _last_model
    is an error model. Before Fix 1 the early-return in _refresh_local swallowed
    the call, freezing the staging pane during network-error state."""
    import json
    import sndeck.records as records
    from sndeck.tree import TreeModel
    from sndeck.watcher import ScratchChanged
    from textual.widgets import DataTable, Static

    app = SndeckApp(_make_client_for_app(), str(tmp_path), theme_name=MACCHIATO)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Inject an error model — simulates network failure after initial load.
        app._last_model = TreeModel(scopes=[], current_set=None, error="boom")

        # Write a new dirty record to disk under a per-set workspace dir.
        ws = set_workspace(tmp_path, "e" * 32, "Error Set")
        folder = ws / "sys_script" / f"DirtyRec__{'f' * 32}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "record.json").write_text(json.dumps(
            {"_meta": {"table": "sys_script", "sys_id": "f" * 32, "name": "DirtyRec"}}),
            encoding="utf-8")
        # Monkeypatch is_dirty to return True only for this folder.
        monkeypatch.setattr(records, "is_dirty", lambda p: str(p) == str(folder))

        # Fire a watcher tick with the error model still in place.
        app.post_message(ScratchChanged({str(folder)}))
        await pilot.pause()

        header = app.query_one("#staging-header", Static)
        tbl = app.query_one("#staging-table", DataTable)
        assert tbl.row_count == 1, (
            f"Expected 1 staging row even with error model; got {tbl.row_count}"
        )
        assert "1 changed" in str(header.render()), (
            f"Expected '1 changed' in header even with error model; got {header.render()!r}"
        )


# ── Fix 2: _reconcile_set relabels nested member set ─────────────────────────

@pytest.mark.asyncio
async def test_reconcile_relabels_member_set(tmp_path):
    """_reconcile_set must relabel a MEMBER sub-set's node in-place when its
    is_current flag changes. Object identity of the member node must be preserved
    (proves the reconcile path was taken, not a full rebuild)."""
    from sndeck.tree import TreeModel, ScopeNode, SetNode, TableNode

    # Base model: a batch base with one MEMBER sub-set (is_current=False).
    member_base = SetNode(
        sys_id="M" * 32, name="member set", state="in progress",
        is_current=False, tables=[], scope="global", is_base=False, members=[],
    )
    base_set = SetNode(
        sys_id="B" * 32, name="base set", state="in progress",
        is_current=True, tables=[], scope="global", is_base=True,
        members=[member_base],
    )
    base_model = TreeModel(
        scopes=[ScopeNode("Global", [base_set])],
        current_set=base_set,
    )

    app = SndeckApp(_make_client_for_app(), tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Force a full rebuild for the base model.
        app._last_model = None
        app._render_tree(base_model)
        await pilot.pause()

        tree = app.query_one("#tree", Tree)
        # Find the member set node (child of the base set node).
        set_nodes = [n for n in SndeckApp._iter_tree_nodes(tree.root)
                     if n.data and n.data[0] == "set"]
        # There should be 2 set nodes: base + member.
        assert len(set_nodes) == 2, (
            f"Expected 2 set nodes (base + member), got {len(set_nodes)}: "
            f"{[str(n.label) for n in set_nodes]}"
        )
        member_node_before = set_nodes[1]  # member is second (nested inside base)
        member_obj_id_before = id(member_node_before)
        label_before = str(member_node_before.label)
        assert "CURRENT" not in label_before, (
            f"Member should not have CURRENT initially; got {label_before!r}"
        )

        # Flip member's is_current to True.
        member_flipped = SetNode(
            sys_id="M" * 32, name="member set", state="in progress",
            is_current=True, tables=[], scope="global", is_base=False, members=[],
        )
        new_base_set = SetNode(
            sys_id="B" * 32, name="base set", state="in progress",
            is_current=True, tables=[], scope="global", is_base=True,
            members=[member_flipped],
        )
        new_model = TreeModel(
            scopes=[ScopeNode("Global", [new_base_set])],
            current_set=new_base_set,
        )

        app._render_tree(new_model)
        await pilot.pause()

        set_nodes_after = [n for n in SndeckApp._iter_tree_nodes(tree.root)
                           if n.data and n.data[0] == "set"]
        assert len(set_nodes_after) == 2, (
            f"Expected 2 set nodes after reconcile, got {len(set_nodes_after)}"
        )
        member_node_after = set_nodes_after[1]
        # Object identity must be preserved — reconcile path, not rebuild.
        assert id(member_node_after) == member_obj_id_before, (
            "Member set node was replaced (full rebuild) instead of reconciled in-place"
        )
        label_after = str(member_node_after.label)
        assert "CURRENT" in label_after, (
            f"Member set label should contain CURRENT after is_current flip; got {label_after!r}"
        )


def test_app_reconciles_scratch_on_mount(monkeypatch, tmp_path):
    from sndeck import app as app_mod

    calls = []
    monkeypatch.setattr(app_mod, "reconcile_and_report",
                        lambda client, root: calls.append(str(root)) or [])

    a = app_mod.SndeckApp(client=object(), scratch_dir=str(tmp_path))
    a._reconcile_scratch_once()   # the extracted hook called from on_mount

    assert calls == [str(tmp_path)]


def test_app_reconcile_never_raises(monkeypatch, tmp_path):
    from sndeck import app as app_mod, prune
    # reconcile_and_report owns the never-raise contract itself: make the real
    # raiser inside it (reconcile_scratch) blow up, with no wrapper guard in
    # app._reconcile_scratch_once backstopping it, and confirm the hook still
    # doesn't raise.
    def boom(client, root):
        raise RuntimeError("boom")
    monkeypatch.setattr(prune, "reconcile_scratch", boom)
    a = app_mod.SndeckApp(client=object(), scratch_dir=str(tmp_path))
    a._reconcile_scratch_once()   # must not raise
