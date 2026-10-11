import json
import os
from pathlib import Path

import httpx
import pytest
from sndeck.rest import TableClient
from sndeck.config import Instance
from sndeck import cli
from sndeck.store import Store
from sndeck.updatesets import Capture
from storekit import SET_A, make_copy, make_legacy, sid

_INST = Instance("dev", "https://x.service-now.com", "cid",
                 "https://x.service-now.com/oauth_token.do", "cbonitz@x")


class _Tok:
    def access_token(self): return "AT"
    def invalidate(self): pass


def _client(routes):
    def handler(req):
        table = str(req.url.path).rsplit("/", 1)[-1]
        return httpx.Response(200, json={"result": routes(table, dict(req.url.params))})
    return TableClient(_INST, _Tok(), http=httpx.Client(transport=httpx.MockTransport(handler)))


def _routes_current_set(table, params):
    if table == "sys_user":
        return [{"sys_id": "U1", "user_name": "cbonitz"}]
    if table == "sys_user_preference":
        return [{"value": "SET1"}]
    if table == "sys_update_set":
        return [{"sys_id": {"value": "SET1"}, "name": {"value": "My Set"},
                 "state": {"value": "in progress", "display_value": "In progress"},
                 "application": {"display_value": "Global"}}]
    return []


def _routes_no_set(table, params):
    if table == "sys_user":
        return [{"sys_id": "U1", "user_name": "cbonitz"}]
    return []   # no sys_update_set pref -> no current set


def test_us_get_human(capsys):
    rc = cli.cmd_us_get(_client(_routes_current_set), as_json=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "My Set" in out and "SET1" in out and "Global" in out


def test_us_get_json(capsys):
    rc = cli.cmd_us_get(_client(_routes_current_set), as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert obj == {"sys_id": "SET1", "name": "My Set",
                   "state": "In progress", "scope": "Global"}


def test_us_get_none(capsys):
    rc = cli.cmd_us_get(_client(_routes_no_set), as_json=True)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) is None


def _routes_two_sets(table, params):
    if table == "sys_update_set":
        return [
            {"sys_id": {"value": "S1"}, "name": {"value": "Alpha"},
             "state": {"value": "in progress", "display_value": "In progress"},
             "application": {"display_value": "Global"}},
            {"sys_id": {"value": "S2"}, "name": {"value": "Beta"},
             "state": {"value": "in progress", "display_value": "In progress"},
             "application": {"display_value": "Dealer Recruiting"}},
        ]
    return []


def test_us_ls_json(capsys, tmp_path):
    rc = cli.cmd_us_ls(_client(_routes_two_sets), Store(tmp_path), as_json=True)
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [r["sys_id"] for r in rows] == ["S1", "S2"]
    assert rows[1]["scope"] == "Dealer Recruiting"


def test_us_ls_runs_reconcile_and_reports_to_stderr(monkeypatch, capsys, tmp_path):
    called = {}
    store = Store(tmp_path)

    def fake_reconcile(client, st):
        called["store"] = st
        return ["gc: removed 1 copy whose set shipped: sys_script/A"]
    monkeypatch.setattr(cli, "reconcile_and_report", fake_reconcile)
    monkeypatch.setattr(cli, "list_update_sets", lambda client: [])

    rc = cli.cmd_us_ls(object(), store, as_json=True)

    assert rc == 0 and called["store"] is store
    err = capsys.readouterr().err
    assert "gc: removed 1 copy whose set shipped: sys_script/A" in err


def test_reconcile_failure_never_breaks_command(monkeypatch, capsys, tmp_path):
    from sndeck import gc
    # reconcile_and_report owns the never-raise contract itself: make the real
    # planner inside it blow up, with no wrapper guard in cli._run_reconcile
    # backstopping it, and confirm the command still succeeds.
    make_copy(tmp_path, "dev", "sys_script", "A", "111")

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(gc, "plan_gc", boom)
    monkeypatch.setattr(cli, "list_update_sets", lambda client: [])
    rc = cli.cmd_us_ls(_client(_routes_two_sets), Store(tmp_path), as_json=True)
    assert rc == 0
    assert (tmp_path / "dev" / "sys_script" / "A__111").exists()


def test_us_set_switches(capsys):
    calls = {}

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": "S1"}, "name": {"value": "Alpha"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_user_preference":
            return []
        return []

    import sndeck.cli as climod
    orig = climod.switch_current_set
    climod.switch_current_set = lambda c, sid: calls.setdefault("args", sid) and True
    try:
        rc = climod.cmd_us_set(_client(routes), "S1", as_json=True)
    finally:
        climod.switch_current_set = orig
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert calls["args"] == "S1"
    assert obj["sys_id"] == "S1" and obj["name"] == "Alpha"


def test_us_set_unknown_id(capsys):
    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        return []   # update_set_meta finds nothing
    rc = cli.cmd_us_set(_client(routes), "NOPE", as_json=False)
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found" in err


def _routes_pull(record):
    a = "a" * 32

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": "SET1"}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": "SET1"}, "name": {"value": "My Set"},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if table == "sys_script_include":
            return [record] if record else []
        return []
    return routes, a


