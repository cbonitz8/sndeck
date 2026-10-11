import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sndeck import refresh, sync
from sndeck.registry import field_extension
from sndeck.store import Store

HEX = "0123456789abcdef0123456789abcdef"  # 32 hex


def _ext(f="script"):
    return field_extension(f)


def _make_record(set_dir, table, name, sysid, *, local, snapshot):
    """Create a pulled-record folder under <set_dir>/<table>/<name>__<sysid>.

    `snapshot` is the dict written to .snapshot.json and mirrored into record.json.
    `local` maps script-field -> local file contents.
    """
    folder = Path(set_dir) / table / f"{name}__{sysid}"
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps(
        {"_meta": {"table": table, "sys_id": sysid, "name": name, "pulled_at": "t"},
         **snapshot}))
    (folder / ".snapshot.json").write_text(json.dumps(snapshot))
    for f, val in local.items():
        (folder / f"{f}{field_extension(f)}").write_text(val)
    return folder


def _set_dir(root, slug, sysid):
    d = Path(root) / f"{slug}__{sysid}"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------- planner

def test_plan_flags_stale_snapshot_but_matching_local(tmp_path):
    """The phantom-dirty shape: local == instance, snapshot frozen at an old value."""
    ws = _set_dir(tmp_path, "s", "1" + HEX[1:])
    folder = _make_record(ws, "sys_script", "R", "a" * 32,
                          local={"script": "NEW"}, snapshot={"script": "OLD"})
    plan = refresh.plan_refresh(folder, {"script": "NEW"})
    assert plan.snapshot_stale == ["script"]
    assert plan.local_diverged == []


def test_plan_flags_local_divergence(tmp_path):
    ws = _set_dir(tmp_path, "s", "1" + HEX[1:])
    folder = _make_record(ws, "sys_script", "R", "a" * 32,
                          local={"script": "LOCAL_EDIT"}, snapshot={"script": "OLD"})
    plan = refresh.plan_refresh(folder, {"script": "INSTANCE"})
    assert plan.local_diverged == ["script"]


# ---------------------------------------------------------------- apply: snapshot-only

def test_apply_snapshot_only_resolves_phantom_dirty(tmp_path):
    """Local already matches the instance; the stale snapshot kept it 'dirty'. A
    snapshot-only refresh rebases the snapshot and the record goes clean (prunable)."""
    ws = _set_dir(tmp_path, "s", "1" + HEX[1:])
    folder = _make_record(ws, "sys_script", "R", "a" * 32,
                          local={"script": "NEW"}, snapshot={"script": "OLD"})
    assert sync.is_dirty(folder) is True                      # phantom dirty before
    out = refresh.apply_refresh(folder, {"script": "NEW", "sys_id": "a" * 32})
    assert out.refreshed and not out.refused and out.clean_after
    assert out.snapshot_changed == ["script"] and out.local_changed == []
    assert json.loads((folder / ".snapshot.json").read_text())["script"] == "NEW"
    assert sync.is_dirty(folder) is False                     # resolved -> prunable


def test_apply_snapshot_only_does_not_touch_local_or_record_json(tmp_path):
    ws = _set_dir(tmp_path, "s", "1" + HEX[1:])
    folder = _make_record(ws, "sys_script", "R", "a" * 32,
                          local={"script": "NEW"}, snapshot={"script": "OLD"})
    before_local = (folder / f"script{_ext()}").read_text()
    before_rj = (folder / "record.json").read_text()
    refresh.apply_refresh(folder, {"script": "NEW"})
    assert (folder / f"script{_ext()}").read_text() == before_local
    assert (folder / "record.json").read_text() == before_rj


def test_apply_refuses_when_local_diverges(tmp_path):
    """Rebasing the snapshot alone here would leave the record dirty AND retire push's
    drift guard, so refresh refuses without --overwrite-local. Nothing is written."""
    ws = _set_dir(tmp_path, "s", "1" + HEX[1:])
    folder = _make_record(ws, "sys_script", "R", "a" * 32,
                          local={"script": "LOCAL_EDIT"}, snapshot={"script": "OLD"})
    snap_before = (folder / ".snapshot.json").read_text()
    out = refresh.apply_refresh(folder, {"script": "INSTANCE"})
    assert out.refused and not out.refreshed and not out.clean_after
    assert "overwrite-local" in out.reason
    assert (folder / ".snapshot.json").read_text() == snap_before   # untouched


# ---------------------------------------------------------------- apply: --overwrite-local

def test_apply_overwrite_local_takes_instance_copy(tmp_path):
    ws = _set_dir(tmp_path, "s", "1" + HEX[1:])
    folder = _make_record(ws, "sys_script", "R", "a" * 32,
                          local={"script": "LOCAL_EDIT"}, snapshot={"script": "OLD"})
    out = refresh.apply_refresh(folder, {"script": "INSTANCE", "sys_id": "a" * 32},
                                overwrite_local=True)
    assert out.refreshed and not out.refused and out.clean_after
    assert out.local_changed == ["script"]
    assert (folder / f"script{_ext()}").read_text() == "INSTANCE"
    assert json.loads((folder / ".snapshot.json").read_text())["script"] == "INSTANCE"
    # record.json refreshed to the instance too
    assert json.loads((folder / "record.json").read_text())["script"] == "INSTANCE"
    assert sync.is_dirty(folder) is False


