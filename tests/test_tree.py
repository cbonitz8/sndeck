import json
import pytest
from sndeck import tree
from sndeck.registry import field_extension
from sndeck.records import set_workspace

# Canonical fake sys_ids: 32 lowercase hex chars (required by scan_workspace regex)
_SET1 = "1" * 32
_SET2 = "2" * 32
_SET_WS = "e" * 32
_BASE_ID = "b" * 32
_CHILD_ID = "c" * 32


class FakeClient:
    """Minimal client implementing just what build_tree calls."""
    def __init__(self, *, user, cur_set_id, prefs, sets, entries):
        self._user = user; self._cur = cur_set_id
        self._prefs = prefs; self._sets = sets; self._entries = entries

    def query(self, table, *, query=None, fields=None, display_value="false", limit=None, offset=None):
        if table == "sys_user":
            return [self._user]
        if table == "sys_user_preference":
            return [{"value": self._prefs}] if self._prefs else []
        if table == "sys_update_set":
            # batch_members uses two query forms:
            #   sys_id=<id>                        — single-set lookup
            #   base_update_set=<id>^ORDERBYparent — family lookup
            if "base_update_set=" in query and not query.startswith("sys_id="):
                base_id = query.split("base_update_set=", 1)[1].split("^", 1)[0]
                return [m for m in self._sets.values()
                        if _raw_val(m, "base_update_set") == base_id
                        or _raw_val(m, "sys_id") == base_id]
            sid = query.split("sys_id=", 1)[1].split("^", 1)[0]
            m = self._sets.get(sid)
            return [m] if m else []
        if table == "sys_update_xml":
            sid = query.split("update_set=", 1)[1].split("^", 1)[0]
            return self._entries.get(sid, [])
        return []


def _raw_val(row, field):
    """Extract raw value from a fake setmeta dict."""
    v = row.get(field, {})
    if isinstance(v, dict):
        return v.get("value", "")
    return v or ""


def _entry(table, sid, name, typ="Update"):
    return {"name": {"value": f"{table}_{sid}"},
            "target_name": {"display_value": name}, "type": {"display_value": typ}}


def _setmeta(sid, name, scope="Global", *, base_update_set=None, parent=None):
    """Build a fake sys_update_set row. base_update_set defaults to self (standalone)."""
    bus = base_update_set if base_update_set is not None else sid
    return {"sys_id": {"value": sid}, "name": {"value": name},
            "state": {"value": "in progress", "display_value": "In progress"},
            "application": {"display_value": scope},
            "base_update_set": {"value": bus},
            "parent": {"value": parent or ""}}


def _pull(scratch, table, sid, name, fields, snapshot):
    folder = scratch / table / f"{name}__{sid}"
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps(
        {"_meta": {"table": table, "sys_id": sid, "name": name}, **snapshot}))
    (folder / ".snapshot.json").write_text(json.dumps(snapshot))
    for f, v in fields.items():
        (folder / f"{f}{field_extension(f)}").write_text(v)


def _pull_into_ws(ws_dir, table, sid, name, fields, snapshot):
    """Like _pull but places the record inside a per-set workspace dir."""
    folder = ws_dir / table / f"{name}__{sid}"
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps(
        {"_meta": {"table": table, "sys_id": sid, "name": name}, **snapshot}))
    (folder / ".snapshot.json").write_text(json.dumps(snapshot))
    for f, v in fields.items():
        (folder / f"{f}{field_extension(f)}").write_text(v)


def test_build_tree_groups_scope_set_table_and_flags(tmp_path):
    a, b = "a"*32, "f"*32
    ws = set_workspace(tmp_path, _SET1, "sn setup")
    ws.mkdir(parents=True, exist_ok=True)
    client = FakeClient(
        user={"sys_id": "U1", "user_name": "cbonitz"},
        cur_set_id=_SET1, prefs=_SET1,
        sets={_SET1: _setmeta(_SET1, "sn setup", "Global")},
        entries={_SET1: [_entry("sys_script", a, "MyBR"), _entry("sys_properties", b, "prop.x")]})
    _pull_into_ws(ws, "sys_script", a, "MyBR", {"script": "orig"}, {"script": "orig"})  # local, clean
    model = tree.build_tree(client, tmp_path, [])
    assert model.error is None
    assert [s.name for s in model.scopes] == ["Global"]
    setnode = model.scopes[0].sets[0]
    assert setnode.is_current is True and setnode.name == "sn setup"
    # code table first
    assert [t.label for t in setnode.tables] == ["Business Rules", "System Properties"]
    br = setnode.tables[0].files[0]
    assert br.name == "MyBR" and br.in_current_set and br.local and not br.dirty
    prop = setnode.tables[1].files[0]
    assert prop.local is False and prop.record_path is None  # not pulled


