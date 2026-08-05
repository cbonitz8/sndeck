"""Reuse the MCP fork's keychain refresh token to mint SN access tokens."""
from __future__ import annotations

import time

import httpx
import keyring as _keyring

from .config import Instance

SERVICE = "happy-platform-mcp"
EXPIRY_BUFFER = 60  # refresh this many seconds before actual expiry


class AuthExpiredError(Exception):
    """Refresh token missing or rejected — the user must re-auth via the fork."""


class TokenProvider:
    def __init__(self, instance: Instance, *, keyring_mod=_keyring,
                 http: httpx.Client | None = None, now=time.monotonic):
        self.instance = instance
        self._keyring = keyring_mod
        self._http = http or httpx.Client(timeout=30)
        self._now = now
        self._token: str | None = None
        self._expires_at: float = 0.0

    def access_token(self) -> str:
        if self._token and self._now() < self._expires_at:
            return self._token
        return self._refresh()

    def invalidate(self) -> None:
        self._expires_at = 0.0

    def _refresh(self) -> str:
        rt = self._keyring.get_password(SERVICE, self.instance.account)
        if not rt:
            raise AuthExpiredError(
                f"No refresh token for {self.instance.account}. Sign in via the MCP fork.")
        resp = self._http.post(self.instance.token_url, data={
            "grant_type": "refresh_token",
            "client_id": self.instance.client_id,
            "refresh_token": rt,
        })
        if resp.status_code != 200:
            raise AuthExpiredError(f"Refresh grant rejected ({resp.status_code}). Re-auth via the fork.")
        body = resp.json()
        try:
            self._token = body["access_token"]
            expires_in = int(body.get("expires_in", 1800))
        except (KeyError, ValueError, TypeError):
            raise AuthExpiredError("Malformed token response from the auth server.")
        self._expires_at = self._now() + max(0, expires_in - EXPIRY_BUFFER)
        if body.get("refresh_token") and body["refresh_token"] != rt:
            self._keyring.set_password(SERVICE, self.instance.account, body["refresh_token"])
        return self._token
