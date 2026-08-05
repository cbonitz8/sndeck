import json
from pathlib import Path
from sndeck import sync
from sndeck.registry import field_extension


def _make_record(tmp_path, table, fields, snapshot):
    """Create a scratch record folder: record.json, .snapshot.json, extracted field files."""
    folder = tmp_path / table / f"rec__{'a'*32}"
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps(
        {"_meta": {"table": table, "sys_id": "a"*32, "name": "Rec"}, **snapshot}))
    (folder / ".snapshot.json").write_text(json.dumps(snapshot))
    for f, val in fields.items():
        (folder / f"{f}{field_extension(f)}").write_text(val)
    return folder


def test_no_changes_when_local_matches_snapshot(tmp_path):
    folder = _make_record(tmp_path, "sys_script", {"script": "line1\n"}, {"script": "line1\n"})
    assert sync.local_field_changes(folder) == []
    assert sync.is_dirty(folder) is False


def test_edited_field_is_a_change(tmp_path):
    folder = _make_record(tmp_path, "sys_script", {"script": "EDITED\n"}, {"script": "orig\n"})
    changes = sync.local_field_changes(folder)
    assert [c.field for c in changes] == ["script"]
    assert changes[0].local == "EDITED\n" and changes[0].snapshot == "orig\n"
    assert sync.is_dirty(folder) is True


def test_line_ending_only_diff_is_not_a_change(tmp_path):
    folder = _make_record(tmp_path, "sys_script", {"script": "a\r\nb\r\n"}, {"script": "a\nb\n"})
    assert sync.local_field_changes(folder) == []


def test_non_code_record_has_no_changes(tmp_path):
    folder = _make_record(tmp_path, "sys_properties", {}, {"value": "x"})
    assert sync.local_field_changes(folder) == []


def test_widget_multiple_changed_fields(tmp_path):
    folder = _make_record(
        tmp_path, "sp_widget",
        {"script": "S2", "client_script": "C1", "template": "T2"},
        {"script": "S1", "client_script": "C1", "template": "T1"})
    assert sorted(c.field for c in sync.local_field_changes(folder)) == ["script", "template"]


def test_is_dirty_false_on_missing_snapshot(tmp_path):
    folder = tmp_path / "sys_script" / "x__1"
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps({"_meta": {"table": "sys_script", "sys_id": "1", "name": "X"}}))
    assert sync.is_dirty(folder) is False


# ---- Task 15: PushPlan / build_push_plan / apply_push ----

class _RecClient:
    def __init__(self, record):
        self._record = record
        self.patched = None
        self.put_called = None
    def get_record(self, table, sys_id, **kw): return self._record
    def patch(self, table, sys_id, body): self.patched = (table, sys_id, body); return {}
    def put(self, table, sys_id, body): self.put_called = (table, sys_id, body); return {}


def test_build_push_plan_clean_when_matches(tmp_path):
    folder = _make_record(tmp_path, "sys_script", {"script": "v1"}, {"script": "v1"})
    plan = sync.build_push_plan(_RecClient({"script": "v1"}), folder)
    assert plan.changes == [] and plan.drifted == [] and plan.missing is False


def test_build_push_plan_detects_edit_and_no_drift(tmp_path):
    folder = _make_record(tmp_path, "sys_script", {"script": "v2"}, {"script": "v1"})
    plan = sync.build_push_plan(_RecClient({"script": "v1"}), folder)  # instance == snapshot
    assert [c.field for c in plan.changes] == ["script"] and plan.drifted == []


def test_build_push_plan_flags_drift_on_edited_field(tmp_path):
    folder = _make_record(tmp_path, "sys_script", {"script": "v2"}, {"script": "v1"})
    plan = sync.build_push_plan(_RecClient({"script": "REMOTE"}), folder)  # instance changed
    assert "script" in plan.drifted


def test_build_push_plan_missing(tmp_path):
    folder = _make_record(tmp_path, "sys_script", {"script": "v2"}, {"script": "v1"})
    plan = sync.build_push_plan(_RecClient(None), folder)
    assert plan.missing is True


def test_apply_push_blocks_on_drift(tmp_path):
    folder = _make_record(tmp_path, "sys_script", {"script": "v2"}, {"script": "v1"})
    c = _RecClient({"script": "REMOTE"})
    import pytest
    with pytest.raises(RuntimeError):
        sync.apply_push(c, sync.build_push_plan(c, folder))


def test_apply_push_writes_changed_fields(tmp_path):
    folder = _make_record(tmp_path, "sys_script", {"script": "v2"}, {"script": "v1"})
    c = _RecClient({"script": "v1"})  # instance matches snapshot -> clean
    sync.apply_push(c, sync.build_push_plan(c, folder))
    assert c.put_called == ("sys_script", "a"*32, {"script": "v2"})


def test_apply_push_unedited_drift_does_not_block(tmp_path):
    """A field that drifted on the instance but was NOT locally edited must not block the push.

    Setup: local edits 'script'; instance has 'client_script' drifted vs snapshot but
    'script' matches snapshot.  apply_push must proceed and PATCH only {script: ...}.
    """
    folder = _make_record(
        tmp_path, "sp_widget",
        # local files: script edited, client_script matches snapshot
        {"script": "local_edited", "client_script": "C_orig"},
        # snapshot
        {"script": "snap_orig", "client_script": "C_orig"},
    )
    # Instance: script matches snapshot (no drift on edited field), client_script drifted
    instance_record = {"script": "snap_orig", "client_script": "C_DRIFTED"}
    c = _RecClient(instance_record)
    plan = sync.build_push_plan(c, folder)

    # client_script is drifted but NOT in the changed set (not locally edited)
    assert "client_script" in plan.drifted
    assert not any(ch.field == "client_script" for ch in plan.changes)

    # apply_push must NOT raise and must write only the locally-changed field
    sync.apply_push(c, plan)  # must not raise
    assert c.put_called == ("sp_widget", "a"*32, {"script": "local_edited"})


def test_apply_push_uses_capturing_put_not_patch(tmp_path):
    """The push write must go through the update-set-capturing path (HTTP PUT),
    not a raw PATCH. A PATCH to the Table API does not fire ServiceNow's customer-
    update engine for records with sys_customer_update=false, so the change lands
    live but never captures into the current update set. PUT mirrors the fork's
    session-aware SN-Update-Record, which does capture."""
    folder = _make_record(tmp_path, "sys_script", {"script": "v2"}, {"script": "v1"})
    c = _RecClient({"script": "v1"})  # instance matches snapshot -> clean, no drift
    sync.apply_push(c, sync.build_push_plan(c, folder))
    assert c.put_called == ("sys_script", "a"*32, {"script": "v2"})
    assert c.patched is None  # must NOT use the non-capturing PATCH


def test_apply_push_raises_on_missing(tmp_path):
    """apply_push raises RuntimeError when the plan marks the record as missing."""
    folder = _make_record(tmp_path, "sys_script", {"script": "v2"}, {"script": "v1"})
    c = _RecClient(None)  # get_record returns None -> missing
    plan = sync.build_push_plan(c, folder)
    assert plan.missing is True
    import pytest
    with pytest.raises(RuntimeError):
        sync.apply_push(c, plan)
