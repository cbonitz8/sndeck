"""Push logic: diff local field files vs the pulled .snapshot.json, and (Task 14)
detect instance drift before writing. The local diff also powers the tree's ✎ flag."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .registry import CODE_ARTIFACTS, field_extension


def _norm(s: str) -> str:
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class FieldChange:
    field: str
    local: str
    snapshot: str


def _read_meta(record_path) -> tuple[str, str, str]:
    meta = json.loads((Path(record_path) / "record.json").read_text()).get("_meta", {})
    return meta.get("table", ""), meta.get("sys_id", ""), meta.get("name", "")


def _read_snapshot(record_path) -> dict:
    p = Path(record_path) / ".snapshot.json"
    return json.loads(p.read_text()) if p.exists() else {}


def local_field_changes(record_path) -> list[FieldChange]:
    record_path = Path(record_path)
    table, _, _ = _read_meta(record_path)
    art = CODE_ARTIFACTS.get(table)
    if not art:
        return []
    snap = _read_snapshot(record_path)
    changes: list[FieldChange] = []
    for f in art.script_fields:
        fp = record_path / f"{f}{field_extension(f)}"
        if not fp.exists():
            continue
        local = fp.read_text(encoding="utf-8")
        snapv = str(snap.get(f, "") or "")
        if _norm(local) != _norm(snapv):
            changes.append(FieldChange(f, local, snapv))
    return changes


def is_dirty(record_path) -> bool:
    try:
        return bool(local_field_changes(record_path))
    except (FileNotFoundError, json.JSONDecodeError):
        return False


@dataclass(frozen=True)
class PushPlan:
    table: str
    sys_id: str
    name: str
    changes: list
    drifted: list
    missing: bool


def build_push_plan(client, record_path) -> PushPlan:
    record_path = Path(record_path)
    table, sys_id, name = _read_meta(record_path)
    changes = local_field_changes(record_path)
    snap = _read_snapshot(record_path)
    rec = client.get_record(table, sys_id, display_value="false")
    if rec is None:
        return PushPlan(table, sys_id, name, changes, [], True)
    art = CODE_ARTIFACTS.get(table)
    fields = art.script_fields if art else ()
    drifted = [f for f in fields
               if _norm(str(rec.get(f, "") or "")) != _norm(str(snap.get(f, "") or ""))]
    return PushPlan(table, sys_id, name, changes, drifted, False)


def apply_push(client, plan: PushPlan) -> None:
    if plan.missing:
        raise RuntimeError(f"{plan.table}/{plan.sys_id} no longer exists on the instance")
    blocked = {c.field for c in plan.changes} & set(plan.drifted)
    if blocked:
        raise RuntimeError(f"instance changed since pull on {sorted(blocked)} — refresh first")
    body = {c.field: c.local for c in plan.changes}
    if body:
        # PUT, not PATCH: PATCH does not fire ServiceNow's customer-update engine for
        # records with sys_customer_update=false, so the write lands live but never
        # captures into the current update set. PUT mirrors the fork's session-aware
        # SN-Update-Record, which does capture. See TableClient.put.
        client.put(plan.table, plan.sys_id, body)
