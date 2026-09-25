from pathlib import Path
from sndeck.tree import FileNode, TableNode, SetNode, ScopeNode, TreeModel
from sndeck.push import set_for_record, scope_for_record


def _model_two_global_members():
    a, b = "a" * 32, "b" * 32
    fa = FileNode("sys_script", a, "A", in_current_set=True, tracked=True,
                  local=True, dirty=True, record_path=Path("/tmp/a"))
    fb = FileNode("sys_script", b, "B", in_current_set=False, tracked=True,
                  local=True, dirty=True, record_path=Path("/tmp/b"))
    member = SetNode(sys_id="M" * 32, name="form inbox refresh", state="in progress",
                     is_current=False, tables=[TableNode("sys_script", "Business Rules", [fb])],
                     scope="global", is_base=False, members=[])
    base = SetNode(sys_id="P" * 32, name="phase 1 scaffold", state="in progress",
                   is_current=True, tables=[TableNode("sys_script", "Business Rules", [fa])],
                   scope="global", is_base=True, members=[member])
    return TreeModel([ScopeNode("Global", [base])], current_set=None), a, b


def test_set_for_record_returns_owning_set():
    model, a, b = _model_two_global_members()
    assert set_for_record(model, "sys_script", a) == ("global", "P" * 32)
    assert set_for_record(model, "sys_script", b) == ("global", "M" * 32)


def test_scope_for_record_finds_nested_member():
    a = "a" * 32
    child_scope = "childscope" + "x" * 22
    f = FileNode("sys_script", a, "BR", in_current_set=False, tracked=True,
                 local=True, dirty=True, record_path=Path("/tmp/x"))
    child = SetNode(sys_id="C" * 32, name="child", state="in progress", is_current=False,
                    tables=[TableNode("sys_script", "Business Rules", [f])],
                    scope=child_scope, is_base=False, members=[])
    base = SetNode(sys_id="B" * 32, name="base", state="in progress", is_current=True,
                   tables=[], scope="global", is_base=True, members=[child])
    model = TreeModel([ScopeNode("G", [base])], current_set=None)
    assert scope_for_record(model, "sys_script", a) == child_scope


def test_set_for_record_missing_returns_none():
    model, _, _ = _model_two_global_members()
    assert set_for_record(model, "sys_script", "z" * 32) is None
    assert set_for_record(None, "sys_script", "a" * 32) is None


import httpx
from sndeck.push import push_all, push_one, PushOutcome
from sndeck.rest import TableClient
from sndeck.config import Instance
from sndeck.sync import PushPlan

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


def test_push_all_routes_same_scope_members_to_own_sets(monkeypatch):
    """Two Global members: the single scope pointer must be aimed at each record's
    OWN set right before its push."""
    model, a, b = _model_two_global_members()

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": "global"}]   # already aligned to global
        return []

    client = _client(routes)
    monkeypatch.setattr("sndeck.push.build_push_plan",
                        lambda c, path: PushPlan("sys_script", a if "a" in str(path) else b,
                                                 "A" if "a" in str(path) else "B", [], [], False))
    monkeypatch.setattr("sndeck.push.apply_push", lambda c, plan: None)
    monkeypatch.setattr("sndeck.push.pull_record", lambda *a, **k: None)
    align_calls, pointer_calls = [], []
    monkeypatch.setattr("sndeck.push.set_current_application",
                        lambda c, u, scope: align_calls.append(scope))
    monkeypatch.setattr("sndeck.push.set_scope_pointer",
                        lambda c, u, scope, sid: pointer_calls.append((scope, sid)))
    owners = {a: "P" * 32, b: "M" * 32}
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap(owners[s], "owner"))

    outcomes = push_all(client, model, ["/tmp/a", "/tmp/b"])

    assert [o.pushed for o in outcomes] == [True, True]
    assert pointer_calls == [("global", "P" * 32), ("global", "M" * 32)]
    assert align_calls == []   # already aligned to global


def test_push_one_reports_failure_reason(monkeypatch):
    model, a, b = _model_two_global_members()

    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        return []

    client = _client(routes)
    monkeypatch.setattr("sndeck.push.build_push_plan",
                        lambda c, path: PushPlan("sys_script", a, "A", [], [], False))
    monkeypatch.setattr("sndeck.push.set_scope_pointer", lambda *a, **k: None)
    monkeypatch.setattr("sndeck.push.set_current_application", lambda *a, **k: None)

    def boom(c, plan):
        raise RuntimeError("instance changed since pull")
    monkeypatch.setattr("sndeck.push.apply_push", boom)

    outcome = push_one(client, model, "/tmp/a")
    assert outcome.pushed is False
    assert "instance changed" in outcome.reason
    assert outcome.name == "A"


def _scoped_model():
    a = "a" * 32
    f = FileNode("sys_script", a, "A", in_current_set=True, tracked=True,
                 local=True, dirty=True, record_path=Path("/tmp/a"))
    s = SetNode(sys_id="S" * 32, name="scoped set", state="in progress", is_current=True,
                tables=[TableNode("sys_script", "Business Rules", [f])],
                scope="SCOPE9", is_base=True, members=[])
    return TreeModel([ScopeNode("App", [s])], current_set=None), a


