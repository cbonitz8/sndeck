"""On-disk scratch layout: the single owner of folder-naming and enumeration
rules for pulled records and per-set workspaces."""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

RECORD_JSON = "record.json"
SNAPSHOT_JSON = ".snapshot.json"

_SANITIZE = re.compile(r"[^A-Za-z0-9_.\- ]")
_SET_DIR = re.compile(r"__(?P<sysid>[0-9a-f]{32})$")


@dataclass(frozen=True)
class RecordRef:
    table: str
    sys_id: str
    name: str
    path: Path


@dataclass(frozen=True)
class WorkspaceRef:
    set_sys_id: str
    ref: RecordRef


def folder_name(name: str, sys_id: str) -> str:
    clean = _SANITIZE.sub("_", name).strip() or "unnamed"
    return f"{clean}__{sys_id}"


_folder_name = folder_name


def set_dir_name(name: str, sys_id: str) -> str:
    """Workspace dir name for a set: sanitized name + set sys_id (same rule as record folders)."""
    return folder_name(name, sys_id)


def set_workspace(root, sys_id: str, name: str) -> Path:
    """The per-set workspace dir under the scratch root."""
    return Path(root) / set_dir_name(name, sys_id)


def scan_scratch(scratch_dir) -> list[RecordRef]:
    refs: list[RecordRef] = []
    for rj in Path(scratch_dir).glob(f"*/*/{RECORD_JSON}"):
        meta = json.loads(rj.read_text()).get("_meta", {})
        if "table" in meta and "sys_id" in meta:
            name = meta.get("name") or rj.parent.name.rsplit("__", 1)[0]
            refs.append(RecordRef(meta["table"], meta["sys_id"], name, rj.parent))
    return sorted(refs, key=lambda r: (r.table, r.name))


def scan_workspace(root) -> list[WorkspaceRef]:
    """Scan a scratch ROOT of per-set workspace dirs. Each top-level dir named
    '<slug>__<32hex>' is a set workspace; records inside are tagged with that set
    sys_id. Legacy/flat dirs (no __<32hex> suffix) are ignored."""
    out: list[WorkspaceRef] = []
    for setdir in sorted(p for p in Path(root).glob("*") if p.is_dir()):
        m = _SET_DIR.search(setdir.name)
        if not m:
            continue
        sid = m.group("sysid")
        for ref in scan_scratch(setdir):
            out.append(WorkspaceRef(sid, ref))
    return out


def delete_record_folders(paths) -> int:
    """Best-effort recursive delete of record folders. Returns count removed."""
    n = 0
    for p in paths:
        try:
            shutil.rmtree(p)
            n += 1
        except OSError:
            pass
    return n


@dataclass(frozen=True)
class SetWorkspace:
    set_sys_id: str
    slug: str
    dir: Path
    records: list


def set_workspaces(root) -> list["SetWorkspace"]:
    """One SetWorkspace per on-disk '<slug>__<32hex>' set dir, with its records."""
    out: list[SetWorkspace] = []
    for p in sorted(x for x in Path(root).glob("*") if x.is_dir()):
        m = _SET_DIR.search(p.name)
        if not m:
            continue
        out.append(SetWorkspace(m.group("sysid"), p.name.rsplit("__", 1)[0], p, scan_scratch(p)))
    return out


def orphans(root) -> list:
    """Legacy flat-root records (no enclosing set dir)."""
    return scan_scratch(root)
