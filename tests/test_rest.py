import httpx
import pytest
from sndeck.rest import TableClient
from sndeck.auth import AuthExpiredError
from sndeck.config import Instance

INST = Instance("dev", "https://x.service-now.com", "cid",
                "https://x.service-now.com/oauth_token.do", "cbonitz@x")


class FakeToken:
    def __init__(self, tokens): self.tokens = list(tokens); self.calls = 0; self.invalidate_calls = 0
    def access_token(self):
        self.calls += 1
        return self.tokens[min(self.calls - 1, len(self.tokens) - 1)]
    def invalidate(self):
        self.invalidate_calls += 1


def test_query_sends_bearer_and_returns_result():
    seen = {}
    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"result": [{"sys_id": "1"}, {"sys_id": "2"}]})
    c = TableClient(INST, FakeToken(["AT1"]), http=httpx.Client(transport=httpx.MockTransport(handler)))
    rows = c.query("sys_script", query="active=true", fields=["sys_id"], limit=10)
    assert [r["sys_id"] for r in rows] == ["1", "2"]
    assert seen["auth"] == "Bearer AT1"
    assert "/api/now/table/sys_script" in seen["url"]
    assert "sysparm_query=active%3Dtrue" in seen["url"]
    assert "sysparm_fields=sys_id" in seen["url"]
    assert "sysparm_limit=10" in seen["url"]


def test_get_record_returns_single_or_none():
    def handler(req):
        if "missing" in str(req.url):
            return httpx.Response(200, json={"result": []})
        return httpx.Response(200, json={"result": [{"sys_id": "abc", "name": "BR"}]})
    c = TableClient(INST, FakeToken(["AT"]), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.get_record("sys_script", "abc")["name"] == "BR"
    assert c.get_record("sys_script", "missing") is None


def test_401_triggers_one_refresh_retry_then_succeeds():
    state = {"n": 0}
    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"result": [{"sys_id": "1"}]})
    tok = FakeToken(["STALE", "FRESH"])
    c = TableClient(INST, tok, http=httpx.Client(transport=httpx.MockTransport(handler)))
    rows = c.query("sys_script")
    assert rows == [{"sys_id": "1"}]
    assert tok.calls == 2  # asked for a fresh token on retry
    assert tok.invalidate_calls == 1  # the retry forced a token refresh


def test_persistent_401_raises_auth_expired():
    def handler(req): return httpx.Response(401, json={"error": "expired"})
    c = TableClient(INST, FakeToken(["A", "B"]), http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthExpiredError):
        c.query("sys_script")


def test_query_sends_offset():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"result": []})
    c = TableClient(INST, FakeToken(["AT"]), http=httpx.Client(transport=httpx.MockTransport(handler)))
    c.query("sys_update_set", limit=25, offset=50)
    assert "sysparm_offset=50" in seen["url"]
    assert "sysparm_limit=25" in seen["url"]


def test_post_sends_body_and_returns_result():
    seen = {}
    def handler(req):
        seen["method"] = req.method
        seen["url"] = str(req.url)
        seen["body"] = req.content.decode()
        seen["ctype"] = req.headers.get("content-type")
        return httpx.Response(201, json={"result": {"sys_id": "NEW", "value": "SET1"}})
    c = TableClient(INST, FakeToken(["AT"]), http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = c.post("sys_user_preference", {"name": "sys_update_set", "value": "SET1"})
    assert out == {"sys_id": "NEW", "value": "SET1"}
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/now/table/sys_user_preference")
    assert '"value": "SET1"' in seen["body"] or '"value":"SET1"' in seen["body"]
    assert "application/json" in seen["ctype"]


def test_patch_targets_sys_id_and_returns_result():
    seen = {}
    def handler(req):
        seen["method"] = req.method
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"result": {"sys_id": "P1", "value": "SET2"}})
    c = TableClient(INST, FakeToken(["AT"]), http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = c.patch("sys_user_preference", "P1", {"value": "SET2"})
    assert out["value"] == "SET2"
    assert seen["method"] == "PATCH"
    assert seen["url"].endswith("/api/now/table/sys_user_preference/P1")


def test_put_targets_sys_id_and_returns_result():
    seen = {}
    def handler(req):
        seen["method"] = req.method
        seen["url"] = str(req.url)
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"result": {"sys_id": "W1", "template": "<b>hi</b>"}})
    c = TableClient(INST, FakeToken(["AT"]), http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = c.put("sp_widget", "W1", {"template": "<b>hi</b>"})
    assert out["template"] == "<b>hi</b>"
    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/api/now/table/sp_widget/W1")
    assert '"template"' in seen["body"]


def test_write_retries_once_on_401():
    state = {"n": 0}
    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"result": {"sys_id": "P1"}})
    tok = FakeToken(["STALE", "FRESH"])
    c = TableClient(INST, tok, http=httpx.Client(transport=httpx.MockTransport(handler)))
    c.patch("sys_user_preference", "P1", {"value": "X"})
    assert tok.invalidate_calls == 1 and tok.calls == 2


def test_write_persistent_401_raises():
    def handler(req): return httpx.Response(401, json={"error": "expired"})
    c = TableClient(INST, FakeToken(["A", "B"]), http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthExpiredError):
        c.post("sys_user_preference", {"x": "y"})