def _stub_push(monkeypatch, a, events):
    monkeypatch.setattr("sndeck.push.build_push_plan",
                        lambda c, path: PushPlan("sys_script", a, "A", [], [], False))
    monkeypatch.setattr("sndeck.push.apply_push", lambda c, plan: events.append("put"))
    monkeypatch.setattr("sndeck.push.pull_record", lambda *a, **k: None)
    monkeypatch.setattr("sndeck.push.set_scope_pointer", lambda *a, **k: None)
    monkeypatch.setattr("sndeck.push.set_current_application",
                        lambda c, u, scope: events.append(f"app:{scope}"))


def _cap(set_id, set_name):
    from sndeck.updatesets import Capture
    return Capture("t", "Business Rule", "A", set_name, set_id)


def _user_routes(app_pref):
    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": app_pref}]
        if table == "sys_update_set":
            return [{"sys_id": {"value": "G" * 32}, "name": {"value": "global set",
                     "display_value": "global set"}, "state": {"value": "in progress"},
                     "application": {"value": "global", "display_value": "Global"}}]
        return []
    return routes


def test_push_all_resets_server_session_after_changing_app(monkeypatch):
    """The open SN session keeps the scope it started in; a PUT in that session after
    PATCHing apps.current_app captures into the old scope's set."""
    model, a = _scoped_model()
    events = []
    client = _client(_user_routes("global"))
    monkeypatch.setattr(client, "reset_session", lambda: events.append("reset"))
    _stub_push(monkeypatch, a, events)
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap("S" * 32, "scoped set"))

    [o] = push_all(client, model, ["/tmp/a"])

    assert events == ["app:SCOPE9", "reset", "put"]
    assert o.pushed and o.routed_scope == "SCOPE9" and o.capture_error is None


def test_push_all_keeps_session_when_app_already_aligned(monkeypatch):
    model, a = _scoped_model()
    events = []
    client = _client(_user_routes("SCOPE9"))
    monkeypatch.setattr(client, "reset_session", lambda: events.append("reset"))
    _stub_push(monkeypatch, a, events)
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap("S" * 32, "scoped set"))

    [o] = push_all(client, model, ["/tmp/a"])

    assert events == ["put"]
    assert o.capture_error is None


def test_push_all_reports_miscapture_naming_both_sets(monkeypatch):
    model, a = _scoped_model()
    client = _client(_user_routes("SCOPE9"))
    _stub_push(monkeypatch, a, [])
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap("G" * 32, "global set"))
    monkeypatch.setattr("sndeck.push.update_set_meta",
                        lambda c, sid: type("M", (), {"name": "scoped set"})())

    [o] = push_all(client, model, ["/tmp/a"])

    assert o.pushed is True
    assert "global set" in o.capture_error and "G" * 32 in o.capture_error
    assert "scoped set" in o.capture_error and "S" * 32 in o.capture_error


def test_push_all_reports_missing_capture(monkeypatch):
    model, a = _scoped_model()
    client = _client(_user_routes("SCOPE9"))
    _stub_push(monkeypatch, a, [])
    monkeypatch.setattr("sndeck.push.capture_for_record", lambda c, t, s: None)
    monkeypatch.setattr("sndeck.push.update_set_meta",
                        lambda c, sid: type("M", (), {"name": "scoped set"})())

    [o] = push_all(client, model, ["/tmp/a"])

    assert o.pushed is True
    assert "no sys_update_xml" in o.capture_error and "scoped set" in o.capture_error


def test_push_all_unstaged_record_verifies_against_current_set(monkeypatch):
    """No owning set in the model: the intended set is the sys_update_set pref."""
    model = TreeModel([], current_set=None)
    a = "a" * 32
    client = _client(_user_routes("CUR" + "x" * 29))
    _stub_push(monkeypatch, a, [])
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap("CUR" + "x" * 29, "current"))

    [o] = push_all(client, model, ["/tmp/a"])

    assert o.pushed is True and o.capture_error is None


def test_push_all_resets_session_when_only_the_scope_pointer_moves(monkeypatch):
    """App already aligned, but the per-scope pointer moved: the open session would
    still route by the pointer it read at open, so the PUT needs a fresh session."""
    model, a = _scoped_model()
    events = []
    client = _client(_user_routes("SCOPE9"))
    monkeypatch.setattr(client, "reset_session", lambda: events.append("reset"))
    _stub_push(monkeypatch, a, events)
    monkeypatch.setattr("sndeck.push.set_scope_pointer",
                        lambda c, u, scope, sid: events.append("pointer") or True)
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap("S" * 32, "scoped set"))

    [o] = push_all(client, model, ["/tmp/a"])

    assert events == ["pointer", "reset", "put"]
    assert o.routed_scope is None and o.capture_error is None


def test_push_all_resets_once_when_pointer_and_app_both_move(monkeypatch):
    model, a = _scoped_model()
    events = []
    client = _client(_user_routes("global"))
    monkeypatch.setattr(client, "reset_session", lambda: events.append("reset"))
    _stub_push(monkeypatch, a, events)
    monkeypatch.setattr("sndeck.push.set_scope_pointer",
                        lambda c, u, scope, sid: events.append("pointer") or True)
    monkeypatch.setattr("sndeck.push.capture_for_record",
                        lambda c, t, s: _cap("S" * 32, "scoped set"))

    push_all(client, model, ["/tmp/a"])

    assert events == ["pointer", "app:SCOPE9", "reset", "put"]
