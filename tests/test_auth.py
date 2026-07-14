import os

import httpx
import pytest
from sndeck import auth as auth_mod
from sndeck.auth import (TokenProvider, AuthExpiredError,
                         read_refresh_token, persist_refresh_token)
from sndeck.config import Instance

INST = Instance("dev", "https://x.service-now.com", "cid",
                "https://x.service-now.com/oauth_token.do", "cbonitz@x")


def const_read(value):
    """A read seam that always returns the same stored refresh token."""
    return lambda account: value


def test_access_token_replays_refresh_grant():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = dict(httpx.QueryParams(req.content.decode()))
        return httpx.Response(200, json={"access_token": "AT1", "expires_in": 1800})
    tp = TokenProvider(INST, read=const_read("RT1"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
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
    tp = TokenProvider(INST, read=const_read("RT1"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tp.access_token() == "AT1"
    assert tp.access_token() == "AT1"  # cached, no second refresh
    assert calls["n"] == 1


def test_rotated_refresh_token_persisted_via_seam():
    def handler(req):
        return httpx.Response(200, json={"access_token": "AT", "expires_in": 1800, "refresh_token": "RT2"})
    persisted = []
    TokenProvider(INST, read=const_read("RT1"),
                  persist=lambda account, token: persisted.append((account, token)),
                  http=httpx.Client(transport=httpx.MockTransport(handler))).access_token()
    assert persisted == [("cbonitz@x", "RT2")]


def test_unchanged_refresh_token_not_persisted():
    def handler(req):
        return httpx.Response(200, json={"access_token": "AT", "expires_in": 1800, "refresh_token": "RT1"})
    persisted = []
    TokenProvider(INST, read=const_read("RT1"),
                  persist=lambda account, token: persisted.append((account, token)),
                  http=httpx.Client(transport=httpx.MockTransport(handler))).access_token()
    assert persisted == []


def test_missing_refresh_token_raises_auth_expired():
    def no_http(req):
        raise AssertionError("must not make an HTTP call when refresh token is missing")
    tp = TokenProvider(INST, read=const_read(None),
                       http=httpx.Client(transport=httpx.MockTransport(no_http)))
    with pytest.raises(AuthExpiredError):
        tp.access_token()


def test_invalid_grant_raises_auth_expired():
    def handler(req):
        return httpx.Response(401, json={"error": "invalid_grant"})
    tp = TokenProvider(INST, read=const_read("RT1"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthExpiredError):
        tp.access_token()


def test_token_refetched_after_expiry():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": f"AT{calls['n']}", "expires_in": 100})
    clock = {"t": 1000.0}
    tp = TokenProvider(INST, read=const_read("RT1"),
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
    tp = TokenProvider(INST, read=const_read("RT1"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthExpiredError):
        tp.access_token()


# --- file-backed store -------------------------------------------------------

def test_file_store_round_trips_per_account(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert read_refresh_token("dev") is None
    persist_refresh_token("dev", "RT-dev")
    persist_refresh_token("prod", "RT-prod")
    assert read_refresh_token("dev") == "RT-dev"
    assert read_refresh_token("prod") == "RT-prod"


def test_file_store_uses_0600_file_in_0700_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    persist_refresh_token("dev", "RT-dev")
    d = tmp_path / "happy-platform-mcp"
    f = d / "token-dev"
    assert (f.stat().st_mode & 0o777) == 0o600
    assert (d.stat().st_mode & 0o777) == 0o700
    # no leftover temp file
    assert not (d / "token-dev.tmp").exists()


def test_file_store_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    persist_refresh_token("dev", "RT-1")
    persist_refresh_token("dev", "RT-2")
    assert read_refresh_token("dev") == "RT-2"


def test_file_store_rejects_unsafe_account(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        persist_refresh_token("../evil", "x")
    with pytest.raises(ValueError):
        read_refresh_token("a/b")


def test_token_provider_reads_and_persists_through_real_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    persist_refresh_token("cbonitz@x", "RT1")  # seed as the MCP fork would
    def handler(req):
        assert dict(httpx.QueryParams(req.content.decode()))["refresh_token"] == "RT1"
        return httpx.Response(200, json={"access_token": "AT", "expires_in": 1800, "refresh_token": "RT2"})
    tp = TokenProvider(INST, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tp.access_token() == "AT"
    # rotation was written back to the shared file
    assert read_refresh_token("cbonitz@x") == "RT2"