def test_dirty_flag_set_when_local_edited(tmp_path):
    a = "a"*32
    ws = set_workspace(tmp_path, _SET1, "s")
    ws.mkdir(parents=True, exist_ok=True)
    client = FakeClient(user={"sys_id": "U1", "user_name": "cbonitz"},
                        cur_set_id=_SET1, prefs=_SET1,
                        sets={_SET1: _setmeta(_SET1, "s")},
                        entries={_SET1: [_entry("sys_script", a, "BR")]})
    _pull_into_ws(ws, "sys_script", a, "BR", {"script": "EDITED"}, {"script": "orig"})
    model = tree.build_tree(client, tmp_path, [])
    assert model.scopes[0].sets[0].tables[0].files[0].dirty is True


def test_tracked_set_included_and_not_current(tmp_path):
    a = "a"*32
    client = FakeClient(user={"sys_id": "U1", "user_name": "cbonitz"},
                        cur_set_id=_SET1, prefs=_SET1,
                        sets={_SET1: _setmeta(_SET1, "cur"), _SET2: _setmeta(_SET2, "pinned")},
                        entries={_SET1: [], _SET2: [_entry("sys_script", a, "BR")]})
    model = tree.build_tree(client, tmp_path, [_SET2])
    names = {s.name: s for scope in model.scopes for s in scope.sets}
    assert names["cur"].is_current and not names["pinned"].is_current
    assert names["pinned"].tables[0].files[0].in_current_set is False


def test_error_when_no_user(tmp_path):
    client = FakeClient(user=None, cur_set_id=None, prefs=None, sets={}, entries={})
    # FakeClient returns [self._user]; make it [] when user None:
    client._user = None
    model = tree.build_tree(_NoUser(), tmp_path, [])
    assert model.error and "user" in model.error.lower()


class _NoUser:
    def query(self, table, **kw):
        return []


# ── dirty_files (staging pane / push-all source) ─────────────────────────────

from pathlib import Path
from sndeck.tree import (
    FileNode, TableNode, SetNode, ScopeNode, TreeModel, dirty_files,
)


def _fnode(table, sid, name, *, dirty, local=True, tracked=True):
    return FileNode(table, sid, name, in_current_set=True, tracked=tracked, local=local,
                    dirty=dirty, record_path=Path(f"/x/{table}/{sid}") if local else None)


def _model(*files_per_set):
    """Each arg is a list of FileNodes → one SetNode (all in one TableNode/ScopeNode)."""
    sets = []
    for i, files in enumerate(files_per_set):
        sets.append(SetNode(f"SET{i}", f"set{i}", "in progress", i == 0,
                            [TableNode("sys_script", "Business Rules", files)]))
    return TreeModel([ScopeNode("Global", sets)], None)


def test_dirty_files_returns_only_dirty_local_nodes():
    clean = _fnode("sys_script", "a" * 32, "Clean", dirty=False)
    dirty = _fnode("sys_script", "b" * 32, "Dirty", dirty=True)
    remote_dirty = _fnode("sys_script", "c" * 32, "Remote", dirty=True, local=False)
    out = dirty_files(_model([clean, dirty, remote_dirty]))
    assert [f.name for f in out] == ["Dirty"]


def test_dirty_files_dedupes_record_in_two_sets():
    sid = "d" * 32
    in_set0 = _fnode("sys_script", sid, "Shared", dirty=True)
    in_set1 = _fnode("sys_script", sid, "Shared", dirty=True)
    out = dirty_files(_model([in_set0], [in_set1]))
    assert len(out) == 1 and out[0].sys_id == sid


def test_dirty_files_empty_model():
    assert dirty_files(TreeModel([], None)) == []
    assert dirty_files(_model([_fnode("sys_script", "e" * 32, "Clean", dirty=False)])) == []


# ── Task-5 new tests ──────────────────────────────────────────────────────────

class _FakeClientWithSetAndLocal:
    """
    Set _SET_WS has record A in its manifest.
    Its workspace dir additionally contains record B (local-only, not in manifest).
    """
    def __init__(self, tmp_path):
        self._tmp = tmp_path
        a_sid = "a" * 32
        b_sid = "b" * 32
        self._set_id = _SET_WS
        self._set_name = "ws set"
        # create the set workspace dir and write both records into it
        ws = set_workspace(tmp_path, self._set_id, self._set_name)
        ws.mkdir(parents=True, exist_ok=True)
        _pull_into_ws(ws, "sys_script", a_sid, "RecordA", {"script": "a"}, {"script": "a"})
        _pull_into_ws(ws, "sys_script", b_sid, "RecordB", {"script": "b"}, {"script": "b"})
        self._a_sid = a_sid
        self._b_sid = b_sid
        # manifest has only record A; record B is local-only
        self._sets = {self._set_id: _setmeta(self._set_id, self._set_name)}
        self._entries = {self._set_id: [_entry("sys_script", a_sid, "RecordA")]}

    def build(self):
        client = FakeClient(
            user={"sys_id": "U1", "user_name": "cbonitz"},
            cur_set_id=self._set_id, prefs=self._set_id,
            sets=self._sets, entries=self._entries)
        return tree.build_tree(client, self._tmp, [])


