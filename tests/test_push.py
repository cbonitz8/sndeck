import json
import httpx
from sndeck.config import Instance
from sndeck.push import push_all, push_one
from sndeck.rest import TableClient
from sndeck.sync import PushPlan
from sndeck.updatesets import Capture
from sndeck.snapshot import is_dirty, read_meta_extra, write_meta_extra
from storekit import SET_A, make_copy, make_legacy

_INST = Instance("dev", "https://x.service-now.com", "cid",
                 "https://x.service-now.com/oauth_token.do", "cbonitz@x")

_P, _M, _S, _G = "P" * 32, "M" * 32, "S" * 32, "G" * 32


class _Tok:
    def access_token(self): return "AT"
    def invalidate(self): pass


def _client(routes):
    def handler(req):
        table = str(req.url.path).rsplit("/", 1)[-1]
        return httpx.Response(200, json={"result": routes(table, dict(req.url.params))})
    return TableClient(_INST, _Tok(), http=httpx.Client(transport=httpx.MockTransport(handler)))


def _set_row(sys_id, name, scope):
    return {"sys_id": {"value": sys_id}, "name": {"value": name, "display_value": name},
            "state": {"value": "in progress", "display_value": "In progress"},
            "application": {"value": scope, "display_value": scope}}


def _set_routes(sets, prefs):
    """sets: {sys_id: (name, raw scope)}; prefs: value every pref read returns."""
    def routes(table, params):
        q = params.get("sysparm_query", "")
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": prefs}] if prefs is not None else []
        if table == "sys_update_set":
            return [_set_row(sid, n, sc) for sid, (n, sc) in sets.items() if sid in q]
        return []
    return routes


def _copy(tmp_path, sys_id, name, target):
    return make_copy(tmp_path, "dev", "sys_script", name, sys_id,
                     target_set={"sys_id": target, "name": "t"}, dirty=True)


def _stub_plan(monkeypatch, by_path):
    monkeypatch.setattr("sndeck.push.build_push_plan",
                        lambda c, path: PushPlan("sys_script", *by_path[str(path)],
                                                 [], [], False))


def _cap(set_id, set_name):
    return Capture("t", "Business Rule", "A", set_name, set_id)


def test_push_all_routes_same_scope_members_to_own_sets(tmp_path, monkeypatch):
    """Two Global copies targeting different sets: the single scope pointer must be
    aimed at each copy's OWN target set right before its push."""
    a, b = "a" * 32, "b" * 32
    fa, fb = _copy(tmp_path, a, "A", _P), _copy(tmp_path, b, "B", _M)
    client = _client(_set_routes({_P: ("phase 1", "global"), _M: ("member", "global")},
                                 "global"))
    _stub_plan(monkeypatch, {str(fa): (a, "A"), str(fb): (b, "B")})
    monkeypatch.setattr("sndeck.push.apply_push", lambda c, plan: None)
    align_calls, pointer_calls = [], []
    monkeypatch.setattr("sndeck.push.set_current_application",
                        lambda c, u, scope: align_calls.append(scope))
    monkeypatch.setattr("sndeck.push.set_scope_pointer",
                        lambda c, u, scope, sid: pointer_calls.append((scope, sid)))
    owners = {a: _P, b: _M}
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap(owners[s], "owner"))

    outcomes = push_all(client, [fa, fb])

    assert [o.pushed for o in outcomes] == [True, True]
    assert pointer_calls == [("global", _P), ("global", _M)]
    assert align_calls == []
    assert all(o.capture_error is None for o in outcomes)


def test_push_one_reports_failure_reason(tmp_path, monkeypatch):
    a = "a" * 32
    fa = _copy(tmp_path, a, "A", _P)
    client = _client(_set_routes({_P: ("phase 1", "global")}, None))
    _stub_plan(monkeypatch, {str(fa): (a, "A")})
    monkeypatch.setattr("sndeck.push.set_scope_pointer", lambda *a, **k: None)
    monkeypatch.setattr("sndeck.push.set_current_application", lambda *a, **k: None)

    def boom(c, plan):
        raise RuntimeError("instance changed since pull")
    monkeypatch.setattr("sndeck.push.apply_push", boom)

    outcome = push_one(client, fa)
    assert outcome.pushed is False
    assert "instance changed" in outcome.reason
    assert outcome.name == "A"


def _scoped(tmp_path, monkeypatch, app_pref, events):
    """One copy targeting scoped set S (scope SCOPE9), with G a Global set."""
    a = "a" * 32
    fa = _copy(tmp_path, a, "A", _S)
    client = _client(_set_routes({_S: ("scoped set", "SCOPE9"),
                                  _G: ("global set", "global")}, app_pref))
    monkeypatch.setattr(client, "reset_session", lambda: events.append("reset"))
    _stub_plan(monkeypatch, {str(fa): (a, "A")})
    monkeypatch.setattr("sndeck.push.apply_push", lambda c, plan: events.append("put"))
    monkeypatch.setattr("sndeck.push.set_scope_pointer", lambda *a, **k: None)
    monkeypatch.setattr("sndeck.push.set_current_application",
                        lambda c, u, scope: events.append(f"app:{scope}"))
    return client, fa


