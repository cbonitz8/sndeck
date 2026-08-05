"""Resolve a ServiceNow Instance from the MCP fork's config file."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

# Instance config is looked up in this order; the first existing file wins.
# sndeck reuses the happy-platform-mcp instance registry when present, so a user
# who has configured the MCP does not duplicate config for sndeck.
DEFAULT_CONFIG_CANDIDATES = (
    "~/.config/happy-platform-mcp/instances.json",  # MCP's user registry
    "~/.config/sndeck/instances.json",              # sndeck's own fallback
)
# Back-compat alias for anything importing the old name.
DEFAULT_CONFIG = DEFAULT_CONFIG_CANDIDATES[0]


def _resolve_config_path(config_path: str | None = None) -> str:
    """Resolve the instance-config file path.

    Precedence: explicit arg > SNDECK_INSTANCES_CONFIG env > SNDECK_FORK_CONFIG env
    (legacy alias) > HAPPY_CONFIG_PATH env (the MCP's own var) > first existing entry
    in DEFAULT_CONFIG_CANDIDATES. If none exist, returns the first candidate so a
    downstream open() error names a sensible path.
    """
    if config_path:
        return os.path.expanduser(config_path)
    for env in ("SNDECK_INSTANCES_CONFIG", "SNDECK_FORK_CONFIG", "HAPPY_CONFIG_PATH"):
        val = os.environ.get(env)
        if val:
            return os.path.expanduser(val)
    for cand in DEFAULT_CONFIG_CANDIDATES:
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            return p
    return os.path.expanduser(DEFAULT_CONFIG_CANDIDATES[0])


@dataclass(frozen=True)
class Instance:
    name: str
    url: str
    client_id: str
    token_url: str
    account: str


def load_instance(name: str = "dev", *, config_path: str | None = None,
                  account: str | None = None) -> Instance:
    path = _resolve_config_path(config_path)
    acct = account or os.environ.get("SNDECK_ACCOUNT") or name
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for inst in data.get("instances", []):
        if inst.get("name") == name:
            url = inst["url"].rstrip("/")
            return Instance(
                name=name,
                url=url,
                client_id=inst["clientId"],
                token_url=inst.get("tokenUrl") or f"{url}/oauth_token.do",
                account=acct,
            )
    raise KeyError(f"instance {name!r} not found in {path}")
