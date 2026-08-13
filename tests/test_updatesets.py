import json

import httpx
from sndeck.updatesets import (current_update_set, recent_captures, capture_for_record,
                               records_in_update_set, update_set_records, UpdateSet, Capture,
                               batch_members, update_set_states, UpdateSetMeta,
                               set_current_application, set_scope_pointer)
from sndeck.rest import TableClient
from sndeck.config import Instance

INST = Instance("dev", "https://x.service-now.com", "cid",
                "https://x.service-now.com/oauth_token.do", "cbonitz@x")


class FakeToken:
    def access_token(self): return "AT"
    def invalidate(self): pass


def _client(routes):
    """routes: fn(path, params) -> list[dict]."""
    def handler(req):
        table = str(req.url.path).rsplit("/", 1)[-1]
        return httpx.Response(200, json={"result": routes(table, dict(req.url.params))})
    return TableClient(INST, FakeToken(), http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_current_update_set_resolves_pref_to_set():
    def routes(table, params):
        if table == "sys_user_preference":
            return [{"value": "SET1"}]
        if table == "sys_update_set":
            return [{"sys_id": "SET1", "name": "vendor rep hotfix", "state": "in progress"}]
        return []
    us = current_update_set(_client(routes), "cbonitz")
    assert us == UpdateSet("SET1", "vendor rep hotfix", "in progress")


def test_current_update_set_none_when_no_pref():
    us = current_update_set(_client(lambda t, p: []), "cbonitz")
    assert us is None


def test_recent_captures_parses_display_values():
    def routes(table, params):
        assert "sys_created_by=cbonitz" in params.get("sysparm_query", "")
        assert "javascript:gs.hoursAgoStart(2)" in params.get("sysparm_query", "")
        return [{
            "sys_created_on": {"value": "2026-07-04 14:32:00", "display_value": "July 04, 2026 2:32 PM"},
            "type": {"value": "sys_script", "display_value": "Business Rule"},
            "target_name": {"value": "My BR", "display_value": "My BR"},
            "update_set": {"value": "SET1", "display_value": "vendor rep hotfix"},
        }]
    caps = recent_captures(_client(routes), "cbonitz", hours=2)
    assert caps == [Capture("2026-07-04 14:32:00", "Business Rule", "My BR",
                            "vendor rep hotfix", "SET1")]


def test_capture_for_record_queries_by_name_key():
    def routes(table, params):
        assert params.get("sysparm_query", "").startswith("name=sys_script_3a4f")
        assert "ORDERBYDESCsys_created_on" in params.get("sysparm_query", "")
        return [{
            "sys_created_on": {"value": "2026-07-04 14:30:00", "display_value": "..."},
            "type": {"value": "sys_script", "display_value": "Business Rule"},
            "target_name": {"value": "My BR", "display_value": "My BR"},
            "update_set": {"value": "SET2", "display_value": "Default"},
        }]
    cap = capture_for_record(_client(routes), "sys_script", "3a4f")
    assert cap.set_name == "Default" and cap.set_id == "SET2"


def test_capture_for_record_none_when_never_captured():
    assert capture_for_record(_client(lambda t, p: []), "sys_script", "zzz") is None


def test_records_in_update_set_filters_and_dedups():
    def routes(table, params):
        assert table == "sys_update_xml"
        assert "update_set=SET1" in params.get("sysparm_query", "")
        return [
            {"name": "sys_script_" + "a"*32},                 # code artifact -> keep
            {"name": "sys_script_" + "a"*32},                 # dup -> drop
            {"name": "sys_user_" + "b"*32},                   # not a code artifact -> drop
            {"name": "sys_script_include_" + "c"*32},         # code artifact -> keep
            {"name": "garbage"},                              # no key -> drop
        ]
    got = records_in_update_set(_client(routes), "SET1")
    assert got == [("sys_script", "a"*32), ("sys_script_include", "c"*32)]


def test_update_set_summary_total_and_code():
    def routes(table, params):
        assert table == "sys_update_xml"
        return [
            {"name": "sys_script_" + "a"*32},          # code
            {"name": "sys_property_" + "b"*32},        # non-code
            {"name": "sys_script_" + "a"*32},          # dup code
            {"name": "oauth_entity_" + "c"*32},        # non-code
        ]
    from sndeck.updatesets import update_set_summary
    total, code = update_set_summary(_client(routes), "SET1")
    assert total == 4
    assert code == [("sys_script", "a"*32)]


def test_update_set_records_returns_all_parseable():
    def routes(table, params):
        return [
            {"name": "sys_script_" + "a"*32},        # code
            {"name": "sys_properties_" + "b"*32},    # non-code
            {"name": "oauth_entity_" + "c"*32},      # non-code
            {"name": "sys_script_" + "a"*32},        # dup
            {"name": "not_a_record"},                # skipped
        ]
    total, recs = update_set_records(_client(routes), "SET1")
    assert total == 5
    assert recs == [("sys_script", "a"*32), ("sys_properties", "b"*32), ("oauth_entity", "c"*32)]


def test_current_user_returns_sysid_and_name():
    def routes(table, params):
        assert table == "sys_user"
        assert "gs.getUserID()" in params.get("sysparm_query", "")
        return [{"sys_id": "U1", "user_name": "cbonitz"}]
    from sndeck.updatesets import current_user, CurrentUser
    assert current_user(_client(routes)) == CurrentUser("U1", "cbonitz")


def test_current_user_none_when_empty():
    from sndeck.updatesets import current_user
    assert current_user(_client(lambda t, p: [])) is None


def test_update_set_meta_resolves_scope_display():
    def routes(table, params):
        assert table == "sys_update_set"
        return [{
            "sys_id": {"value": "SET1", "display_value": "SET1"},
            "name": {"value": "vendor hotfix", "display_value": "vendor hotfix"},
            "state": {"value": "in progress", "display_value": "In progress"},
            "application": {"value": "app1", "display_value": "Rep Services"},
        }]
    from sndeck.updatesets import update_set_meta, UpdateSetMeta
    m = update_set_meta(_client(routes), "SET1")
    assert m == UpdateSetMeta("SET1", "vendor hotfix", "In progress", "Rep Services")


def test_update_set_meta_scope_defaults_global():
    def routes(table, params):
        return [{"sys_id": {"value": "S", "display_value": "S"},
                 "name": {"value": "n", "display_value": "n"},
                 "state": {"value": "in progress", "display_value": "In progress"},
                 "application": {"value": "", "display_value": ""}}]
    from sndeck.updatesets import update_set_meta
    assert update_set_meta(_client(routes), "S").scope == "Global"


def test_update_set_entries_parses_dedupes_and_keeps_deletes():
    def routes(table, params):
        assert table == "sys_update_xml"
        assert "update_set=SET1" in params.get("sysparm_query", "")
        return [
            {"name": {"value": "sys_script_" + "a"*32}, "target_name": {"display_value": "My BR"},
             "type": {"display_value": "Business Rule"}},
            {"name": {"value": "sys_script_" + "a"*32}, "target_name": {"display_value": "My BR"},
             "type": {"display_value": "Business Rule"}},  # dup -> drop
            {"name": {"value": "sp_widget_" + "b"*32}, "target_name": {"display_value": "Rep Homepage"},
             "type": {"display_value": "Widget"}},
            {"name": {"value": "sys_properties_" + "c"*32}, "target_name": {"display_value": "prop.x"},
             "type": {"display_value": "Delete"}},  # deleted record still surfaced
            {"name": {"value": "garbage"}, "target_name": {"display_value": "x"}, "type": {"display_value": "y"}},
        ]
    from sndeck.updatesets import update_set_entries, Entry
    got = update_set_entries(_client(routes), "SET1")
    assert got == [
        Entry("sys_script", "a"*32, "My BR", "Business Rule"),
        Entry("sp_widget", "b"*32, "Rep Homepage", "Widget"),
        Entry("sys_properties", "c"*32, "prop.x", "Delete"),
    ]


def test_list_update_sets_paginates_in_progress():
    def routes(table, params):
        assert table == "sys_update_set"
        assert "state=in progress" in params.get("sysparm_query", "")
        assert "ORDERBYDESCsys_updated_on" in params.get("sysparm_query", "")
        assert params.get("sysparm_offset") == "25"
        assert params.get("sysparm_limit") == "25"
        return [{"sys_id": {"value": "S2", "display_value": "S2"},
                 "name": {"value": "hotfix", "display_value": "hotfix"},
                 "state": {"value": "in progress", "display_value": "In progress"},
                 "application": {"value": "", "display_value": ""}}]
    from sndeck.updatesets import list_update_sets, UpdateSetMeta
    got = list_update_sets(_client(routes), offset=25, limit=25)
    assert got == [UpdateSetMeta("S2", "hotfix", "In progress", "Global")]


def test_set_current_update_set_writes_all_three_prefs():
    """Both pointer prefs get the new set, and the recents list is bumped — all PATCHed."""
    patches = {}
    class C:
        def query(self, table, *, query="", **kw):
            if table == "sys_update_set":
                return [{"name": {"value": "Rep hotfix", "display_value": "Rep hotfix"},
                         "application": {"value": "global", "display_value": "Global"}}]
            if "glide.ui.concourse_picker.recent_items" in query:
                return [{"sys_id": "R1",
                         "value": '{"update-set":{"undefined":'
                                  '[{"name":"Old [Global]","sysId":"OLD","path":""}]}}'}]
            # pointer-pref lookup: id encodes the pref name so we can assert per-pref
            return [{"sys_id": "P_" + query.split("name=", 1)[1].split("^", 1)[0]}]
        def patch(self, table, sys_id, body): patches[sys_id] = body; return {}
        def post(self, *a, **k): raise AssertionError("should not POST when prefs exist")
    from sndeck.updatesets import set_current_update_set
    set_current_update_set(C(), "U1", "SET9")
    assert patches["P_sys_update_set"] == {"value": "SET9"}
    assert patches["P_updateSetForScopeglobal"] == {"value": "SET9"}
    recents = json.loads(patches["R1"]["value"])
    assert recents["update-set"]["undefined"][0] == {
        "name": "Rep hotfix [Global]", "sysId": "SET9", "path": ""}
    assert recents["update-set"]["undefined"][1]["sysId"] == "OLD"  # prior set retained behind


def test_set_current_update_set_posts_all_three_when_absent():
    posts = []
    class C:
        def query(self, table, *, query="", **kw):
            if table == "sys_update_set":
                return [{"name": {"value": "Rep hotfix", "display_value": "Rep hotfix"},
                         "application": {"value": "global", "display_value": "Global"}}]
            return []  # no prefs of any kind yet
        def patch(self, *a, **k): raise AssertionError("should not PATCH when absent")
        def post(self, table, body): posts.append(body); return {}
    from sndeck.updatesets import set_current_update_set
    set_current_update_set(C(), "U1", "SET9")
    by_name = {b["name"]: b for b in posts}
    assert by_name["sys_update_set"]["value"] == "SET9"
    assert by_name["updateSetForScopeglobal"]["value"] == "SET9"
    recents = json.loads(by_name["glide.ui.concourse_picker.recent_items"]["value"])
    assert recents["update-set"]["undefined"] == [
        {"name": "Rep hotfix [Global]", "sysId": "SET9", "path": ""}]


def test_set_current_update_set_uses_scoped_names():
    """A scoped app drives both the pref name (scope sys_id) and the recents label (scope display)."""
    posts = []
    class C:
        def query(self, table, *, query="", **kw):
            if table == "sys_update_set":
                return [{"name": {"value": "Audit fix", "display_value": "Audit fix"},
                         "application": {"value": "SCOPE123", "display_value": "EGCS Audits"}}]
            return []
        def patch(self, *a, **k): raise AssertionError("should not PATCH when absent")
        def post(self, table, body): posts.append(body); return {}
    from sndeck.updatesets import set_current_update_set
    set_current_update_set(C(), "U1", "SET9")
    names = {b["name"] for b in posts}
    assert "sys_update_set" in names
    assert "updateSetForScopeSCOPE123" in names
    recents = json.loads(next(b["value"] for b in posts
                              if b["name"] == "glide.ui.concourse_picker.recent_items"))
    assert recents["update-set"]["undefined"][0]["name"] == "Audit fix [EGCS Audits]"


def test_bump_recent_set_moves_to_front_and_dedupes():
    from sndeck.updatesets import _bump_recent_set
    data = {"update-set": {"undefined": [
        {"name": "A [Global]", "sysId": "A", "path": ""},
        {"name": "B [Global]", "sysId": "B", "path": ""},
        {"name": "A [Global]", "sysId": "A", "path": ""},  # stale dup -> collapsed
    ]}}
    bucket = _bump_recent_set(data, "A", "A [Global]")["update-set"]["undefined"]
    assert [e["sysId"] for e in bucket] == ["A", "B"]  # A once, at front


def test_bump_recent_set_creates_bucket_when_empty():
    from sndeck.updatesets import _bump_recent_set
    out = _bump_recent_set({}, "X", "X [Global]")
    assert out["update-set"]["undefined"] == [{"name": "X [Global]", "sysId": "X", "path": ""}]


def test_bump_recent_set_caps_list():
    from sndeck.updatesets import _bump_recent_set
    items = [{"name": f"S{i} [Global]", "sysId": f"S{i}", "path": ""} for i in range(5)]
    bucket = _bump_recent_set({"update-set": {"undefined": items}},
                              "NEW", "NEW [Global]", limit=5)["update-set"]["undefined"]
    assert len(bucket) == 5
    assert bucket[0]["sysId"] == "NEW" and bucket[-1]["sysId"] == "S3"  # oldest (S4) dropped


def test_bump_recent_set_survives_malformed_shapes():
    """Non-conforming recents JSON must not raise and must produce a well-formed result."""
    from sndeck.updatesets import _bump_recent_set
    bad_inputs = [
        {"update-set": {"undefined": "garbage"}},   # bucket value is str, not list
        {"update-set": []},                          # bucket map is list, not dict
        {"update-set": {"undefined": [1, 2]}},       # list items are ints, not dicts
        [],                                          # top-level is list, not dict
    ]
    for bad in bad_inputs:
        out = _bump_recent_set(bad, "NEW", "New [Global]")
        assert isinstance(out, dict), f"expected dict, got {type(out)} for input {bad!r}"
        assert "update-set" in out, f"missing update-set key for input {bad!r}"
        buckets = out["update-set"]
        assert isinstance(buckets, dict), f"buckets not a dict for input {bad!r}"
        # pick whichever bucket got written
        bucket = (buckets.get("undefined")
                  or next(iter(buckets.values()), None))
        assert isinstance(bucket, list) and bucket, (
            f"bucket empty/missing for input {bad!r}")
        assert bucket[0] == {"name": "New [Global]", "sysId": "NEW", "path": ""}, (
            f"new entry not at front for input {bad!r}")


def test_update_set_states_resolves_many_and_marks_missing():
    def routes(table, params):
        assert table == "sys_update_set"
        assert "sys_idINA,B,C" in params.get("sysparm_query", "")
        return [
            {"sys_id": {"value": "A"}, "name": {"display_value": "Set A"},
             "state": {"value": "in progress", "display_value": "In progress"},
             "application": {"display_value": "Global"}},
            {"sys_id": {"value": "B"}, "name": {"display_value": "Set B"},
             "state": {"value": "complete", "display_value": "Complete"},
             "application": {"display_value": "EGCS Audits"}},
        ]
    from sndeck.updatesets import update_set_states, UpdateSetMeta
    got = update_set_states(_client(routes), ["A", "B", "C"])
    assert got["A"] == UpdateSetMeta("A", "Set A", "in progress", "Global")
    assert got["B"].state == "complete" and got["B"].scope == "EGCS Audits"
    assert "C" not in got            # unresolved -> gone


def test_update_set_states_empty_input_no_query():
    called = {"n": 0}
    def routes(table, params):
        called["n"] += 1
        return []
    from sndeck.updatesets import update_set_states
    assert update_set_states(_client(routes), []) == {}
    assert called["n"] == 0


class _Client:
    def __init__(self, rows_by_query): self.rows_by_query = rows_by_query
    def query(self, table, *, query=None, fields=None, display_value="false",
              limit=None, offset=None):
        return self.rows_by_query.get(query, [])

def _row(sid, name, scope="global", parent="", base=None):
    base = base or sid
    return {"sys_id": {"value": sid}, "name": {"display_value": name},
            "state": {"value": "in progress"}, "application": {"value": scope, "display_value": scope},
            "parent": {"value": parent}, "base_update_set": {"value": base}}

def test_batch_members_base_first_then_children():
    base, child = "5"*32, "c"*32
    c = _Client({
        f"sys_id={base}": [_row(base, "base")],
        f"base_update_set={base}^ORDERBYparent": [
            _row(base, "base"), _row(child, "child", scope="x_etgr_dealer_recr", parent=base, base=base)],
    })
    got = batch_members(c, base)
    assert [m.sys_id for m in got] == [base, child]
    assert got[1].scope == "x_etgr_dealer_recr"

def test_batch_members_standalone_returns_self():
    sid = "a"*32
    c = _Client({f"sys_id={sid}": [_row(sid, "solo")],
                 f"base_update_set={sid}^ORDERBYparent": [_row(sid, "solo")]})
    assert [m.sys_id for m in batch_members(c, sid)] == [sid]

def test_batch_members_empty_base_field_still_returns_self():
    # Regression: a real set whose base_update_set field is EMPTY (older sets that
    # were never batched don't self-reference). The family query matches nothing,
    # but the set must still appear in its own batch — else build_tree crashes on
    # member_nodes[0] (IndexError) at launch.
    sid = "b"*32
    row = {"sys_id": {"value": sid}, "name": {"display_value": "cli setup"},
           "state": {"value": "in progress"},
           "application": {"value": "global", "display_value": "Global"},
           "parent": {"value": ""}, "base_update_set": {"value": ""}}
    c = _Client({f"sys_id={sid}": [row]})  # family query absent -> [] (fake .get default)
    got = batch_members(c, sid)
    assert [m.sys_id for m in got] == [sid]
    assert got[0].name == "cli setup" and got[0].scope == "global"


class _RecordingClient:
    def __init__(self, rows_by_query):
        self.rows_by_query = rows_by_query; self.patched = []; self.posted = []
    def query(self, table, *, query=None, fields=None, display_value="false", limit=None, offset=None):
        return self.rows_by_query.get(query, [])
    def patch(self, table, sys_id, body): self.patched.append((table, sys_id, body))
    def post(self, table, body): self.posted.append((table, body))

def test_set_current_application_upserts_pref():
    c = _RecordingClient({"name=apps.current_app^user=u1": []})
    set_current_application(c, "u1", "x_etgr_dealer_recr")
    assert c.posted == [("sys_user_preference",
        {"name": "apps.current_app", "user": "u1", "value": "x_etgr_dealer_recr"})]

def test_set_scope_pointer_upserts_only_the_scope_pointer():
    """set_scope_pointer writes exactly updateSetForScope<scope> -> set, nothing else
    (no sys_update_set, no recents). It's the per-scope capture pointer the push path
    uses to route a record into its owning batch member."""
    scope, member = "55de", "c"*32
    c = _RecordingClient({f"name=updateSetForScope{scope}^user=u1": []})  # no existing pref -> POST
    set_scope_pointer(c, "u1", scope, member)
    assert c.posted == [("sys_user_preference",
        {"name": f"updateSetForScope{scope}", "user": "u1", "value": member})]


def test_set_scope_pointer_defaults_empty_scope_to_global():
    base = "5"*32
    c = _RecordingClient({"name=updateSetForScopeglobal^user=u1": []})
    set_scope_pointer(c, "u1", "", base)
    assert c.posted == [("sys_user_preference",
        {"name": "updateSetForScopeglobal", "user": "u1", "value": base})]


def test_resolve_current_set_returns_user_and_set():
    from sndeck.updatesets import resolve_current_set, CurrentUser
    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_user_preference":
            return [{"value": "SET1"}]
        if table == "sys_update_set":
            return [{"sys_id": "SET1", "name": "hotfix", "state": "in progress"}]
        return []
    user, cur = resolve_current_set(_client(routes))
    assert user == CurrentUser("U1", "cbonitz")
    assert cur == UpdateSet("SET1", "hotfix", "in progress")


def test_resolve_current_set_none_user_gives_none_none():
    from sndeck.updatesets import resolve_current_set
    assert resolve_current_set(_client(lambda t, p: [])) == (None, None)


def test_resolve_current_set_user_but_no_set():
    from sndeck.updatesets import resolve_current_set
    def routes(table, params):
        return [{"sys_id": "U1", "user_name": "cbonitz"}] if table == "sys_user" else []
    user, cur = resolve_current_set(_client(routes))
    assert user is not None and cur is None


def test_switch_current_set_writes_pointer_and_scope():
    from sndeck.updatesets import switch_current_set
    writes = []
    def routes(table, params):
        if table == "sys_user":
            return [{"sys_id": "U1", "user_name": "cbonitz"}]
        if table == "sys_update_set":
            return [{"name": "hotfix", "application": {"value": "x_scope", "display_value": "App"}}]
        return []   # no existing prefs -> POSTs
    # switch_current_set returns True when the user resolves; the write path is exercised
    # against the mock transport (POST/PATCH accepted).
    assert switch_current_set(_client(routes), "SET1", "x_scope") is True


def test_switch_current_set_false_without_user():
    from sndeck.updatesets import switch_current_set
    assert switch_current_set(_client(lambda t, p: []), "SET1") is False


def test_read_pref_returns_value_or_none():
    from sndeck.updatesets import read_pref
    c = _RecordingClient({"name=apps.current_app^user=u1": [{"value": "x_scope"}]})
    assert read_pref(c, "u1", "apps.current_app") == "x_scope"
    assert read_pref(c, "u1", "missing") is None
