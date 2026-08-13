"""Push logic: diff local field files vs the pulled .snapshot.json, and detect
instance drift before writing. The snapshot read/write, the field->file rule, and the
"dirty" predicate live in .snapshot; this module owns the push plan built on top of them.

The local-edit names (`local_field_changes`, `is_dirty`, `FieldChange`, `_norm`) are
re-exported from .snapshot for importers and tests that still reach for them here."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .registry import CODE_ARTIFACTS
from .snapshot import (FieldChange, _norm, field_changes as local_field_changes,
                       is_dirty, read_meta as _read_meta, read_snapshot as _read_snapshot)

__all__ = ["FieldChange", "local_field_changes", "is_dirty", "PushPlan",
           "build_push_plan", "apply_push"]


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
