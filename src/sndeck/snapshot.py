"""The on-disk record snapshot: content, not folder naming.

A pulled record folder holds three things, and this module is the single owner of
all three shapes plus the one definition of "dirty":

  - record.json     ``{"_meta": {table, sys_id, name, instance, kind, target_set, pulled_at}, **fields}``
                    identity + the full non-underscore column dump.
  - .snapshot.json  ``{**fields}`` — the frozen baseline a local edit is diffed against.
  - <field><ext>    one file per code field — the editable surface.

`scratch.py` owns *where* a record folder lives (naming, enumeration); this owns
*what* is inside it. Before this module existed the snapshot read/write, the
record.json body shape, the field->filename rule, and the dirty predicate were
duplicated across records / sync / refresh — and refresh computed "dirty" a second,
divergent way. Everything snapshot-shaped now routes through here."""
from __future__ import annotations

import difflib
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


META_EXTRA_KEYS = ("instance", "kind", "target_set")


def record_body(table: str, sys_id: str, name: str, fields: dict,
                extra: dict | None = None) -> dict:
    """The record.json document: `_meta` identity, the store keys in `extra`, a fresh
    pulled_at, then fields."""
    meta = {"table": table, "sys_id": sys_id, "name": name}
    if extra is not None:
        meta.update(extra)
    meta["pulled_at"] = datetime.now(timezone.utc).isoformat()
    return {"_meta": meta, **fields}


def write_record_json(record_path, table: str, sys_id: str, name: str, fields: dict,
                      extra: dict | None = None) -> None:
    """Write record.json from `fields`, stamping a fresh pulled_at."""
    (Path(record_path) / RECORD_JSON).write_text(
        json.dumps(record_body(table, sys_id, name, fields, extra), indent=2),
        encoding="utf-8")


def read_meta_extra(record_path) -> dict:
    """The store keys (instance, kind, target_set) present in a folder's _meta."""
    meta = json.loads((Path(record_path) / RECORD_JSON).read_text())["_meta"]
    return {k: meta[k] for k in META_EXTRA_KEYS if k in meta}


def write_meta_extra(record_path, **updates) -> None:
    """Update store keys in _meta in place; fields and pulled_at are untouched."""
    p = Path(record_path) / RECORD_JSON
    doc = json.loads(p.read_text())
    for k, v in updates.items():
        if k not in META_EXTRA_KEYS:
            raise KeyError(f"{k!r} is not a store meta key")
        doc["_meta"][k] = v
    p.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _body(fields: dict, field: str) -> str:
    v = fields.get(field)
    return "" if v is None else str(v)


def local_diff(record_path, live_fields: dict) -> str:
    """Unified diff of each code field, instance -> local. Empty when they match."""
    record_path = Path(record_path)
    table, _, _ = read_meta(record_path)
    art = CODE_ARTIFACTS.get(table)
    if not art:
        return ""
    chunks: list[str] = []
    for f in art.script_fields:
        fp = record_path / field_file(f)
        local = _norm(fp.read_text(encoding="utf-8")) if fp.exists() else ""
        live = _norm(_body(live_fields, f))
        if local != live:
            chunks.extend(difflib.unified_diff(
                live.splitlines(keepends=True), local.splitlines(keepends=True),
                fromfile=f"instance/{field_file(f)}", tofile=f"local/{field_file(f)}"))
    return "".join(chunks)


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
    """True when the record has an unpushed local edit. Used by the staging pane,
    push and status; gc and migrate use is_dirty_or_unreadable."""
    try:
        return bool(field_changes(record_path))
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def is_dirty_or_unreadable(record_path) -> bool:
    """Like is_dirty, but a corrupt snapshot counts as dirty. is_dirty answers "is
    there an edit worth pushing"; this answers "is it safe to delete this copy", and
    an edit that cannot be proven absent must be treated as present."""
    try:
        return bool(field_changes(record_path))
    except json.JSONDecodeError:
        return True
