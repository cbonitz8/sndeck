import json

from sndeck import snapshot
from storekit import SET_A, make_copy


def test_record_body_merges_extra_before_pulled_at():
    body = snapshot.record_body("sys_script", "1", "A", {"script": "x"},
                                {"instance": "dev", "kind": "edit", "target_set": SET_A})
    assert list(body["_meta"]) == ["table", "sys_id", "name", "instance", "kind",
                                   "target_set", "pulled_at"]
    assert body["script"] == "x"


def test_record_body_without_extra_is_unchanged():
    body = snapshot.record_body("sys_script", "1", "A", {})
    assert list(body["_meta"]) == ["table", "sys_id", "name", "pulled_at"]


def test_read_and_write_meta_extra(tmp_path):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111")
    assert snapshot.read_meta_extra(f) == {"instance": "dev", "kind": "edit",
                                           "target_set": SET_A}
    snapshot.write_meta_extra(f, target_set={"sys_id": "S2", "name": "Two"})
    meta = json.loads((f / "record.json").read_text())["_meta"]
    assert meta["target_set"] == {"sys_id": "S2", "name": "Two"}
    assert meta["pulled_at"] == "2026-10-01T00:00:00+00:00"


def test_local_diff_names_field_and_both_sides(tmp_path):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", dirty=True)
    d = snapshot.local_diff(f, {"script": "gs.info('x');"})
    assert "--- instance/script.js" in d and "+++ local/script.js" in d
    assert "+gs.info('x'); // EDITED" in d


def test_local_diff_empty_when_equal(tmp_path):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111")
    assert snapshot.local_diff(f, {"script": "gs.info('x');"}) == ""