def test_push_all_resets_server_session_after_changing_app(tmp_path, monkeypatch):
    """The open SN session keeps the scope it started in; a PUT in that session after
    PATCHing apps.current_app captures into the old scope's set."""
    events = []
    client, fa = _scoped(tmp_path, monkeypatch, "global", events)
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap(_S, "scoped set"))

    [o] = push_all(client, [fa])

    assert events == ["app:SCOPE9", "reset", "put"]
    assert o.pushed and o.routed_scope == "SCOPE9" and o.capture_error is None


def test_push_all_keeps_session_when_app_already_aligned(tmp_path, monkeypatch):
    events = []
    client, fa = _scoped(tmp_path, monkeypatch, "SCOPE9", events)
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap(_S, "scoped set"))

    [o] = push_all(client, [fa])

    assert events == ["put"]
    assert o.capture_error is None


def test_push_all_reports_miscapture_naming_both_sets(tmp_path, monkeypatch):
    client, fa = _scoped(tmp_path, monkeypatch, "SCOPE9", [])
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap(_G, "global set"))

    [o] = push_all(client, [fa])

    assert o.pushed is True
    assert "global set" in o.capture_error and _G in o.capture_error
    assert "scoped set" in o.capture_error and _S in o.capture_error


def test_push_all_reports_missing_capture(tmp_path, monkeypatch):
    client, fa = _scoped(tmp_path, monkeypatch, "SCOPE9", [])
    monkeypatch.setattr("sndeck.push.capture_for_record", lambda c, t, s: None)

    [o] = push_all(client, [fa])

    assert o.pushed is True
    assert "no sys_update_xml" in o.capture_error and "scoped set" in o.capture_error


def test_push_all_resets_session_when_only_the_scope_pointer_moves(tmp_path, monkeypatch):
    """App already aligned, but the per-scope pointer moved: the open session would
    still route by the pointer it read at open, so the PUT needs a fresh session."""
    events = []
    client, fa = _scoped(tmp_path, monkeypatch, "SCOPE9", events)
    monkeypatch.setattr("sndeck.push.set_scope_pointer",
                        lambda c, u, scope, sid: events.append("pointer") or True)
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap(_S, "scoped set"))

    [o] = push_all(client, [fa])

    assert events == ["pointer", "reset", "put"]
    assert o.routed_scope is None and o.capture_error is None


def test_push_all_resets_once_when_pointer_and_app_both_move(tmp_path, monkeypatch):
    events = []
    client, fa = _scoped(tmp_path, monkeypatch, "global", events)
    monkeypatch.setattr("sndeck.push.set_scope_pointer",
                        lambda c, u, scope, sid: events.append("pointer") or True)
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap(_S, "scoped set"))

    push_all(client, [fa])

    assert events == ["pointer", "app:SCOPE9", "reset", "put"]


def test_push_refuses_copy_without_target_set(tmp_path, monkeypatch):
    fa = make_copy(tmp_path, "dev", "sys_script", "A", "a" * 32, target_set=None, dirty=True)
    client = _client(_set_routes({}, None))
    _stub_plan(monkeypatch, {str(fa): ("a" * 32, "A")})
    puts = []
    monkeypatch.setattr("sndeck.push.apply_push", lambda c, plan: puts.append(plan))

    [o] = push_all(client, [fa])

    assert not o.pushed and "no target update set" in o.reason and puts == []


def test_push_refuses_target_set_missing_on_instance(tmp_path, monkeypatch):
    fa = _copy(tmp_path, "a" * 32, "A", _P)
    client = _client(_set_routes({}, None))
    _stub_plan(monkeypatch, {str(fa): ("a" * 32, "A")})
    puts = []
    monkeypatch.setattr("sndeck.push.apply_push", lambda c, plan: puts.append(plan))

    [o] = push_all(client, [fa])

    assert not o.pushed and "not found on dev" in o.reason and puts == []


def _routes(state="in progress", application="global"):
    def routes(table, params):
        if table == "sys_update_set":
            return [{"sys_id": SET_A["sys_id"], "name": "Set A", "state": state,
                     "application": application}]
        if table == "sys_script":
            return [{"sys_id": "111", "name": "A", "script": "gs.info('x');"}]
        return []
    return routes


