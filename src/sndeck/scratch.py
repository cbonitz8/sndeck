"""Folder-naming rule for record folders: the sanitized record name plus its sys_id."""
from __future__ import annotations

import re

_SANITIZE = re.compile(r"[^A-Za-z0-9_.\- ]")


def folder_name(name: str, sys_id: str) -> str:
    clean = _SANITIZE.sub("_", name).strip() or "unnamed"
    return f"{clean}__{sys_id}"