def test_pull_writes_edit_copy_into_store(tmp_path, capsys):
    routes, a = _routes_pull({"sys_id": "a" * 32, "name": "MyInclude", "script": "gs.log('hi');"})
    rc = cli.cmd_pull(_client(routes), Store(tmp_path), "sys_script_include", a,
                      ref=False, edit=False, overwrite_local=False, as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert obj["table"] == "sys_script_include" and obj["sys_id"] == a
    assert "script.js" in obj["files"]
    assert (tmp_path / "dev" / "sys_script_include" / f"MyInclude__{a}" / "script.js").exists()


def test_pull_no_current_set(tmp_path, capsys):
    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        return []
    rc = cli.cmd_pull(_client(routes), Store(tmp_path), "sys_script_include", "a" * 32,
                      ref=False, edit=False, overwrite_local=False, as_json=False)
    assert rc == 1
    assert "no current update set" in capsys.readouterr().err


def test_pull_record_not_found(tmp_path, capsys):
    routes, a = _routes_pull(None)
    rc = cli.cmd_pull(_client(routes), Store(tmp_path), "sys_script_include", a,
                      ref=False, edit=False, overwrite_local=False, as_json=False)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


REC = "a" * 32
OTHER = {"sys_id": sid("c"), "name": "Other Set"}


def _set_row(set_id, name, state="in progress"):
    return {"sys_id": {"value": set_id}, "name": {"value": name, "display_value": name},
            "state": {"value": state, "display_value": state},
            "application": {"value": "global", "display_value": "Global"}}


def _routes_status(current, sets, captures):
    """current: the user's current set sys_id; sets: rows served for sys_update_set;
    captures: (table, sys_id, set_id) triples served for sys_update_xml."""
    def routes(t, params):
        if t == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if t == "sys_user_preference":
            return [{"value": current}] if current else []
        if t == "sys_update_set":
            q = params.get("sysparm_query", "")
            if q.startswith("sys_id=") and "IN" not in q:
                wanted = q[len("sys_id="):]
                return [r for r in sets if r["sys_id"]["value"] == wanted]
            return sets
        if t == "sys_update_xml":
            return [{"name": f"{tb}_{s}", "update_set": {"value": us}}
                    for tb, s, us in captures]
        return []
    return routes


def test_status_reports_dirty(tmp_path, capsys):
    make_copy(tmp_path, "dev", "sys_script_include", "Thing", REC, dirty=True)
    routes = _routes_status(SET_A["sys_id"], [_set_row(SET_A["sys_id"], "Set A")],
                            [("sys_script_include", REC, SET_A["sys_id"])])
    rc = cli.cmd_status(_client(routes), Store(tmp_path), as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert obj["current_set"]["sys_id"] == SET_A["sys_id"]
    [group] = obj["groups"]
    assert group["set"] == SET_A
    [rec] = group["records"]
    assert rec["state"] == "dirty" and rec["fields_changed"] == ["script"]


def test_status_groups_by_target_set_with_current_first(tmp_path, capsys):
    make_copy(tmp_path, "dev", "sys_script", "Clean", "1" * 32, target_set=SET_A)
    make_copy(tmp_path, "dev", "sys_script", "Lonely", "2" * 32, target_set=OTHER)
    routes = _routes_status(
        OTHER["sys_id"],
        [_set_row(SET_A["sys_id"], "Set A"), _set_row(OTHER["sys_id"], "Other Set")],
        [("sys_script", "1" * 32, SET_A["sys_id"]), ("sys_script", "1" * 32, OTHER["sys_id"])])
    rc = cli.cmd_status(_client(routes), Store(tmp_path), as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [g["set"]["name"] for g in obj["groups"]] == ["Other Set", "Set A"]
    lonely = obj["groups"][0]["records"][0]
    clean = obj["groups"][1]["records"][0]
    assert lonely["state"] == "local-only" and lonely["other_in_progress_sets"] == []
    assert clean["state"] == "clean" and clean["other_in_progress_sets"] == ["Other Set"]


def test_status_human_marks_records_also_in_other_sets(tmp_path, capsys):
    make_copy(tmp_path, "dev", "sys_script", "Clean", "1" * 32, target_set=SET_A)
    routes = _routes_status(
        SET_A["sys_id"],
        [_set_row(SET_A["sys_id"], "Set A"), _set_row(OTHER["sys_id"], "Other Set")],
        [("sys_script", "1" * 32, SET_A["sys_id"]), ("sys_script", "1" * 32, OTHER["sys_id"])])
    rc = cli.cmd_status(_client(routes), Store(tmp_path), as_json=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert f"→ Set A ({SET_A['sys_id']})" in out
    assert "✓ clean" in out and "⚠ also in: Other Set" in out
    assert "clean — nothing to push" in out


def test_status_no_current_set(tmp_path, capsys):
    def routes(t, params):
        if t == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        return []
    rc = cli.cmd_status(_client(routes), Store(tmp_path), as_json=True)
    assert rc == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj == {"instance": "dev", "current_set": None, "groups": []}


def _stub_push(monkeypatch, capture_set, capture_name, pushed):
    from sndeck.sync import PushPlan
    monkeypatch.setattr("sndeck.push.build_push_plan",
                        lambda c, path: PushPlan("sys_script_include", REC, "Thing", [], [], False))
    monkeypatch.setattr("sndeck.push.apply_push", lambda c, plan: pushed.append(plan.sys_id))
    monkeypatch.setattr("sndeck.push.set_scope_pointer", lambda *a, **k: None)
    monkeypatch.setattr("sndeck.push.set_current_application", lambda *a, **k: None)
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: Capture("t", "Script Include", "Thing",
                                                capture_name, capture_set))


def _routes_push():
    return _routes_status(SET_A["sys_id"], [_set_row(SET_A["sys_id"], "Set A")], [])


def test_push_single_dirty(tmp_path, capsys, monkeypatch):
    make_copy(tmp_path, "dev", "sys_script_include", "Thing", REC, dirty=True)
    pushed = []
    _stub_push(monkeypatch, SET_A["sys_id"], "Set A", pushed)
    rc = cli.cmd_push(_client(_routes_push()), Store(tmp_path), "sys_script_include", REC,
                      False, as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert pushed == [REC]
    assert obj[0]["pushed"] is True
    assert obj[0]["capture_error"] is None


def test_push_miscapture_fails_loudly(tmp_path, capsys, monkeypatch):
    make_copy(tmp_path, "dev", "sys_script_include", "Thing", REC, dirty=True)
    _stub_push(monkeypatch, "G" * 32, "Stray Global", [])
    rc = cli.cmd_push(_client(_routes_push()), Store(tmp_path), "sys_script_include", REC,
                      False, as_json=False)
    out = capsys.readouterr()
    assert rc == 1
    assert "Stray Global" in out.err and "G" * 32 in out.err


def test_push_all_pushes_only_this_instances_dirty_edit_copies(tmp_path, capsys, monkeypatch):
    make_copy(tmp_path, "dev", "sys_script_include", "Thing", REC, dirty=True)
    make_copy(tmp_path, "dev", "sys_script_include", "Clean", "b" * 32)
    make_copy(tmp_path, "prod", "sys_script_include", "Prod", "c" * 32, dirty=True)
    make_copy(tmp_path, "dev", "sys_script_include", "Ref", "d" * 32, kind="ref", dirty=True)
    pushed = []
    seen = []
    _stub_push(monkeypatch, SET_A["sys_id"], "Set A", pushed)
    import sndeck.push as push_mod
    real = push_mod.push_all
    monkeypatch.setattr(cli, "push_all", lambda c, paths: seen.extend(paths) or real(c, paths))
    rc = cli.cmd_push(_client(_routes_push()), Store(tmp_path), None, None, True, as_json=True)
    assert rc == 0
    assert seen == [tmp_path / "dev" / "sys_script_include" / f"Thing__{REC}"]


def test_push_without_edit_copy(tmp_path, capsys):
    rc = cli.cmd_push(_client(_routes_push()), Store(tmp_path), "sys_script_include", "z" * 32,
                      False, as_json=False)
    assert rc == 1
    assert "no edit copy of sys_script_include/" in capsys.readouterr().err


def test_push_with_only_reference_copy_says_pull_edit(tmp_path, capsys):
    make_copy(tmp_path, "dev", "sys_script_include", "Thing", REC, kind="ref", dirty=True)
    rc = cli.cmd_push(_client(_routes_push()), Store(tmp_path), "sys_script_include", REC,
                      False, as_json=False)
    assert rc == 1
    assert "only a reference copy" in capsys.readouterr().err


def test_push_ignores_other_instances_copy(tmp_path, capsys):
    make_copy(tmp_path, "prod", "sys_script_include", "Thing", REC, dirty=True)
    rc = cli.cmd_push(_client(_routes_push()), Store(tmp_path), "sys_script_include", REC,
                      False, as_json=False)
    assert rc == 1
    assert "on dev" in capsys.readouterr().err


def test_push_all_clean(tmp_path, capsys):
    make_copy(tmp_path, "dev", "sys_script_include", "Thing", REC)
    rc = cli.cmd_push(_client(_routes_push()), Store(tmp_path), None, None, True, as_json=False)
    assert rc == 0
    assert "nothing to push" in capsys.readouterr().out


def test_push_requires_target(tmp_path, capsys):
    rc = cli.cmd_push(_client(_routes_push()), Store(tmp_path), None, None, False, as_json=False)
    assert rc == 1
    assert "requires" in capsys.readouterr().err


def test_refresh_without_edit_copy_fails(tmp_path, capsys):
    make_copy(tmp_path, "dev", "sys_script", "A", "111", kind="ref")
    rc = cli.cmd_refresh(_client(_routes_push()), Store(tmp_path), "sys_script", "111",
                         False, False, as_json=False)
    assert rc == 1 and "no edit copy of sys_script/111 on dev" in capsys.readouterr().err


def test_refresh_all_rebases_only_this_instances_edit_copies(tmp_path, capsys):
    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    make_copy(tmp_path, "prod", "sys_script", "P", "222")

    def routes(t, params):
        if t == "sys_script":
            return [{"sys_id": "111", "name": "A", "script": "gs.info('new');"}]
        return []
    rc = cli.cmd_refresh(_client(routes), Store(tmp_path), None, None, True, True,
                         as_json=True)
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0 and len(rows) == 1 and rows[0]["refreshed"] is True
    assert (tmp_path / "dev" / "sys_script" / "A__111" / "script.js").read_text() == "gs.info('new');"


# ---------------------------------------------------------------------------
# dispatch() seam tests
# ---------------------------------------------------------------------------

def test_dispatch_us_get_routes(tmp_path, capsys):
    rc = cli.dispatch(["us", "get"], client_factory=lambda name: _client(_routes_current_set))
    assert rc == 0


def test_dispatch_us_ls_routes(tmp_path, capsys):
    rc = cli.dispatch(["us", "ls"], client_factory=lambda name: _client(_routes_two_sets))
    assert rc == 0


def test_dispatch_us_ls_keeps_copy_whose_set_is_absent_from_response(tmp_path, capsys):
    """Regression: `us ls` reconciles the resolved store. A set missing from a non-empty
    state response is not proof it was deleted, so its clean copy stays."""
    root = os.environ["SNDECK_SCRATCH"]
    folder = make_copy(root, "dev", "sys_script_include", "Thing", "a" * 32)
    rc = cli.dispatch(["us", "ls"], client_factory=lambda name: _client(_routes_two_sets))
    assert rc == 0
    assert folder.is_dir()
    assert "not found on dev" in capsys.readouterr().err


def _routes_us_set(table, params):
    if table == "sys_user":
        return [{"sys_id": "U1", "user_name": "cbonitz"}]
    if table == "sys_update_set":
        return [{"sys_id": {"value": "S1"}, "name": {"value": "Alpha"},
                 "state": {"value": "in progress", "display_value": "In progress"},
                 "application": {"display_value": "Global"}}]
    if table == "sys_user_preference":
        return []
    return []


def test_dispatch_us_set_routes(tmp_path, capsys, monkeypatch):
    import sndeck.cli as climod
    orig = climod.switch_current_set
    climod.switch_current_set = lambda c, sid: True
    try:
        rc = cli.dispatch(["us", "set", "S1"],
                          client_factory=lambda name: _client(_routes_us_set))
    finally:
        climod.switch_current_set = orig
    assert rc == 0


def test_dispatch_instance_flag_reaches_factory(capsys, monkeypatch):
    """--instance flag overrides resolution and is passed to client_factory."""
    import sndeck.cli as climod
    recorded = {}
    monkeypatch.setattr(climod, "switch_current_set", lambda c, sid: True)

    def capture(name):
        recorded["name"] = name
        return _client(_routes_us_set)

    cli.dispatch(["us", "set", "S1", "--instance", "dev"],
                 client_factory=capture)
    assert recorded.get("name") == "dev"


def test_dispatch_json_flag_produces_valid_json(capsys, monkeypatch):
    """--json flag causes output to be valid JSON."""
    rc = cli.dispatch(["us", "get", "--json"],
                      client_factory=lambda name: _client(_routes_current_set))
    assert rc == 0
    out = capsys.readouterr().out
    import json as _json
    obj = _json.loads(out)   # raises if not valid JSON
    assert obj is not None


def test_dispatch_missing_subcommand_exits_2():
    """Argparse exits with code 2 when a required subcommand is omitted."""
    with pytest.raises(SystemExit) as exc_info:
        cli.dispatch(["us"], client_factory=lambda name: _client(_routes_current_set))
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# __main__ gate classification tests
# ---------------------------------------------------------------------------

def test_main_subcommands_set():
    from sndeck.__main__ import _SUBCOMMANDS
    assert {"us", "pull", "status", "push", "refresh", "retarget", "gc", "migrate"} <= _SUBCOMMANDS


def test_main_subcommands_excludes_non_subcommands():
    from sndeck.__main__ import _SUBCOMMANDS
    assert "." not in _SUBCOMMANDS
    assert "--json" not in _SUBCOMMANDS
    assert "us" in _SUBCOMMANDS


# ---------------------------------------------------------------------------
# Store cutover: pull / retarget / gc / migrate / dispatch root check
# ---------------------------------------------------------------------------

def test_dispatch_refuses_legacy_root_from_env(tmp_path, monkeypatch, capsys):
    root = tmp_path / "vendor-hotfix"
    make_legacy(root / f"rel__{sid('b')}", "sys_script", "A", "111")
    monkeypatch.setenv("SNDECK_SCRATCH", str(root))
    rc = cli.dispatch(["status"], client_factory=lambda n: _client(_routes_current_set))
    err = capsys.readouterr().err
    assert rc == 1 and "legacy per-task root" in err and "SNDECK_SCRATCH" in err


def test_pull_dev_defaults_to_edit_and_json_reports_action(tmp_path, capsys, sn_client):
    def routes(table, params):
        if table == "sys_script":
            return [{"sys_id": "111", "name": "A", "script": "a"}]
        return _routes_current_set(table, params)
    rc = cli.cmd_pull(sn_client(routes), Store(tmp_path), "sys_script", "111",
                      ref=False, edit=False, overwrite_local=False, as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0 and obj["action"] == "created" and obj["kind"] == "edit"
    assert obj["target_set"] == {"sys_id": "SET1", "name": "My Set"}


def test_pull_refused_exits_1(tmp_path, capsys, sn_client):
    def routes(table, params):
        if table == "sys_script":
            return [{"sys_id": "111", "name": "A", "script": "a"}]
        return _routes_current_set(table, params)
    c = sn_client(routes)
    cli.cmd_pull(c, Store(tmp_path), "sys_script", "111", ref=False, edit=False,
                 overwrite_local=False, as_json=True)
    (tmp_path / "dev" / "sys_script" / "A__111" / "script.js").write_text("mine")
    capsys.readouterr()
    rc = cli.cmd_pull(c, Store(tmp_path), "sys_script", "111", ref=False, edit=False,
                      overwrite_local=False, as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 1 and obj["action"] == "refused" and obj["blocked_fields"] == ["script"]


def test_pull_ref_and_edit_are_exclusive(tmp_path, capsys, sn_client):
    rc = cli.cmd_pull(sn_client(_routes_current_set), Store(tmp_path), "sys_script", "1",
                      ref=True, edit=True, overwrite_local=False, as_json=False)
    assert rc == 1 and "exclusive" in capsys.readouterr().err


def test_pull_ref_writes_reference_copy_without_target_set(tmp_path, capsys, sn_client):
    def routes(table, params):
        if table == "sys_script":
            return [{"sys_id": "111", "name": "A", "script": "a"}]
        return []
    rc = cli.cmd_pull(sn_client(routes), Store(tmp_path), "sys_script", "111",
                      ref=True, edit=False, overwrite_local=False, as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0 and obj["kind"] == "ref" and obj["target_set"] is None
    assert (tmp_path / "dev" / "_ref" / "sys_script" / "A__111" / "script.js").exists()


def test_pull_refresh_into_other_set_hints_retarget(tmp_path, capsys, sn_client):
    make_copy(tmp_path, "dev", "sys_script", "A", "111", target_set=SET_A)

    def routes(table, params):
        if table == "sys_script":
            return [{"sys_id": "111", "name": "A", "script": "gs.info('x');"}]
        return _routes_current_set(table, params)
    rc = cli.cmd_pull(sn_client(routes), Store(tmp_path), "sys_script", "111",
                      ref=False, edit=False, overwrite_local=False, as_json=False)
    out = capsys.readouterr().out
    assert rc == 0 and "refreshed edit copy" in out
    assert "target set stays 'Set A'" in out and "sndeck retarget sys_script 111" in out


def test_retarget_refuses_shipped_set(tmp_path, capsys, sn_client):
    make_copy(tmp_path, "dev", "sys_script", "A", "111")

    def routes(table, params):
        if table == "sys_update_set":
            return [{"sys_id": {"value": "S9"}, "name": {"value": "Old"},
                     "state": {"value": "complete"}, "application": {"display_value": "Global"}}]
        return []
    rc = cli.cmd_retarget(sn_client(routes), Store(tmp_path), "sys_script", "111", "S9",
                          as_json=False)
    assert rc == 1 and "complete" in capsys.readouterr().err


def test_retarget_defaults_to_current_set(tmp_path, capsys, sn_client):
    folder = make_copy(tmp_path, "dev", "sys_script", "A", "111")
    rc = cli.cmd_retarget(sn_client(_routes_current_set), Store(tmp_path), "sys_script",
                          "111", None, as_json=True)
    obj = json.loads(capsys.readouterr().out)
    meta = json.loads((folder / "record.json").read_text())["_meta"]
    assert rc == 0 and obj["target_set"] == {"sys_id": "SET1", "name": "My Set"}
    assert meta["target_set"] == {"sys_id": "SET1", "name": "My Set"}


def test_retarget_without_edit_copy_fails(tmp_path, capsys, sn_client):
    rc = cli.cmd_retarget(sn_client(_routes_current_set), Store(tmp_path), "sys_script",
                          "111", "SET1", as_json=False)
    assert rc == 1 and "no edit copy" in capsys.readouterr().err


def _routes_set_a(state):

    def routes(table, params):
        if table == "sys_update_set":
            return [{"sys_id": {"value": SET_A["sys_id"]}, "name": {"value": "Set A"},
                     "state": {"value": state}, "application": {"display_value": "Global"}}]
        return []
    return routes


def test_gc_dry_run_lists_actions(tmp_path, capsys, sn_client):
    make_copy(tmp_path, "dev", "sys_script", "A", "111", target_set=SET_A)

    def routes(table, params):
        if table == "sys_update_set":
            return [{"sys_id": {"value": SET_A["sys_id"]}, "name": {"value": "Set A"},
                     "state": {"value": "complete"}, "application": {"display_value": "Global"}}]
        return []
    rc = cli.cmd_gc(sn_client(routes), Store(tmp_path), apply=False, attic_older_than=None,
                    as_json=False)
    out = capsys.readouterr().out
    assert rc == 0 and "plan  C1  delete" in out and "dry run" in out
    assert (tmp_path / "dev" / "sys_script" / "A__111").exists()


def test_gc_apply_deletes_and_reports_done(tmp_path, capsys, sn_client):
    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    rc = cli.cmd_gc(sn_client(_routes_set_a("complete")), Store(tmp_path), apply=True,
                    attic_older_than=None, as_json=True)
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0 and [(r["class"], r["applied"]) for r in rows] == [("C1", True)]
    assert not (tmp_path / "dev" / "sys_script" / "A__111").exists()


@pytest.mark.parametrize("n", [0, -3])
def test_gc_rejects_attic_threshold_below_one(tmp_path, capsys, sn_client, n):
    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    rc = cli.cmd_gc(sn_client(_routes_set_a("complete")), Store(tmp_path), apply=True,
                    attic_older_than=n, as_json=False)
    assert rc == 1 and "--attic-older-than" in capsys.readouterr().err
    assert (tmp_path / "dev" / "sys_script" / "A__111").exists()


def test_dispatch_gc_rejects_attic_threshold_below_one(capsys):
    rc = cli.dispatch(["gc", "--attic-older-than", "0"],
                      client_factory=lambda n: _client(_routes_current_set))
    assert rc == 1 and "--attic-older-than" in capsys.readouterr().err


def _legacy_routes(set_id):
    def routes(table, params):
        if table == "sys_update_set":
            return [{"sys_id": set_id, "name": "Rel", "state": "in progress"}]
        if table == "sys_script":
            return [{"sys_id": "111", "name": "A", "script": "gs.info('x');"}]
        return []
    return routes


def _tree(root):
    return sorted((str(p.relative_to(root)), p.read_bytes() if p.is_file() else None)
                  for p in root.rglob("*"))


def test_migrate_dry_run_writes_nothing(tmp_path, capsys, sn_client):
    set_id = sid("b")
    make_legacy(tmp_path / "task" / f"Rel__{set_id}", "sys_script", "A", "111")
    before = _tree(tmp_path)
    rc = cli.cmd_migrate(Store(tmp_path), [sn_client(_legacy_routes(set_id))],
                         apply=False, as_json=False)
    out = capsys.readouterr().out
    assert rc == 0 and "store" in out and "dry run: 1 legacy folders" in out
    assert _tree(tmp_path) == before


def test_migrate_apply_moves_legacy_copy_into_store(tmp_path, capsys, sn_client):
    set_id = sid("b")
    make_legacy(tmp_path / "task" / f"Rel__{set_id}", "sys_script", "A", "111")
    rc = cli.cmd_migrate(Store(tmp_path), [sn_client(_legacy_routes(set_id))],
                         apply=True, as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0 and obj["stored"] == 1 and obj["dropped"] == 0
    meta = json.loads((tmp_path / "dev" / "sys_script" / "A__111" / "record.json")
                      .read_text())["_meta"]
    assert meta["instance"] == "dev" and meta["target_set"] == {"sys_id": set_id, "name": "Rel"}
    assert (tmp_path / "task").is_dir() and not any((tmp_path / "task").iterdir())


def test_dispatch_migrate_dry_run_on_empty_store(capsys):
    rc = cli.dispatch(["migrate"], client_factory=lambda n: _client(_routes_current_set))
    assert rc == 0 and "dry run: 0 legacy folders" in capsys.readouterr().out


def test_dispatch_refuses_instance_folder_as_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SNDECK_SCRATCH", str(tmp_path / "dev"))
    rc = cli.dispatch(["status", "--json"],
                      client_factory=lambda n: _client(_routes_current_set))
    err = json.loads(capsys.readouterr().err)
    assert rc == 1 and "instance folder" in err["error"]


def test_dispatch_scratch_flag_names_its_source(tmp_path, capsys):
    root = tmp_path / "vendor-hotfix"
    make_legacy(root / f"rel__{sid('b')}", "sys_script", "A", "111")
    rc = cli.dispatch(["status", "--scratch", str(root)],
                      client_factory=lambda n: _client(_routes_current_set))
    assert rc == 1 and "(store root from --scratch)" in capsys.readouterr().err


def test_pull_refresh_of_copy_without_target_set_hints_retarget(tmp_path, capsys, sn_client):
    make_copy(tmp_path, "dev", "sys_script", "A", "111", target_set=None)

    def routes(table, params):
        if table == "sys_script":
            return [{"sys_id": "111", "name": "A", "script": "gs.info('x');"}]
        return _routes_current_set(table, params)
    rc = cli.cmd_pull(sn_client(routes), Store(tmp_path), "sys_script", "111",
                      ref=False, edit=False, overwrite_local=False, as_json=False)
    out = capsys.readouterr().out
    assert rc == 0 and "no target set" in out and "sndeck retarget sys_script 111" in out


def test_migrate_apply_with_clashing_destination_fails_cleanly(tmp_path, capsys, sn_client):
    set_id = sid("b")
    legacy = make_legacy(tmp_path / "task" / f"Rel__{set_id}", "sys_script", "A", "111")
    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    rc = cli.cmd_migrate(Store(tmp_path), [sn_client(_legacy_routes(set_id))],
                         apply=True, as_json=False)
    assert rc == 1 and "nothing was migrated" in capsys.readouterr().err
    assert legacy.is_dir()


def test_status_shows_copies_without_target_set_and_counts_their_dirt(tmp_path, capsys):
    make_copy(tmp_path, "dev", "sys_script", "Orphan", "3" * 32, target_set=None, dirty=True)
    routes = _routes_status(SET_A["sys_id"], [_set_row(SET_A["sys_id"], "Set A")], [])
    rc = cli.cmd_status(_client(routes), Store(tmp_path), as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    [group] = obj["groups"]
    assert group["set"] is None and group["label"] == cli.NO_TARGET_LABEL
    assert group["records"][0]["state"] == "dirty"
    assert group["records"][0]["fields_changed"] == ["script"]

    rc = cli.cmd_status(_client(routes), Store(tmp_path), as_json=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "→ (no target set — run `sndeck retarget`)" in out
    assert "✎ dirty" in out and "1 dirty" in out and "nothing to push" not in out


def _named_client(name, routes):
    inst = Instance(name, f"https://{name}.service-now.com", "cid",
                    f"https://{name}.service-now.com/oauth_token.do", name)

    def handler(req):
        table = str(req.url.path).rsplit("/", 1)[-1]
        return httpx.Response(200, json={"result": routes(table, dict(req.url.params))})
    return TableClient(inst, _Tok(), http=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def two_instances(tmp_path, monkeypatch):
    reg = tmp_path / "instances.json"
    reg.write_text(json.dumps({"instances": [{"name": "prod"}, {"name": "dev"}]}))
    monkeypatch.setenv("SNDECK_INSTANCES_CONFIG", str(reg))


def test_dispatch_migrate_for_prod_still_stores_edit_copy_on_dev(two_instances, capsys):
    root = Path(os.environ["SNDECK_SCRATCH"])
    set_id = sid("b")
    make_legacy(root / "task" / f"Rel__{set_id}", "sys_script", "A", "111")
    rc = cli.dispatch(["migrate", "--instance", "prod", "--apply"],
                      client_factory=lambda n: _named_client(n, _legacy_routes(set_id)))
    err = capsys.readouterr().err
    assert rc == 0 and "migrate reads instances dev, prod" in err
    assert (root / "dev" / "sys_script" / "A__111" / "record.json").exists()
    assert not (root / "prod").exists()


def test_dispatch_migrate_expired_token_fails_cleanly(two_instances, capsys):
    from sndeck.auth import AuthExpiredError

    def factory(n):
        if n == "prod":
            raise AuthExpiredError("prod token expired")
        return _named_client(n, _routes_current_set)
    rc = cli.dispatch(["migrate"], client_factory=factory)
    assert rc == 1 and "prod token expired" in capsys.readouterr().err


def _capture(monkeypatch, fn_name):
    seen = {}

    def fake(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return 0
    monkeypatch.setattr(cli, fn_name, fake)
    return seen


def test_dispatch_pull_flags_reach_cmd_pull(monkeypatch):
    seen = _capture(monkeypatch, "cmd_pull")
    cli.dispatch(["pull", "sys_script", "111", "--ref", "--overwrite-local"],
                 client_factory=lambda n: _client(_routes_current_set))
    assert seen["args"][2:] == ("sys_script", "111")
    assert seen["kwargs"] == {"ref": True, "edit": False, "overwrite_local": True,
                              "as_json": False}
    cli.dispatch(["pull", "sys_script", "111", "--edit"],
                 client_factory=lambda n: _client(_routes_current_set))
    assert seen["kwargs"] == {"ref": False, "edit": True, "overwrite_local": False,
                              "as_json": False}


def test_dispatch_retarget_set_reaches_cmd_retarget(monkeypatch):
    seen = _capture(monkeypatch, "cmd_retarget")
    cli.dispatch(["retarget", "sys_script", "111", "--set", "S"],
                 client_factory=lambda n: _client(_routes_current_set))
    assert seen["args"][2:] == ("sys_script", "111", "S")
    cli.dispatch(["retarget", "sys_script", "111"],
                 client_factory=lambda n: _client(_routes_current_set))
    assert seen["args"][2:] == ("sys_script", "111", None)


def test_dispatch_gc_flags_reach_cmd_gc(monkeypatch):
    seen = _capture(monkeypatch, "cmd_gc")
    cli.dispatch(["gc", "--apply", "--attic-older-than", "5"],
                 client_factory=lambda n: _client(_routes_current_set))
    assert seen["kwargs"] == {"apply": True, "attic_older_than": 5, "as_json": False}


def test_dispatch_migrate_apply_reaches_cmd_migrate(monkeypatch):
    seen = _capture(monkeypatch, "cmd_migrate")
    cli.dispatch(["migrate", "--apply", "--json"],
                 client_factory=lambda n: _client(_routes_current_set))
    assert seen["kwargs"] == {"apply": True, "as_json": True}


def test_migrate_dry_run_shows_target_set_root_summary_and_clashes(tmp_path, capsys, sn_client):
    set_id = sid("b")
    make_legacy(tmp_path / "task" / f"Rel__{set_id}", "sys_script", "A", "111")
    (tmp_path / "task" / "notes.md").write_text("n")
    rc = cli.cmd_migrate(Store(tmp_path), [sn_client(_legacy_routes(set_id))],
                         apply=False, as_json=False)
    out = capsys.readouterr().out
    assert rc == 0
    store_line = next(line for line in out.splitlines() if line.startswith("store"))
    assert "Rel" in store_line and set_id in store_line
    assert "task: store 1 · attic 0 · drop 0" in out
    assert "notes.md" in out
    assert "clash" not in out

    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    before = _tree(tmp_path)
    cli.cmd_migrate(Store(tmp_path), [sn_client(_legacy_routes(set_id))],
                    apply=False, as_json=False)
    out = capsys.readouterr().out
    assert "--apply would refuse" in out
    assert str(tmp_path / "dev" / "sys_script" / "A__111") in out
    assert _tree(tmp_path) == before


def test_pull_onto_damaged_copy_reports_damage_not_missing(tmp_path, capsys, sn_client):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", dirty=True)
    doc = json.loads((f / "record.json").read_text())
    del doc["_meta"]["target_set"]
    (f / "record.json").write_text(json.dumps(doc))

    def routes(table, params):
        if table == "sys_script":
            return [{"sys_id": "111", "name": "A", "script": "live"}]
        return _routes_current_set(table, params)
    rc = cli.cmd_pull(sn_client(routes), Store(tmp_path), "sys_script", "111",
                      ref=False, edit=False, overwrite_local=False, as_json=False)
    err = capsys.readouterr().err
    assert rc == 1
    assert f"damaged record.json at {f / 'record.json'}: missing target_set" in err
