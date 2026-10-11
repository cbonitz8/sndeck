import json
import stat

import pytest

from sndeck import snapshot
from sndeck.records import default_kind, pull, pull_set_into_store, retarget
from sndeck.store import EDIT, REF, Store
from storekit import SET_A, sid


def _client(sn_client, rec):
    def routes(table, params):
        return [rec] if rec is not None else []
    return sn_client(routes)


def test_default_kind():
    assert default_kind("dev") == EDIT
    assert default_kind("prod") == REF


def test_edit_pull_needs_target_set(tmp_path, sn_client):
    with pytest.raises(ValueError, match="target update set"):
        pull(_client(sn_client, {"sys_id": "1"}), Store(tmp_path), "sys_script", "1", kind=EDIT)


def test_missing_record_raises_lookup(tmp_path, sn_client):
    with pytest.raises(LookupError):
        pull(_client(sn_client, None), Store(tmp_path), "sys_script", "1",
             kind=EDIT, target_set=SET_A)


def test_first_pull_creates_edit_copy(tmp_path, sn_client):
    rec = {"sys_id": "111", "name": "My BR", "script": "a"}
    o = pull(_client(sn_client, rec), Store(tmp_path), "sys_script", "111",
             kind=EDIT, target_set=SET_A)
    assert o.action == "created" and o.target_set == SET_A
    assert o.path == tmp_path / "dev" / "sys_script" / "My BR__111"
    assert snapshot.read_meta_extra(o.path) == {"instance": "dev", "kind": "edit",
                                                "target_set": SET_A}
    assert (o.path / "script.js").read_text() == "a"


def test_clean_repull_refreshes_and_keeps_target(tmp_path, sn_client):
    s = Store(tmp_path)
    pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "a"}), s,
         "sys_script", "111", kind=EDIT, target_set=SET_A)
    other = {"sys_id": "S2", "name": "Two"}
    o = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "b"}), s,
             "sys_script", "111", kind=EDIT, target_set=other)
    assert o.action == "refreshed" and o.target_set == SET_A
    assert (o.path / "script.js").read_text() == "b"
    assert not snapshot.is_dirty(o.path)


def test_dirty_repull_is_refused(tmp_path, sn_client):
    s = Store(tmp_path)
    o = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "a"}), s,
             "sys_script", "111", kind=EDIT, target_set=SET_A)
    (o.path / "script.js").write_text("mine")
    o2 = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "a"}), s,
              "sys_script", "111", kind=EDIT, target_set=SET_A)
    assert o2.action == "refused" and o2.blocked_fields == ("script",)
    assert "--overwrite-local" in o2.reason
    assert (o.path / "script.js").read_text() == "mine"


def test_dirty_repull_with_overwrite_local_replaces(tmp_path, sn_client):
    s = Store(tmp_path)
    o = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "a"}), s,
             "sys_script", "111", kind=EDIT, target_set=SET_A)
    (o.path / "script.js").write_text("mine")
    o2 = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "a"}), s,
              "sys_script", "111", kind=EDIT, target_set=SET_A, overwrite_local=True)
    assert o2.action == "refreshed" and (o.path / "script.js").read_text() == "a"


def test_dirty_but_already_live_is_refreshed(tmp_path, sn_client):
    s = Store(tmp_path)
    o = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "a"}), s,
             "sys_script", "111", kind=EDIT, target_set=SET_A)
    (o.path / "script.js").write_text("pushed elsewhere")
    o2 = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "pushed elsewhere"}),
              s, "sys_script", "111", kind=EDIT, target_set=SET_A)
    assert o2.action == "refreshed" and not snapshot.is_dirty(o.path)


def test_pull_after_rename_reuses_folder(tmp_path, sn_client):
    s = Store(tmp_path)
    o = pull(_client(sn_client, {"sys_id": "111", "name": "Old", "script": "a"}), s,
             "sys_script", "111", kind=EDIT, target_set=SET_A)
    o2 = pull(_client(sn_client, {"sys_id": "111", "name": "New", "script": "a"}), s,
              "sys_script", "111", kind=EDIT, target_set=SET_A)
    assert o2.path == o.path
    assert sorted(p.name for p in (tmp_path / "dev" / "sys_script").iterdir()) == ["Old__111"]


def test_refresh_removes_field_emptied_on_instance(tmp_path, sn_client):
    s = Store(tmp_path)
    o = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "a"}), s,
             "sys_script", "111", kind=EDIT, target_set=SET_A)
    pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": ""}), s,
         "sys_script", "111", kind=EDIT, target_set=SET_A)
    assert not (o.path / "script.js").exists() and not snapshot.is_dirty(o.path)


def test_ref_pull_is_read_only_and_repull_works(tmp_path, sn_client):
    s = Store(tmp_path)
    o = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "a"}), s,
             "sys_script", "111", kind=REF)
    assert o.path == tmp_path / "dev" / "_ref" / "sys_script" / "A__111"
    assert o.target_set is None
    assert stat.S_IMODE((o.path / "script.js").stat().st_mode) == 0o444
    o2 = pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "b"}), s,
              "sys_script", "111", kind=REF)
    assert o2.action == "refreshed" and (o.path / "script.js").read_text() == "b"
    assert json.loads((o.path / "record.json").read_text())["_meta"]["kind"] == "ref"


def test_retarget_rewrites_target(tmp_path, sn_client):
    s = Store(tmp_path)
    pull(_client(sn_client, {"sys_id": "111", "name": "A", "script": "a"}), s,
         "sys_script", "111", kind=EDIT, target_set=SET_A)
    two = {"sys_id": "S2", "name": "Two"}
    p = retarget(s, "dev", "sys_script", "111", two)
    assert snapshot.read_meta_extra(p)["target_set"] == two


def test_retarget_without_edit_copy_raises(tmp_path):
    with pytest.raises(LookupError, match="no edit copy"):
        retarget(Store(tmp_path), "dev", "sys_script", "111", SET_A)


def test_pull_set_into_store_counts(tmp_path, sn_client):
    r1, r2 = sid("1"), sid("2")

    def routes(table, params):
        q = params.get("sysparm_query", "")
        if table == "sys_update_xml":
            return [{"name": f"sys_script_{r1}"}, {"name": f"sys_script_{r2}"}]
        if table == "sys_script" and q == f"sys_id={r1}":
            return [{"sys_id": r1, "name": "A", "script": "a"}]
        return []
    summary = pull_set_into_store(sn_client(routes), Store(tmp_path), SET_A["sys_id"], "Set A",
                                  kind=EDIT)
    assert (summary.pulled, summary.refused, summary.skipped) == (1, 0, 1)


def test_pull_set_into_store_ref_kind_writes_untargeted_ref_copies(tmp_path, sn_client):
    r1 = sid("1")

    def routes(table, params):
        if table == "sys_update_xml":
            return [{"name": f"sys_script_{r1}"}]
        if table == "sys_script":
            return [{"sys_id": r1, "name": "A", "script": "a"}]
        return []
    store = Store(tmp_path)
    summary = pull_set_into_store(sn_client(routes), store, SET_A["sys_id"], "Set A", kind=REF)
    assert (summary.pulled, summary.refused, summary.skipped) == (1, 0, 0)
    assert store.iter_copies("dev", EDIT) == []
    [c] = store.iter_copies("dev", REF)
    assert c.target_set is None
