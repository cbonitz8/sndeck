import httpx
import pytest
from sndeck.auth import TokenProvider, AuthExpiredError
from sndeck.config import Instance

INST = Instance("dev", "https://x.service-now.com", "cid",
                "https://x.service-now.com/oauth_token.do", "cbonitz@x")


class FakeKeyring:
    def __init__(self, secret): self.store = {("happy-platform-mcp", "cbonitz@x"): secret}
    def get_password(self, s, a): return self.store.get((s, a))
    def set_password(self, s, a, v): self.store[(s, a)] = v


def test_access_token_replays_refresh_grant():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = dict(httpx.QueryParams(req.content.decode()))
        return httpx.Response(200, json={"access_token": "AT1", "expires_in": 1800})
    kr = FakeKeyring("RT1")
    tp = TokenProvider(INST, keyring_mod=kr, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tp.access_token() == "AT1"
    assert seen["url"] == INST.token_url
    assert seen["body"]["grant_type"] == "refresh_token"
    assert seen["body"]["client_id"] == "cid"
    assert seen["body"]["refresh_token"] == "RT1"


def test_access_token_is_cached_until_expiry():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": f"AT{calls['n']}", "expires_in": 1800})
    tp = TokenProvider(INST, keyring_mod=FakeKeyring("RT1"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tp.access_token() == "AT1"
    assert tp.access_token() == "AT1"  # cached, no second refresh
    assert calls["n"] == 1


def test_rotated_refresh_token_written_back():
    def handler(req):
        return httpx.Response(200, json={"access_token": "AT", "expires_in": 1800, "refresh_token": "RT2"})
    kr = FakeKeyring("RT1")
    TokenProvider(INST, keyring_mod=kr,
                  http=httpx.Client(transport=httpx.MockTransport(handler))).access_token()
    assert kr.store[("happy-platform-mcp", "cbonitz@x")] == "RT2"


def test_missing_refresh_token_raises_auth_expired():
    def no_http(req):
        raise AssertionError("must not make an HTTP call when refresh token is missing")
    tp = TokenProvider(INST, keyring_mod=FakeKeyring(None),
                       http=httpx.Client(transport=httpx.MockTransport(no_http)))
    with pytest.raises(AuthExpiredError):
        tp.access_token()


def test_invalid_grant_raises_auth_expired():
    def handler(req):
        return httpx.Response(401, json={"error": "invalid_grant"})
    tp = TokenProvider(INST, keyring_mod=FakeKeyring("RT1"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthExpiredError):
        tp.access_token()


def test_token_refetched_after_expiry():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": f"AT{calls['n']}", "expires_in": 100})
    clock = {"t": 1000.0}
    tp = TokenProvider(INST, keyring_mod=FakeKeyring("RT1"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)),
                       now=lambda: clock["t"])
    assert tp.access_token() == "AT1"      # fetch 1 (expires_at = 1000 + (100-60) = 1040)
    clock["t"] = 1039.0
    assert tp.access_token() == "AT1"      # still cached
    clock["t"] = 1041.0
    assert tp.access_token() == "AT2"      # past expiry → refetch
    assert calls["n"] == 2


def test_malformed_200_raises_auth_expired():
    def handler(req):
        return httpx.Response(200, json={"no_token_here": True})
    tp = TokenProvider(INST, keyring_mod=FakeKeyring("RT1"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthExpiredError):
        tp.access_token()
