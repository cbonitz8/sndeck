import json
import httpx
import pytest
from sndeck.rest import TableClient
from sndeck.config import Instance
from sndeck import cli

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
    rc = cli.cmd_us_ls(_client(_routes_two_sets), str(tmp_path), as_json=True)
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [r["sys_id"] for r in rows] == ["S1", "S2"]
    assert rows[1]["scope"] == "Dealer Recruiting"


def test_us_ls_runs_reconcile_and_reports_to_stderr(monkeypatch, capsys, tmp_path):
    from sndeck import cli

    called = {}
    def fake_reconcile(client, root):
        called["root"] = str(root)
        return ["pruned 1 shipped set workspace(s): shipped"]
    monkeypatch.setattr(cli, "reconcile_and_report", fake_reconcile)
    monkeypatch.setattr(cli, "list_update_sets", lambda client: [])

    rc = cli.cmd_us_ls(object(), str(tmp_path), as_json=True)

    assert rc == 0 and called["root"] == str(tmp_path)
    err = capsys.readouterr().err
    assert "pruned 1 shipped set workspace(s): shipped" in err


def test_reconcile_failure_never_breaks_command(monkeypatch, capsys, tmp_path):
    from sndeck import cli, prune
    # reconcile_and_report owns the never-raise contract itself: make the real
    # raiser inside it (reconcile_scratch) blow up, with no wrapper guard in
    # cli._run_reconcile backstopping it, and confirm the command still succeeds.
    def boom(client, root):
        raise RuntimeError("network down")
    monkeypatch.setattr(prune, "reconcile_scratch", boom)
    monkeypatch.setattr(cli, "list_update_sets", lambda client: [])
    rc = cli.cmd_us_ls(object(), str(tmp_path), as_json=True)
    assert rc == 0   # command still succeeds despite reconcile blowing up


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
    orig = climod.set_current_update_set
    climod.set_current_update_set = lambda c, u, sid: calls.setdefault("args", (u, sid))
    try:
        rc = climod.cmd_us_set(_client(routes), "S1", as_json=True)
    finally:
        climod.set_current_update_set = orig
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert calls["args"] == ("U1", "S1")
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


