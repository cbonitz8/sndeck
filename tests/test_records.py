import json
import httpx
import pytest
from pathlib import Path
from sndeck.records import pull_record, scan_scratch, RecordRef, dirty_files_from_disk, set_workspace
from sndeck.rest import TableClient
from sndeck.config import Instance

INST = Instance("dev", "https://x.service-now.com", "cid",
                "https://x.service-now.com/oauth_token.do", "cbonitz@x")


class FakeToken:
    def access_token(self): return "AT"
    def invalidate(self): pass


def _client_returning(record):
    def handler(req):
        return httpx.Response(200, json={"result": [record]})
    return TableClient(INST, FakeToken(), http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_pull_writes_folder_record_json_and_extracts_script(tmp_path):
    rec = {"sys_id": "3a4f", "name": "My BR", "when": "after", "order": "100",
           "active": "true", "script": "gs.info('hi');"}
    ref = pull_record(_client_returning(rec), "sys_script", "3a4f", tmp_path)
    folder = tmp_path / "sys_script" / "My BR__3a4f"
    assert ref.path == folder and ref.name == "My BR" and ref.sys_id == "3a4f"
    body = json.loads((folder / "record.json").read_text())
    assert body["_meta"] == {"table": "sys_script", "sys_id": "3a4f", "name": "My BR",
                             "pulled_at": body["_meta"]["pulled_at"]}
    assert body["when"] == "after" and body["order"] == "100"  # all columns present
    assert body["script"] == "gs.info('hi');"                  # value kept in record.json too
    assert (folder / "script.js").read_text() == "gs.info('hi');"  # and extracted


def test_pull_ng_template_extracts_template_html_and_detects_edit(tmp_path):
    from sndeck.sync import local_field_changes
    rec = {"sys_id": "ng1", "name": "sp-hero", "sp_widget": "w1",
           "template": "<div>hi</div>"}
    ref = pull_record(_client_returning(rec), "sp_ng_template", "ng1", tmp_path)
    tpl = ref.path / "template.html"
    assert tpl.read_text() == "<div>hi</div>"          # code field extracted to .html
    assert local_field_changes(ref.path) == []          # clean right after pull
    tpl.write_text("<div>edited</div>")
    changes = local_field_changes(ref.path)
    assert [c.field for c in changes] == ["template"]    # edit is a pushable change


def test_pull_writes_snapshot(tmp_path):
    rec = {"sys_id": "abc", "name": "X", "script": "a"}
    pull_record(_client_returning(rec), "sys_script", "abc", tmp_path)
    snap = json.loads((tmp_path / "sys_script" / "X__abc" / ".snapshot.json").read_text())
    assert snap["script"] == "a"


def test_pull_sanitizes_slashes_in_name(tmp_path):
    rec = {"sys_id": "1", "name": "a/b:c", "script": ""}
    ref = pull_record(_client_returning(rec), "sys_script", "1", tmp_path)
    assert "/" not in ref.path.name and ref.path.name.endswith("__1")


def test_scan_scratch_reads_meta(tmp_path):
    rec = {"sys_id": "9", "name": "Widget A", "script": "x"}
    pull_record(_client_returning(rec), "sys_script", "9", tmp_path)
    refs = scan_scratch(tmp_path)
    assert len(refs) == 1
    assert refs[0] == RecordRef("sys_script", "9", "Widget A",
                                tmp_path / "sys_script" / "Widget A__9")


def test_pull_raises_when_record_missing(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"result": []})
    client = TableClient(INST, FakeToken(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(LookupError):
        pull_record(client, "sys_script", "nope", tmp_path)


def test_scan_recovers_real_name_from_meta(tmp_path):
    rec = {"sys_id": "1", "name": "a/b:c", "script": ""}
    pull_record(_client_returning(rec), "sys_script", "1", tmp_path)
    refs = scan_scratch(tmp_path)
    assert refs[0].name == "a/b:c"  # real name from _meta, not the sanitized folder


def _mk_record(scratch: Path, table: str, sid: str, name: str = "R", dirty: bool = False) -> Path:
    folder = scratch / table / f"{name}__{sid}"
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps(
        {"_meta": {"table": table, "sys_id": sid, "name": name}, "script": "x"}))
    (folder / ".snapshot.json").write_text(json.dumps({"script": "y" if dirty else "x"}))
    (folder / "script.js").write_text("x")
    return folder


def test_folders_for_records_returns_only_matches(tmp_path):
    from sndeck.records import folders_for_records
    f1 = _mk_record(tmp_path, "sys_script", "a" * 32)
    _mk_record(tmp_path, "sys_script", "b" * 32)
    got = folders_for_records(tmp_path, [("sys_script", "a" * 32)])
    assert got == [f1]


def test_folders_for_records_ignores_unpulled(tmp_path):
    from sndeck.records import folders_for_records
    assert folders_for_records(tmp_path, [("sys_script", "z" * 32)]) == []


def test_delete_record_folders_removes_and_counts(tmp_path):
    from sndeck.records import delete_record_folders
    f1 = _mk_record(tmp_path, "sys_script", "a" * 32)
    n = delete_record_folders([f1])
    assert n == 1 and not f1.exists()


def test_delete_record_folders_survives_missing(tmp_path):
    from sndeck.records import delete_record_folders
    assert delete_record_folders([tmp_path / "does_not_exist"]) == 0


def test_set_dir_name_slug_and_sysid():
    from sndeck.records import set_dir_name
    assert set_dir_name("talent show + ocr", "59545947938dcb10666275d97bba101d") \
        == "talent show _ ocr__59545947938dcb10666275d97bba101d"


def test_set_dir_name_sanitizes_and_falls_back():
    from sndeck.records import set_dir_name
    assert set_dir_name("a/b:c", "x").startswith("a_b_c__")
    assert set_dir_name("", "sid") == "unnamed__sid"


def test_set_workspace_is_root_child(tmp_path):
    from sndeck.records import set_workspace
    ws = set_workspace(tmp_path, "abc123", "My Set")
    assert ws == tmp_path / "My Set__abc123"


class _FakeClient:
    def get_record(self, table, sys_id, display_value="false"):
        return {"sys_id": sys_id, "name": f"rec-{sys_id}"}


def test_scan_workspace_tags_set_sysid(tmp_path):
    from sndeck.records import scan_workspace, set_workspace
    ws = set_workspace(tmp_path, "5" * 32, "Talent")
    pull_record(_FakeClient(), "sys_script_include", "a" * 32, ws)
    refs = scan_workspace(tmp_path)
    assert len(refs) == 1
    assert refs[0].set_sys_id == "5" * 32
    assert refs[0].ref.table == "sys_script_include"
    assert refs[0].ref.sys_id == "a" * 32


def test_scan_workspace_ignores_untagged_top_dirs(tmp_path):
    from sndeck.records import scan_workspace
    # a top dir with no __<32hex> suffix is legacy/flat — skipped by workspace scan
    (tmp_path / "sys_script_include" / "x__" ).mkdir(parents=True)
    assert scan_workspace(tmp_path) == []


_SETA = "a" * 32
_SETB = "b" * 32


def test_dirty_files_from_disk_filters_and_dedupes(tmp_path, monkeypatch):
    # Build two set workspaces on disk with record.json + _meta via set_workspace + manual write.
    wsa = set_workspace(tmp_path, _SETA, "Set A")
    wsb = set_workspace(tmp_path, _SETB, "Set B")
    # Minimal record folders (record.json with _meta) — mirror scan_scratch's expectations.
    import json

    def rec(ws, table, sys_id, name):
        # scan_scratch uses glob("*/*/record.json"), so structure is ws/table/name__sys_id/
        folder = ws / table / f"{name}__{sys_id}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "record.json").write_text(json.dumps(
            {"_meta": {"table": table, "sys_id": sys_id, "name": name}}), encoding="utf-8")
        return folder

    d1 = rec(wsa, "sys_script_include", "r1", "Alpha")
    d2 = rec(wsa, "sys_script", "r2", "Beta")
    d3 = rec(wsb, "sys_script_include", "r1", "Alpha")  # same (table,sys_id) as d1
    rec(wsb, "sys_script", "r9", "Zeta")

    import sndeck.records as records
    monkeypatch.setattr(records, "is_dirty", lambda p: str(p) in {str(d1), str(d2), str(d3)})

    out = dirty_files_from_disk(tmp_path)
    keys = [(f.table, f.sys_id) for f in out]
    assert ("sys_script", "r2") in keys
    assert ("sys_script_include", "r1") in keys
    assert ("sys_script", "r9") not in keys          # clean, excluded
    assert len(keys) == len(set(keys))               # deduped
    assert keys == sorted(keys)                       # sorted by (table, sys_id-effectively via name)
    assert all(f.dirty and f.local and f.record_path is not None for f in out)