def test_apply_overwrite_local_removes_field_emptied_on_instance(tmp_path):
    ws = _set_dir(tmp_path, "s", "1" + HEX[1:])
    folder = _make_record(ws, "sys_script", "R", "a" * 32,
                          local={"script": "SOMETHING"}, snapshot={"script": "SOMETHING"})
    out = refresh.apply_refresh(folder, {"script": "", "sys_id": "a" * 32},
                                overwrite_local=True)
    assert out.refreshed
    assert not (folder / f"script{_ext()}").exists()          # emptied field file removed


def test_missing_outcome_touches_nothing(tmp_path):
    ws = _set_dir(tmp_path, "s", "1" + HEX[1:])
    folder = _make_record(ws, "sys_script", "R", "a" * 32,
                          local={"script": "v"}, snapshot={"script": "v"})
    o = refresh.missing_outcome(folder, "sys_script", "a" * 32, "R")
    assert o.missing and not o.refreshed
    assert (folder / ".snapshot.json").read_text() == json.dumps({"script": "v"})


# ---------------------------------------------------------------- CLI

class _FakeClient:
    """get_record by (table, sys_id); None => record was deleted upstream."""
    instance = SimpleNamespace(name="dev")

    def __init__(self, records):
        self._records = records
    def get_record(self, table, sys_id, **kw):
        return self._records.get((table, sys_id))


def _copy(root, name, sysid, *, local, snapshot):
    """An edit copy on dev whose target set has shipped; the CLI refreshes regardless."""
    from storekit import make_copy
    folder = make_copy(root, "dev", "sys_script", name, sysid, script=snapshot,
                       target_set={"sys_id": "1" + HEX[1:], "name": "shipped"})
    (folder / f"script{_ext()}").write_text(local)
    return folder


def test_cmd_refresh_single_resolves_and_exits_zero(tmp_path, capsys):
    from sndeck import cli
    _copy(tmp_path, "R", "a" * 32, local="NEW", snapshot="OLD")
    client = _FakeClient({("sys_script", "a" * 32): {"script": "NEW", "sys_id": "a" * 32}})
    rc = cli.cmd_refresh(client, Store(tmp_path), "sys_script", "a" * 32, False, False,
                         as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out[0]["refreshed"] and out[0]["clean_after"]


def test_cmd_refresh_single_refused_exits_one(tmp_path, capsys):
    from sndeck import cli
    _copy(tmp_path, "R", "a" * 32, local="LOCAL_EDIT", snapshot="OLD")
    client = _FakeClient({("sys_script", "a" * 32): {"script": "INSTANCE"}})
    rc = cli.cmd_refresh(client, Store(tmp_path), "sys_script", "a" * 32, False, False,
                         as_json=True)
    assert rc == 1


def test_cmd_refresh_not_found_exits_one(tmp_path):
    from sndeck import cli
    client = _FakeClient({})
    rc = cli.cmd_refresh(client, Store(tmp_path), "sys_script", "z" * 32, False, False,
                         as_json=True)
    assert rc == 1


def test_cmd_refresh_record_deleted_upstream_exits_one(tmp_path, capsys):
    from sndeck import cli
    _copy(tmp_path, "R", "a" * 32, local="v", snapshot="v")
    rc = cli.cmd_refresh(_FakeClient({}), Store(tmp_path), "sys_script", "a" * 32, False,
                         False, as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out[0]["missing"] is True


def test_cmd_refresh_all_is_best_effort_zero(tmp_path, capsys):
    from sndeck import cli
    _copy(tmp_path, "Clean", "a" * 32, local="NEW", snapshot="OLD")        # will resolve
    _copy(tmp_path, "Diverged", "b" * 32, local="LOCAL", snapshot="OLD")   # will refuse
    client = _FakeClient({
        ("sys_script", "a" * 32): {"script": "NEW"},
        ("sys_script", "b" * 32): {"script": "INSTANCE"},
    })
    rc = cli.cmd_refresh(client, Store(tmp_path), None, None, True, False, as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    by_name = {o["name"]: o for o in out}
    assert by_name["Clean"]["refreshed"] is True
    assert by_name["Diverged"]["refused"] is True


def test_overwrite_local_keeps_meta_extras(tmp_path):
    from sndeck import snapshot
    from sndeck.refresh import apply_refresh
    from storekit import SET_A, make_copy
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", dirty=True)
    apply_refresh(f, {"sys_id": "111", "name": "A", "script": "live"}, overwrite_local=True)
    assert snapshot.read_meta_extra(f) == {"instance": "dev", "kind": "edit",
                                           "target_set": SET_A}