def test_pull_writes_workspace(tmp_path, capsys):
    a = "a" * 32
    routes, a = _routes_pull({"sys_id": a, "name": "MyInclude", "script": "gs.log('hi');"})
    rc = cli.cmd_pull(_client(routes), str(tmp_path), "sys_script_include", a, as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert obj["table"] == "sys_script_include" and obj["sys_id"] == a
    assert "script.js" in obj["files"]
    assert (tmp_path / "My Set__SET1" / "sys_script_include").exists()


def test_pull_no_current_set(tmp_path, capsys):
    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        return []
    rc = cli.cmd_pull(_client(routes), str(tmp_path), "sys_script_include", "a" * 32, as_json=False)
    assert rc == 1
    assert "no current update set" in capsys.readouterr().err


def test_pull_record_not_found(tmp_path, capsys):
    routes, a = _routes_pull(None)
    rc = cli.cmd_pull(_client(routes), str(tmp_path), "sys_script_include", a, as_json=False)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


from sndeck.records import set_workspace as _sw


def _stage_record(scratch, set_sys_id, set_name, table, sys_id, name,
                  script="x", snapshot="x"):
    ws = _sw(scratch, set_sys_id, set_name)
    folder = ws / table / f"{name}__{sys_id}"
    folder.mkdir(parents=True)
    folder.joinpath("record.json").write_text(json.dumps(
        {"_meta": {"table": table, "sys_id": sys_id, "name": name}, "script": script}))
    folder.joinpath(".snapshot.json").write_text(json.dumps({"script": snapshot}))
    folder.joinpath("script.js").write_text(script)
    return folder


def _routes_status(set_sys_id, table, sys_id, name):
    def routes(t, params):
        if t == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if t == "sys_user_preference":
            return [{"value": set_sys_id}]
        if t == "sys_update_set":
            return [{"sys_id": {"value": set_sys_id}, "name": {"value": name},
                     "state": {"value": "in progress", "display_value": "In progress"},
                     "application": {"display_value": "Global"}}]
        if t == "sys_update_xml":
            return [{"name": {"value": f"{table}_{sys_id}"},
                     "target_name": {"display_value": "Thing"},
                     "type": {"display_value": "Update"}}]
        return []
    return routes


def test_status_reports_dirty(tmp_path, capsys):
    sid = "1" * 32
    rec = "a" * 32
    _stage_record(str(tmp_path), sid, "My Set", "sys_script_include", rec, "Thing",
                  script="EDITED", snapshot="x")   # dirty: local != snapshot
    routes = _routes_status(sid, "sys_script_include", rec, "My Set")
    rc = cli.cmd_status(_client(routes), str(tmp_path), as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert obj["set"]["sys_id"] == sid
    states = {r["sys_id"]: r["state"] for r in obj["records"]}
    assert states[rec] == "dirty"


def test_status_no_current_set(tmp_path, capsys):
    def routes(t, params):
        if t == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        return []
    rc = cli.cmd_status(_client(routes), str(tmp_path), as_json=True)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) is None


def test_push_single_dirty(tmp_path, capsys, monkeypatch):
    sid = "1" * 32
    rec = "a" * 32
    _stage_record(str(tmp_path), sid, "My Set", "sys_script_include", rec, "Thing",
                  script="EDITED", snapshot="x")
    routes = _routes_status(sid, "sys_script_include", rec, "My Set")
    pushed = []
    monkeypatch.setattr("sndeck.push.build_push_plan",
                        lambda c, path: __import__("sndeck.sync", fromlist=["PushPlan"])
                        .PushPlan("sys_script_include", rec, "Thing", [], [], False))
    monkeypatch.setattr("sndeck.push.apply_push", lambda c, plan: pushed.append(plan.sys_id))
    monkeypatch.setattr("sndeck.push.pull_record", lambda *a, **k: None)
    monkeypatch.setattr("sndeck.push.set_scope_pointer", lambda *a, **k: None)
    monkeypatch.setattr("sndeck.push.set_current_application", lambda *a, **k: None)

    rc = cli.cmd_push(_client(routes), str(tmp_path), "sys_script_include", rec,
                      False, as_json=True)
    obj = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert pushed == [rec]
    assert obj[0]["pushed"] is True


def test_push_not_staged(tmp_path, capsys):
    sid = "1" * 32
    routes = _routes_status(sid, "sys_script_include", "a" * 32, "My Set")
    rc = cli.cmd_push(_client(routes), str(tmp_path), "sys_script_include", "z" * 32,
                      False, as_json=False)
    assert rc == 1
    assert "not staged" in capsys.readouterr().err


def test_push_all_clean(tmp_path, capsys):
    sid = "1" * 32
    rec = "a" * 32
    _stage_record(str(tmp_path), sid, "My Set", "sys_script_include", rec, "Thing",
                  script="x", snapshot="x")   # clean: local == snapshot
    routes = _routes_status(sid, "sys_script_include", rec, "My Set")
    rc = cli.cmd_push(_client(routes), str(tmp_path), None, None, True, as_json=False)
    assert rc == 0
    assert "nothing to push" in capsys.readouterr().out


def test_push_requires_target(tmp_path, capsys):
    sid = "1" * 32
    routes = _routes_status(sid, "sys_script_include", "a" * 32, "My Set")
    rc = cli.cmd_push(_client(routes), str(tmp_path), None, None, False, as_json=False)
    assert rc == 1
    assert "requires" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# dispatch() seam tests
# ---------------------------------------------------------------------------

def test_dispatch_us_get_routes(tmp_path, capsys):
    rc = cli.dispatch(["us", "get"], client_factory=lambda name: _client(_routes_current_set))
    assert rc == 0


def test_dispatch_us_ls_routes(tmp_path, capsys):
    rc = cli.dispatch(["us", "ls"], client_factory=lambda name: _client(_routes_two_sets))
    assert rc == 0


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
    orig = climod.set_current_update_set
    climod.set_current_update_set = lambda c, u, sid: None
    try:
        rc = cli.dispatch(["us", "set", "S1"],
                          client_factory=lambda name: _client(_routes_us_set))
    finally:
        climod.set_current_update_set = orig
    assert rc == 0


def test_dispatch_instance_flag_reaches_factory(capsys, monkeypatch):
    """--instance flag overrides resolution and is passed to client_factory."""
    import sndeck.cli as climod
    recorded = {}
    monkeypatch.setattr(climod, "set_current_update_set", lambda c, u, sid: None)

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
    assert {"us", "pull", "status", "push"} <= _SUBCOMMANDS


def test_main_subcommands_excludes_non_subcommands():
    from sndeck.__main__ import _SUBCOMMANDS
    assert "." not in _SUBCOMMANDS
    assert "--json" not in _SUBCOMMANDS
    assert "us" in _SUBCOMMANDS
