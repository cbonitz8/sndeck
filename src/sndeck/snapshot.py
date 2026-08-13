"""The on-disk record snapshot: content, not folder naming.

A pulled record folder holds three things, and this module is the single owner of
all three shapes plus the one definition of "dirty":

  - record.json     ``{"_meta": {table, sys_id, name, pulled_at}, **fields}``
                    identity + the full non-underscore column dump.
  - .snapshot.json  ``{**fields}`` — the frozen baseline a local edit is diffed against.
  - <field><ext>    one file per code field — the editable surface.

`scratch.py` owns *where* a record folder lives (naming, enumeration); this owns
*what* is inside it. Before this module existed the snapshot read/write, the
record.json body shape, the field->filename rule, and the dirty predicate were
duplicated across records / sync / refresh — and refresh computed "dirty" a second,
divergent way. Everything snapshot-shaped now routes through here."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .registry import CODE_ARTIFACTS, field_extension

RECORD_JSON = "record.json"
SNAPSHOT_JSON = ".snapshot.json"


def _norm(s: str) -> str:
    """Newline-normalize for field comparison. The arbiter of whether two field
    bodies are 'equal' — CRLF/CR differences are not edits."""
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


def field_file(field: str) -> str:
    """The on-disk filename for a code field: `script` -> `script.js`. The single
    owner of the field->filename convention (was duplicated 4x)."""
    return f"{field}{field_extension(field)}"


def instance_fields(record: dict) -> dict:
    """The persisted-snapshot view of a live record: every non-underscore column.
    Matches exactly what a pull writes to .snapshot.json (SN adds `_meta`-style
    underscore keys we never persist)."""
    return {k: v for k, v in record.items() if not k.startswith("_")}


def read_meta(record_path) -> tuple[str, str, str]:
    """(table, sys_id, name) from a folder's record.json `_meta`."""
    meta = json.loads((Path(record_path) / RECORD_JSON).read_text()).get("_meta", {})
    return meta.get("table", ""), meta.get("sys_id", ""), meta.get("name", "")


def read_snapshot(record_path) -> dict:
    """The frozen baseline fields, or {} if the folder has no snapshot yet."""
    p = Path(record_path) / SNAPSHOT_JSON
    return json.loads(p.read_text()) if p.exists() else {}


def write_snapshot(record_path, fields: dict) -> None:
    """Freeze `fields` as the new baseline."""
    (Path(record_path) / SNAPSHOT_JSON).write_text(
        json.dumps(fields, indent=2), encoding="utf-8")


def record_body(table: str, sys_id: str, name: str, fields: dict) -> dict:
    """The record.json document: `_meta` identity + a fresh pulled_at, then fields."""
    return {"_meta": {"table": table, "sys_id": sys_id, "name": name,
                      "pulled_at": datetime.now(timezone.utc).isoformat()}, **fields}


def write_record_json(record_path, table: str, sys_id: str, name: str, fields: dict) -> None:
    """Write record.json from `fields`, stamping a fresh pulled_at."""
    (Path(record_path) / RECORD_JSON).write_text(
        json.dumps(record_body(table, sys_id, name, fields), indent=2), encoding="utf-8")


@dataclass(frozen=True)
class FieldChange:
    field: str
    local: str
    snapshot: str


def field_changes(record_path) -> list[FieldChange]:
    """Code fields whose local file differs from the frozen snapshot — the canonical
    definition of a local edit. Empty for non-artifact tables."""
    record_path = Path(record_path)
    table, _, _ = read_meta(record_path)
    art = CODE_ARTIFACTS.get(table)
    if not art:
        return []
    snap = read_snapshot(record_path)
    changes: list[FieldChange] = []
    for f in art.script_fields:
        fp = record_path / field_file(f)
        if not fp.exists():
            continue
        local = fp.read_text(encoding="utf-8")
        snapv = str(snap.get(f, "") or "")
        if _norm(local) != _norm(snapv):
            changes.append(FieldChange(f, local, snapv))
    return changes


def is_dirty(record_path) -> bool:
    """True when the record has an unpushed local edit. The one predicate every
    caller (staging pane, push, prune, refresh) shares."""
    try:
        return bool(field_changes(record_path))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
