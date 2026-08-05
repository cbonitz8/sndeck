"""ServiceNow Table API client. GET plus guarded writes: session-preference
(set-switch, POST/PATCH) and code-artifact fields (push, PUT — the capturing path)."""
from __future__ import annotations

import httpx

from .auth import AuthExpiredError
from .config import Instance


class TableClient:
    def __init__(self, instance: Instance, token_provider, *, http: httpx.Client | None = None):
        self.instance = instance
        self.tokens = token_provider
        self._http = http or httpx.Client(timeout=30)

    def _get(self, table: str, params: dict) -> list[dict]:
        url = f"{self.instance.url}/api/now/table/{table}"
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self.tokens.access_token()}",
                       "Accept": "application/json"}
            resp = self._http.get(url, params=params, headers=headers)
            if resp.status_code == 401:
                if attempt == 1:
                    self.tokens.invalidate()
                    continue  # token may be stale — force a refresh and retry once
                raise AuthExpiredError("SN rejected the access token (401). Re-auth via the fork.")
            resp.raise_for_status()
            return resp.json().get("result", [])
        raise RuntimeError("unreachable: _get loop always returns or raises")

    def query(self, table: str, *, query: str | None = None, fields: list[str] | None = None,
              display_value: str = "false", limit: int | None = None,
              offset: int | None = None) -> list[dict]:
        params: dict = {"sysparm_display_value": display_value}
        if query:
            params["sysparm_query"] = query
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        if limit is not None:
            params["sysparm_limit"] = limit
        if offset is not None:
            params["sysparm_offset"] = offset
        return self._get(table, params)

    def get_record(self, table: str, sys_id: str, *, fields: list[str] | None = None,
                   display_value: str = "false") -> dict | None:
        rows = self.query(table, query=f"sys_id={sys_id}", fields=fields,
                          display_value=display_value, limit=1)
        return rows[0] if rows else None

    def _write(self, method: str, url: str, body: dict) -> dict:
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self.tokens.access_token()}",
                       "Accept": "application/json", "Content-Type": "application/json"}
            resp = self._http.request(method, url, json=body, headers=headers)
            if resp.status_code == 401:
                if attempt == 1:
                    self.tokens.invalidate()
                    continue  # token may be stale — force a refresh and retry once
                raise AuthExpiredError("SN rejected the access token (401). Re-auth via the fork.")
            resp.raise_for_status()
            return resp.json().get("result", {})
        raise RuntimeError("unreachable: _write loop always returns or raises")

    def post(self, table: str, body: dict) -> dict:
        return self._write("POST", f"{self.instance.url}/api/now/table/{table}", body)

    def patch(self, table: str, sys_id: str, body: dict) -> dict:
        return self._write("PATCH", f"{self.instance.url}/api/now/table/{table}/{sys_id}", body)

    def put(self, table: str, sys_id: str, body: dict) -> dict:
        """Update a record via HTTP PUT (the update-set-capturing write path).

        Used by the code-artifact push. A PATCH to the Table API does not reliably
        fire ServiceNow's customer-update engine for records with
        sys_customer_update=false — the write lands live but no sys_update_xml row is
        created, so the change cannot be promoted via the current update set. PUT
        mirrors the fork's session-aware SN-Update-Record, which does capture. Like
        PATCH, it only writes the fields present in `body`; omitted fields are left
        untouched."""
        return self._write("PUT", f"{self.instance.url}/api/now/table/{table}/{sys_id}", body)
