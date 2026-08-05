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
