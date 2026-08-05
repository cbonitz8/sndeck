"""Resolve a ServiceNow Instance from the MCP fork's config file."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

DEFAULT_CONFIG = "~/.config/sndeck/instances.json"


@dataclass(frozen=True)
class Instance:
    name: str
    url: str
    client_id: str
    token_url: str
    account: str


def load_instance(name: str = "dev", *, config_path: str | None = None,
                  account: str | None = None) -> Instance:
    path = os.path.expanduser(config_path or os.environ.get("SNDECK_FORK_CONFIG", DEFAULT_CONFIG))
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
