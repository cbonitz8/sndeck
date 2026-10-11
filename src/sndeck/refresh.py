"""Re-baseline a pulled record's .snapshot.json from the live instance, decoupled
from update-set state.

This is the missing other half of apply_push's "instance changed since pull … —
refresh first" error: that error tells the user to refresh, but until now there was
no way to do it. An edit copy whose local field files match the instance while its
frozen snapshot does not (e.g. the record was pushed via a *different* set) is "phantom
dirty": while its target set is in progress it keeps showing as an unpushed edit. Once
the set ships gc deletes it as already-live (C2), and a re-pull clears it, because pull
blocks only fields that are dirty AND differ from the instance; refresh clears it
without waiting.

Design (see module tests): snapshot-only by default. Rewriting only the snapshot
resolves the already-pushed case to clean (local == instance == new snapshot → not
dirty → gc-eligible once the set ships) and leaves the drift guard intact for the genuinely-divergent case.
If the local field files DIFFER from the instance, a snapshot-only rewrite would both
leave the record dirty AND silently retire apply_push's drift protection (a later push
would clobber the instance), so we refuse unless --overwrite-local is passed, which
replaces the local field files with the instance copy (discarding the local edit).

Pure/disk-only core: the planner and writer take an already-fetched instance record
dict, so they are network-free and testable exactly like sync's planner. The
CLI owns the single client.get_record call.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .registry import CODE_ARTIFACTS
from . import snapshot
from .snapshot import _norm, field_file, instance_fields as _instance_fields


@dataclass(frozen=True)
class RefreshPlan:
    """What a refresh would touch, computed against an already-fetched instance record.

    snapshot_stale  — code fields whose frozen snapshot differs from the instance
                      (the drift baseline that a rebase would correct).
    local_diverged  — code fields whose local field FILE differs from the instance
                      (a real, unpushed local edit). Non-empty => snapshot-only refresh
                      is refused unless the caller passes overwrite_local.
    """
    table: str
    sys_id: str
    name: str
    snapshot_stale: list[str]
    local_diverged: list[str]


def plan_refresh(record_path, instance_fields: dict) -> RefreshPlan:
    """Network-free classification of a refresh. `instance_fields` is the live record's
    non-underscore columns (see _instance_fields)."""
    record_path = Path(record_path)
    table, sys_id, name = snapshot.read_meta(record_path)
    snap = snapshot.read_snapshot(record_path)
    art = CODE_ARTIFACTS.get(table)
    fields = art.script_fields if art else ()

    snapshot_stale: list[str] = []
    local_diverged: list[str] = []
    for f in fields:
        inst_v = str(instance_fields.get(f, "") or "")
        if _norm(str(snap.get(f, "") or "")) != _norm(inst_v):
            snapshot_stale.append(f)
        fp = record_path / field_file(f)
        if fp.exists() and _norm(fp.read_text(encoding="utf-8")) != _norm(inst_v):
            local_diverged.append(f)
    return RefreshPlan(table, sys_id, name, snapshot_stale, local_diverged)


@dataclass(frozen=True)
class RefreshOutcome:
    table: str
    sys_id: str
    name: str
    folder: str
    refreshed: bool              # .snapshot.json was rewritten
    refused: bool                # divergent local edit, no --overwrite-local
    missing: bool                # record no longer exists on the instance
    reason: str | None           # why refused/missing, else None
    snapshot_changed: list[str]  # code fields whose snapshot baseline moved
    local_changed: list[str]     # code fields whose local file was overwritten
    clean_after: bool            # record is not dirty after the refresh


def _refuse_reason(local_diverged: list[str]) -> str:
    return (f"local edits differ from the instance on {sorted(local_diverged)} — "
            "refusing to rebase the snapshot alone (that would leave the record dirty "
            "and retire push's drift guard). Re-run with --overwrite-local to replace "
            "the local files with the instance copy, or push/resolve the edit first")


def missing_outcome(record_path, table: str, sys_id: str, name: str) -> RefreshOutcome:
    """Outcome for a record the instance no longer has. No files are touched."""
    return RefreshOutcome(table, sys_id, name, str(record_path), False, False, True,
                          f"{table}/{sys_id} no longer exists on the instance",
                          [], [], False)


def apply_refresh(record_path, instance_record: dict, *,
                  overwrite_local: bool = False) -> RefreshOutcome:
    """Re-baseline `record_path`'s snapshot from `instance_record` (an already-fetched
    live record). Network-free — the caller performs the fetch.

    Snapshot-only by default: rewrites .snapshot.json to the instance's columns. If the
    local field files diverge from the instance and overwrite_local is False, refuses
    and touches nothing. With overwrite_local, also rewrites the local field files and
    record.json from the instance (discarding the local edit)."""
    record_path = Path(record_path)
    inst_fields = _instance_fields(instance_record)
    plan = plan_refresh(record_path, inst_fields)

    if plan.local_diverged and not overwrite_local:
        return RefreshOutcome(plan.table, plan.sys_id, plan.name, str(record_path),
                              False, True, False, _refuse_reason(plan.local_diverged),
                              [], [], False)

    snapshot.write_snapshot(record_path, inst_fields)

    local_changed: list[str] = []
    if overwrite_local:
        art = CODE_ARTIFACTS.get(plan.table)
        for f in (art.script_fields if art else ()):
            fp = record_path / field_file(f)
            newv = str(inst_fields.get(f, "") or "")
            oldv = fp.read_text(encoding="utf-8") if fp.exists() else ""
            if newv == "":
                if fp.exists():
                    fp.unlink()
                    local_changed.append(f)
            else:
                if _norm(oldv) != _norm(newv):
                    local_changed.append(f)
                fp.write_text(newv, encoding="utf-8")
        # --overwrite-local is a full local re-materialization; snapshot-only refresh
        # never touches record.json.
        snapshot.write_record_json(record_path, plan.table, plan.sys_id, plan.name,
                                   inst_fields, snapshot.read_meta_extra(record_path))

    return RefreshOutcome(plan.table, plan.sys_id, plan.name, str(record_path),
                          True, False, False, None,
                          list(plan.snapshot_stale), local_changed, True)
