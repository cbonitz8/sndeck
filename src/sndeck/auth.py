"""Reuse the MCP fork's shared refresh token (a 0600 file) to mint SN access tokens."""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from .config import Instance

SERVICE = "happy-platform-mcp"
EXPIRY_BUFFER = 60  # refresh this many seconds before actual expiry

# The refresh token is SHARED with the MCP fork. It used to live in the macOS
# keychain, but that item is read by two differently-code-signed binaries — node
# (the MCP server) and this python3 (sndeck) — and macOS gates keychain access
# by a per-code-signature "partition list" that no `security` flag durably opens
# for both (node is self-signed, so its partition changes on every re-sign).
# That was an unwinnable game of re-prompts. Both tools now share a 0600 file
# under ~/.config/happy-platform-mcp instead: zero prompts, forever, surviving
# node re-signs and python upgrades. The fork's token-store.js FileTokenStore
# uses the exact same on-disk convention.


def _token_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / SERVICE


def _token_file(account: str) -> Path:
    if "/" in account or ".." in account:
        raise ValueError(f"unsafe account key: {account!r}")
    return _token_dir() / f"token-{account}"


def read_refresh_token(account: str) -> str | None:
    try:
        token = _token_file(account).read_text().strip()
    except FileNotFoundError:
        return None
    return token or None


def persist_refresh_token(account: str, token: str) -> None:
    """Write the refresh token to its 0600 file (0700 dir), via a temp file +
    atomic rename so a concurrent reader never sees a half-written token."""
    directory = _token_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    file = _token_file(account)
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(token)
    os.chmod(tmp, 0o600)  # defeat umask
    os.replace(tmp, file)


class AuthExpiredError(Exception):
    """Refresh token missing or rejected — the user must re-auth via the fork."""


class TokenProvider:
    def __init__(self, instance: Instance, *, read=read_refresh_token,
                 persist=persist_refresh_token, http: httpx.Client | None = None,
                 now=time.monotonic):
        self.instance = instance
        self._read = read
        self._persist = persist
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
        rt = self._read(self.instance.account)
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
            self._persist(self.instance.account, body["refresh_token"])
        return self._token
