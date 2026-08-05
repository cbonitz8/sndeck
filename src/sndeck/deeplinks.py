"""Build ServiceNow UI URLs for deep-linking records and update sets."""
from __future__ import annotations


def instance_url_for(instance, *, kind: str, sys_id: str, table: str | None = None) -> str:
    base = instance.url.rstrip("/")
    if kind == "update_set":
        return f"{base}/sys_update_set.do?sys_id={sys_id}"
    if kind == "record":
        if not table:
            raise ValueError("record deep-link requires a table")
        return f"{base}/{table}.do?sys_id={sys_id}"
    raise ValueError(f"unknown deep-link kind {kind!r}")