def test_dirty_files_from_disk_empty(tmp_path):
    assert dirty_files_from_disk(tmp_path) == []


def _client_routes(routes):
    """routes: fn(table, params) -> list[dict]."""
    def handler(req):
        table = str(req.url.path).rsplit("/", 1)[-1]
        return httpx.Response(200, json={"result": routes(table, dict(req.url.params))})
    return TableClient(INST, FakeToken(), http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_pull_set_materializes_all_records_and_counts_skips(tmp_path):
    from sndeck.records import pull_set, PullSummary
    a, b, gone = "a" * 32, "b" * 32, "c" * 32

    def routes(table, params):
        if table == "sys_update_xml":
            return [{"name": "sys_script_" + a}, {"name": "sys_script_" + b},
                    {"name": "sys_script_" + gone}]
        if table == "sys_script":
            q = params.get("sysparm_query", "")
            if gone in q:
                return []          # deleted on the instance -> pull_record raises LookupError
            sid = a if a in q else b
            return [{"sys_id": sid, "name": "R" + sid[:2], "script": "x"}]
        return []

    summary = pull_set(_client_routes(routes), tmp_path, "SET1", "My Set")
    assert summary == PullSummary(pulled=2, skipped=1)
    ws = set_workspace(tmp_path, "SET1", "My Set")
    assert {r.sys_id for r in scan_scratch(ws)} == {a, b}