def test_push_refuses_ref_copy(tmp_path, sn_client):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", kind="ref", dirty=True)
    [o] = push_all(sn_client(_routes()), [f])
    assert not o.pushed and "reference copies are never pushed" in o.reason


def test_push_refuses_shipped_target(tmp_path, sn_client):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", dirty=True)
    [o] = push_all(sn_client(_routes("complete")), [f])
    assert not o.pushed and "sndeck retarget" in o.reason


def test_push_puts_local_edit(tmp_path, sn_client):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", dirty=True)
    writes = []

    def write(method, table, sys_id, body):
        writes.append((method, table, sys_id, body))
        return {}
    [o] = push_all(sn_client(_routes(), write), [f])
    assert ("PUT", "sys_script", "111", {"script": "gs.info('x'); // EDITED"}) in writes


def test_push_refuses_target_set_without_application_scope(tmp_path, sn_client):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", dirty=True)
    writes = []

    def write(method, table, sys_id, body):
        writes.append((method, table, sys_id, body))
        return {}
    [o] = push_all(sn_client(_routes(application=""), write), [f])
    assert not o.pushed and "has no application scope" in o.reason
    assert writes == []


def test_push_refuses_legacy_copy_without_store_metadata(tmp_path, sn_client):
    f = make_legacy(tmp_path, "sys_script", "A", "111", dirty=True)
    [o] = push_all(sn_client(_routes()), [f])
    assert not o.pushed and "sndeck migrate" in o.reason


def test_push_refuses_unknown_copy_kind(tmp_path, sn_client):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", dirty=True)
    write_meta_extra(f, kind="weird")
    [o] = push_all(sn_client(_routes()), [f])
    assert not o.pushed and "unknown copy kind 'weird'" in o.reason


def test_push_refuses_malformed_target_set(tmp_path, sn_client):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", dirty=True,
                  target_set={"sys_id": SET_A["sys_id"]})
    writes = []

    def write(method, table, sys_id, body):
        writes.append((method, table, sys_id, body))
        return {}
    [o] = push_all(sn_client(_routes(), write), [f])
    assert not o.pushed and "malformed target_set" in o.reason and writes == []


def _widget_copy(root, sid):
    """sp_widget edit copy: `script` edited locally, `client_script` clean at c0."""
    folder = make_copy(root, "dev", "sp_widget", "W", sid, script="s0")
    snap = {"sys_id": sid, "name": "W", "script": "s0", "client_script": "c0"}
    (folder / ".snapshot.json").write_text(json.dumps(snap))
    (folder / "script.js").write_text("s1")
    (folder / "client_script.js").write_text("c0")
    return folder


def test_push_rebaselines_multi_field_copy_whose_other_field_drifted(tmp_path, sn_client):
    """client_script drifted on the instance, script edited locally: the PUT carries
    script only, and the copy must come out clean with the live client_script."""
    sid = "w" * 32
    f = _widget_copy(tmp_path, sid)
    live = {"sys_id": sid, "name": "W", "script": "s0", "client_script": "c1"}

    def routes(table, params):
        if table == "sys_update_set":
            return _routes()(table, params)
        if table == "sp_widget":
            return [dict(live)]
        return []

    def write(method, table, sys_id, body):
        live.update(body)
        return {}
    [o] = push_all(sn_client(routes, write), [f])

    assert o.pushed and o.warning is None
    assert not is_dirty(f)
    assert (f / "client_script.js").read_text() == "c1"
    assert (f / "script.js").read_text() == "s1"
    extra = read_meta_extra(f)
    assert extra["instance"] == "dev" and extra["kind"] == "edit"
    assert extra["target_set"] == SET_A


def test_push_failed_rebaseline_is_still_a_push_with_a_warning(tmp_path, sn_client):
    f = make_copy(tmp_path, "dev", "sys_script", "A", "111", dirty=True)
    put_done = []

    def routes(table, params):
        if table == "sys_script" and put_done:
            raise RuntimeError("instance unreachable")
        return _routes()(table, params)

    def write(method, table, sys_id, body):
        put_done.append(body)
        return {}
    [o] = push_all(sn_client(routes, write), [f])

    assert put_done == [{"script": "gs.info('x'); // EDITED"}]
    assert o.pushed is True and o.reason is None
    assert "pushed, but could not re-baseline the local copy" in o.warning
    assert o.capture_error is not None and "no sys_update_xml capture" in o.capture_error


def test_push_refuses_copy_from_another_instance(tmp_path, sn_client):
    f = make_copy(tmp_path, "prod", "sys_script", "A", "111", dirty=True)
    writes = []

    def write(method, table, sys_id, body):
        writes.append((method, table, sys_id, body))
        return {}
    [o] = push_all(sn_client(_routes(), write), [f])
    assert not o.pushed and o.reason == "copy belongs to instance 'prod', not 'dev'"
    assert writes == []
