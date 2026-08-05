import httpx
import pytest


@pytest.fixture
def mock_client():
    """Factory: given an httpx MockTransport handler, return an httpx.Client backed by it."""
    def _make(handler):
        return httpx.Client(transport=httpx.MockTransport(handler))
    return _make


from sndeck.rest import TableClient
from sndeck.config import Instance

_INST = Instance("dev", "https://x.service-now.com", "cid",
                 "https://x.service-now.com/oauth_token.do", "dev")


class _FakeToken:
    def access_token(self): return "AT"
    def invalidate(self): pass


@pytest.fixture
def sn_client():
    """Factory: given get_routes(table, params)->list and optional write_handler(method, table, sys_id, body)->dict,
    return a TableClient backed by MockTransport that serves both reads and writes."""
    def _make(get_routes, write_handler=None):
        def handler(req):
            table = str(req.url.path).split("/api/now/table/", 1)[1].split("/", 1)[0]
            if req.method in ("POST", "PATCH", "PUT"):
                import json as _json
                sys_id = None if req.method == "POST" else str(req.url.path).rsplit("/", 1)[-1]
                body = _json.loads(req.content.decode() or "{}")
                result = write_handler(req.method, table, sys_id, body) if write_handler else {}
                return httpx.Response(200, json={"result": result})
            return httpx.Response(200, json={"result": get_routes(table, dict(req.url.params))})
        return TableClient(_INST, _FakeToken(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    return _make
