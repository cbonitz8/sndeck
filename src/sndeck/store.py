"""The on-disk store: one record folder per (instance, table, sys_id, kind). The single
owner of where a record folder lives. `snapshot.py` owns what is inside it."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .scratch import folder_name
from .settings import DEFAULT_STORE_ROOT
from .snapshot import RECORD_JSON, missing_key, read_meta_doc

EDIT, REF = "edit", "ref"
REF_DIR, ATTIC_DIR = "_ref", "_attic"
_SET_DIR = re.compile(r"__[0-9a-f]{32}$")


class StoreInvariantError(Exception):
    """Two folders claim the same (instance, table, sys_id, kind)."""


@dataclass(frozen=True)
class Copy:
    instance: str
    kind: str
    table: str
    sys_id: str
    name: str
    path: Path
    target_set: dict | None
    pulled_at: str


class Store:
    def __init__(self, root):
        self.root = Path(root)

    def instance_dir(self, instance: str) -> Path:
        return self.root / instance

    def kind_dir(self, instance: str, kind: str) -> Path:
        if kind == EDIT:
            return self.instance_dir(instance)
        if kind == REF:
            return self.instance_dir(instance) / REF_DIR
        raise ValueError(f"unknown copy kind {kind!r}")

    def new_path(self, instance: str, kind: str, table: str, name: str, sys_id: str) -> Path:
        return self.kind_dir(instance, kind) / table / folder_name(name, sys_id)

    def attic_path(self, instance: str | None, day: date, table: str, name: str,
                   sys_id: str) -> Path:
        base = self.root if instance is None else self.instance_dir(instance)
        return base / ATTIC_DIR / day.isoformat() / table / folder_name(name, sys_id)

    def locate(self, instance: str, table: str, sys_id: str, kind: str = EDIT) -> Path | None:
        tdir = self.kind_dir(instance, kind) / table
        if not tdir.is_dir():
            return None
        hits = sorted(p for p in tdir.glob(f"*__{sys_id}") if p.is_dir())
        if len(hits) > 1:
            raise StoreInvariantError(
                f"{len(hits)} folders for {instance}/{kind}/{table}/{sys_id}: "
                + ", ".join(str(h) for h in hits))
        return hits[0] if hits else None

    def iter_copies(self, instance: str, kind: str = EDIT) -> list[Copy]:
        base = self.kind_dir(instance, kind)
        if not base.is_dir():
            return []
        out: list[Copy] = []
        for tdir in sorted(p for p in base.iterdir() if p.is_dir()):
            if tdir.name.startswith("_"):
                continue
            for rj in sorted(tdir.glob(f"*/{RECORD_JSON}")):
                meta = read_meta_doc(rj.parent)
                if meta.get("instance") != instance or meta.get("kind") != kind:
                    continue
                for key in ("table", "sys_id", "name", "pulled_at"):
                    if key not in meta:
                        raise missing_key(rj.parent, key)
                out.append(Copy(instance, kind, meta["table"], meta["sys_id"], meta["name"],
                                rj.parent, meta.get("target_set"), meta["pulled_at"]))
        return out


def _default_root() -> Path:
    return Path(os.path.expanduser(DEFAULT_STORE_ROOT)).resolve()


def check_store_root(root, instance_names) -> str | None:
    """Why `root` cannot serve as a store root, or None when it can."""
    p = Path(root)
    if _SET_DIR.search(p.name):
        return f"{p} is an update-set folder, not a store root"
    if p.name in instance_names or p.name in (REF_DIR, ATTIC_DIR):
        return f"{p} is an instance folder inside a store; use its parent"
    default = _default_root()
    if p.is_dir():
        set_dirs = [c.name for c in sorted(p.iterdir())
                    if c.is_dir() and _SET_DIR.search(c.name)]
        if set_dirs:
            if p.resolve() == default:
                return (f"{p} holds update-set folders directly ({', '.join(set_dirs)}); "
                        f"move them into a subfolder of {p}, then run `sndeck migrate`")
            return (f"{p} is a legacy per-task root (it holds set folder {set_dirs[0]}); "
                    f"point sndeck at ~/.sn-scratch and run `sndeck migrate`")
    if default in p.resolve().parents:
        return (f"{p} is inside the default store root {default}; "
                f"use {default} itself as the store root")
    if not p.is_dir():
        return None
    if any(p.glob(f"*/{RECORD_JSON}")) or any(p.glob(f"*/*/{RECORD_JSON}")):
        return f"{p} holds record folders directly, so it is not a store root"
    return None


def legacy_meta(record_json: Path, root: Path, instance_names) -> dict | None:
    """The `_meta` of a legacy copy's record.json, or None when it is not one. A legacy
    copy sits outside every instance folder and the root `_attic`, and its `_meta` has
    `table` and `sys_id` but no `instance`. The one rule `find_legacy` and `count_legacy`
    share."""
    skip = [root / n for n in instance_names] + [root / ATTIC_DIR]
    if any(s in record_json.parents for s in skip):
        return None
    meta = read_meta_doc(record_json.parent)
    if "instance" in meta or "table" not in meta or "sys_id" not in meta:
        return None
    return meta


def count_legacy(root, instance_names) -> int:
    """How many legacy copies `find_legacy` would find under `root`."""
    root = Path(root)
    return sum(1 for rj in root.rglob(RECORD_JSON)
               if legacy_meta(rj, root, instance_names) is not None)
