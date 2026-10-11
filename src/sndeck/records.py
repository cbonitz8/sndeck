"""Pull SN records (all columns) into the store: the network->disk side of the one-copy
layout, whose paths and enumeration are owned by .store."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .refresh import plan_refresh
from .registry import CODE_ARTIFACTS
from . import snapshot
from .snapshot import is_dirty
from .store import EDIT, REF, Store


def default_kind(instance: str) -> str:
    """dev pulls are edit copies; every other instance pulls reference copies."""
    return EDIT if instance == "dev" else REF


@dataclass(frozen=True)
class PullOutcome:
    action: str                       # "created" | "refreshed" | "refused"
    table: str
    sys_id: str
    name: str
    kind: str
    path: Path
    target_set: dict | None
    reason: str | None = None
    blocked_fields: tuple[str, ...] = ()


def _write_fields(folder: Path, table: str, fields: dict) -> None:
    art = CODE_ARTIFACTS.get(table)
    if not art:
        return
    for f in art.script_fields:
        fp = folder / snapshot.field_file(f)
        val = fields.get(f)
        if val is None or val == "":
            if fp.exists():
                fp.unlink()
        else:
            fp.write_text(str(val), encoding="utf-8")


def _set_read_only(folder: Path, read_only: bool) -> None:
    mode = 0o444 if read_only else 0o644
    for p in folder.iterdir():
        if p.is_file():
            p.chmod(mode)


def pull(client, store: Store, table: str, sys_id: str, *, kind: str,
         target_set: dict | None = None, overwrite_local: bool = False) -> PullOutcome:
    """SN read → the one local copy of this kind. Never writes to ServiceNow.

    Absent → created. Present and clean, or dirty with every edit already live →
    refreshed, keeping its target_set. Present with an unpushed edit that differs from
    the instance → refused unless overwrite_local."""
    if kind == EDIT and target_set is None:
        raise ValueError("an edit pull needs a target update set")
    rec = client.get_record(table, sys_id, display_value="false")
    if rec is None:
        raise LookupError(f"{table}/{sys_id} not found")
    instance = client.instance.name
    name = rec.get("name") or rec.get("sys_name") or sys_id
    fields = snapshot.instance_fields(rec)
    existing = store.locate(instance, table, sys_id, kind)

    if existing is None:
        folder = store.new_path(instance, kind, table, name, sys_id)
        folder.mkdir(parents=True)
        extra = {"instance": instance, "kind": kind}
        if kind == EDIT:
            extra["target_set"] = target_set
        action = "created"
    else:
        folder = existing
        extra = snapshot.read_meta_extra(folder)
        if kind == EDIT:
            dirty = {c.field for c in snapshot.field_changes(folder)}
            blocked = sorted(dirty & set(plan_refresh(folder, fields).local_diverged))
            if blocked and not overwrite_local:
                if "target_set" not in extra:
                    raise snapshot.missing_key(folder, "target_set")
                return PullOutcome(
                    "refused", table, sys_id, name, kind, folder, extra["target_set"],
                    reason=(f"unpushed local edits on {blocked} differ from the instance — "
                            f"push them, or re-run with --overwrite-local to discard them"),
                    blocked_fields=tuple(blocked))
        else:
            _set_read_only(folder, False)
        action = "refreshed"

    snapshot.write_record_json(folder, table, sys_id, name, fields, extra)
    snapshot.write_snapshot(folder, fields)
    _write_fields(folder, table, fields)
    if kind == REF:
        _set_read_only(folder, True)
    return PullOutcome(action, table, sys_id, name, kind, folder, extra.get("target_set"))


def retarget(store: Store, instance: str, table: str, sys_id: str, target_set: dict) -> Path:
    """Point an edit copy's push at a different update set."""
    path = store.locate(instance, table, sys_id, EDIT)
    if path is None:
        raise LookupError(f"no edit copy of {table}/{sys_id} on {instance}")
    snapshot.write_meta_extra(path, target_set=target_set)
    return path


@dataclass(frozen=True)
class StorePullSummary:
    pulled: int
    refused: int
    skipped: int


def pull_set_into_store(client, store: Store, set_sys_id: str, set_name: str, *,
                        kind: str) -> StorePullSummary:
    """Pull every record the set holds as `kind` copies. Edit copies target that set; a
    record that already has an edit copy keeps its own target_set."""
    from .updatesets import update_set_records
    _total, recs = update_set_records(client, set_sys_id)
    if kind == EDIT:
        target = {"sys_id": set_sys_id, "name": set_name}
    else:
        target = None
    pulled = refused = skipped = 0
    for table, sys_id in recs:
        try:
            o = pull(client, store, table, sys_id, kind=kind, target_set=target)
        except LookupError:
            skipped += 1
            continue
        if o.action == "refused":
            refused += 1
        else:
            pulled += 1
    return StorePullSummary(pulled, refused, skipped)


def dirty_files_from_disk(store: Store, instance: str) -> list:
    """Every dirty edit copy for `instance` — the staging set, read from disk alone.
    Sorted by (table, name). Feeds the staging pane and push-all so newly pulled
    records appear without a full refresh."""
    from .tree import FileNode          # local import: tree imports records at module top
    out = [FileNode(table=c.table, sys_id=c.sys_id, name=c.name, in_current_set=False,
                    tracked=False, local=True, dirty=True, record_path=c.path)
           for c in store.iter_copies(instance, EDIT) if is_dirty(c.path)]
    out.sort(key=lambda f: (f.table, f.name))
    return out
