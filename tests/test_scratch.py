import json
from pathlib import Path
from sndeck.scratch import set_workspaces, orphans, SetWorkspace, RecordRef

HEX = "0123456789abcdef0123456789abcdef"


def _rec(parent, table, name, sysid, *, script="x", dirty=False):
    folder = Path(parent) / table / f"{name}__{sysid}"
    folder.mkdir(parents=True)
    fields = {"sys_id": sysid, "name": name, "script": script}
    (folder / "record.json").write_text(json.dumps(
        {"_meta": {"table": table, "sys_id": sysid, "name": name, "pulled_at": "t"}, **fields}))
    (folder / ".snapshot.json").write_text(json.dumps(fields))
    (folder / "script.js").write_text(script + (" // E" if dirty else ""))
    return folder


def _setdir(root, slug, sysid):
    d = Path(root) / f"{slug}__{sysid}"; d.mkdir(parents=True); return d


def test_set_workspaces_groups_records_by_set_and_extracts_slug(tmp_path):
    d = _setdir(tmp_path, "vendor fix", "a" + HEX[1:])
    _rec(d, "sys_script", "A", "111"); _rec(d, "sp_widget", "W", "222")
    (tmp_path / "sp_widget").mkdir()  # a flat-root table dir must NOT become a workspace
    _rec(tmp_path, "sp_widget", "OrphanRec", "999")
    wss = set_workspaces(tmp_path)
    assert [w.set_sys_id for w in wss] == ["a" + HEX[1:]]
    w = wss[0]
    assert w.slug == "vendor fix" and w.dir == d
    assert sorted((r.table, r.name) for r in w.records) == [("sp_widget", "W"), ("sys_script", "A")]


def test_orphans_returns_only_flat_root_records(tmp_path):
    d = _setdir(tmp_path, "live", "b" + HEX[1:]); _rec(d, "sys_script", "InSet", "111")
    _rec(tmp_path, "sp_widget", "Flat", "222")
    orl = orphans(tmp_path)
    assert [(r.table, r.name) for r in orl] == [("sp_widget", "Flat")]