@pytest.fixture
def fake_client_with_set_and_local(tmp_path):
    return _FakeClientWithSetAndLocal(tmp_path)


class _FakeBatchClient:
    """
    Batch: base set _BASE_ID with one member _CHILD_ID.
    _BASE_ID manifest: record C; _CHILD_ID manifest: record D.
    _CHILD_ID name is "ocr + resume parsing" (checked by the test).
    """
    def __init__(self, tmp_path):
        self._tmp = tmp_path
        c_sid = "cc" * 16
        d_sid = "dd" * 16
        sets = {
            _BASE_ID: _setmeta(_BASE_ID, "base set", "Global",
                                base_update_set=_BASE_ID, parent=None),
            _CHILD_ID: _setmeta(_CHILD_ID, "ocr + resume parsing", "Global",
                                 base_update_set=_BASE_ID, parent=_BASE_ID),
        }
        entries = {
            _BASE_ID: [_entry("sys_script", c_sid, "RecordC")],
            _CHILD_ID: [_entry("sys_script", d_sid, "RecordD")],
        }
        self._sets = sets
        self._entries = entries

    def build(self):
        client = FakeClient(
            user={"sys_id": "U1", "user_name": "cbonitz"},
            cur_set_id=_BASE_ID, prefs=_BASE_ID,
            sets=self._sets, entries=self._entries)
        return tree.build_tree(client, self._tmp, [])


@pytest.fixture
def fake_batch_client(tmp_path):
    return _FakeBatchClient(tmp_path)


def test_local_only_file_appears_untracked(fake_client_with_set_and_local):
    # set manifest has record A; workspace dir for the set additionally has record B (local only)
    model = fake_client_with_set_and_local.build()
    files = [f for sc in model.scopes for st in sc.sets for tb in st.tables for f in tb.files]
    by_id = {f.sys_id: f for f in files}
    assert by_id["a" * 32].tracked is True and by_id["a" * 32].local is True
    assert by_id["b" * 32].tracked is False and by_id["b" * 32].local is True   # local only — not in set yet


def test_batch_base_nests_members(fake_batch_client):
    model = fake_batch_client.build()
    base = [st for sc in model.scopes for st in sc.sets if st.is_base][0]
    assert base.members and base.members[0].name == "ocr + resume parsing"
    # M4: the base's OWN records must survive the top-SetNode reconstruction
    base_record_ids = {f.sys_id for tbl in base.tables for f in tbl.files}
    assert "cc" * 16 in base_record_ids, (
        f"Base's own record (RecordC) missing from tables after reconstruction; found {base_record_ids!r}"
    )


# ── model lookups (find_set / owner_of_record) ───────────────────────────────

def _model_with_batch():
    from sndeck.tree import SetNode, ScopeNode, TableNode, FileNode, TreeModel
    a = "a" * 32
    child_file = FileNode("sys_script", a, "R", in_current_set=False, tracked=True,
                          local=False, dirty=False, record_path=None)
    child = SetNode("C" * 32, "child", "in progress", False,
                    [TableNode("sys_script", "Business Rules", [child_file])],
                    scope="x_child", is_base=False, members=[])
    base = SetNode("B" * 32, "base", "in progress", True, [],
                   scope="global", is_base=True, members=[child])
    return TreeModel([ScopeNode("Global", [base])], current_set=None), a


def test_find_set_resolves_nested_member_to_its_scope():
    model, _a = _model_with_batch()
    node, scope = tree.find_set(model, "C" * 32)
    assert node.sys_id == "C" * 32 and node.scope == "x_child" and scope.name == "Global"
    assert tree.find_set(model, "B" * 32)[0].sys_id == "B" * 32
    assert tree.find_set(model, "z" * 32) is None
    assert tree.find_set(None, "B" * 32) is None


def test_owner_of_record_finds_staging_set_at_any_depth():
    model, a = _model_with_batch()
    assert tree.owner_of_record(model, "sys_script", a) == ("x_child", "C" * 32)
    assert tree.owner_of_record(model, "sys_script", "z" * 32) is None
    assert tree.owner_of_record(None, "sys_script", a) is None


def test_iter_sets_yields_base_then_members():
    model, _a = _model_with_batch()
    ids = [s.sys_id for _scope, s in tree.iter_sets(model)]
    assert ids == ["B" * 32, "C" * 32]
    assert list(tree.iter_sets(None)) == []
