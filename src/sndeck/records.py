"""Pull SN records (all columns) to disk as folders; scan them back.

The on-disk scratch layout itself (folder naming, set-dir/record enumeration)
is owned by .scratch — this module re-exports those names for existing
importers and focuses on the network->disk pull."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .registry import CODE_ARTIFACTS, field_extension
from .sync import is_dirty
from .scratch import (RECORD_JSON, SNAPSHOT_JSON, _SET_DIR, RecordRef, WorkspaceRef,
                      folder_name as _folder_name, set_dir_name, set_workspace,
                      scan_scratch, scan_workspace, delete_record_folders)


def pull_record(client, table: str, sys_id: str, scratch_dir) -> RecordRef:
    """SN read → local write. Never writes to ServiceNow."""
    rec = client.get_record(table, sys_id, display_value="false")
    if rec is None:
        raise LookupError(f"{table}/{sys_id} not found")
    name = rec.get("name") or rec.get("sys_name") or sys_id
    folder = Path(scratch_dir) / table / _folder_name(name, sys_id)
    folder.mkdir(parents=True, exist_ok=True)

    fields = {k: v for k, v in rec.items() if not k.startswith("_")}
    body = {"_meta": {"table": table, "sys_id": sys_id, "name": name,
                      "pulled_at": datetime.now(timezone.utc).isoformat()}, **fields}
    (folder / RECORD_JSON).write_text(json.dumps(body, indent=2), encoding="utf-8")
    (folder / SNAPSHOT_JSON).write_text(json.dumps(fields, indent=2), encoding="utf-8")

    artifact = CODE_ARTIFACTS.get(table)
    if artifact:
        for f in artifact.script_fields:
            if f in fields and fields[f] not in (None, ""):
                (folder / f"{f}{field_extension(f)}").write_text(str(fields[f]), encoding="utf-8")

    return RecordRef(table, sys_id, name, folder)


def dirty_files_from_disk(scratch) -> list:
    """Every locally-edited, unpushed record on disk — the staging set, sourced purely
    from the scratch dir with no network model. Deduped by (table, sys_id), sorted by
    (table, name). Feeds the staging pane and push-all so newly pulled/added records
    appear without a full refresh."""
    from .tree import FileNode          # local import: tree imports records at module top

    seen: set[tuple[str, str]] = set()
    out = []
    for wref in scan_workspace(scratch):
        ref = wref.ref
        key = (ref.table, ref.sys_id)
        if key in seen:
            continue
        if not is_dirty(ref.path):
            continue
        seen.add(key)
        out.append(FileNode(
            table=ref.table, sys_id=ref.sys_id, name=ref.name,
            in_current_set=False, tracked=False, local=True, dirty=True,
            record_path=ref.path,
        ))
    out.sort(key=lambda f: (f.table, f.name))
    return out


def folders_for_records(scratch_dir, records) -> list[Path]:
    """Existing scratch folders for the given (table, sys_id) pairs."""
    wanted = {(t, s) for t, s in records}
    return [ref.path for ref in scan_scratch(scratch_dir)
            if (ref.table, ref.sys_id) in wanted]
