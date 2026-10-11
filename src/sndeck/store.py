"""The on-disk store: one record folder per (instance, table, sys_id, kind). The single
owner of where a record folder lives. `snapshot.py` owns what is inside it."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .scratch import folder_name
from .snapshot import RECORD_JSON

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
                meta = json.loads(rj.read_text()).get("_meta", {})
                if meta.get("instance") != instance or meta.get("kind") != kind:
                    continue
                out.append(Copy(instance, kind, meta["table"], meta["sys_id"], meta["name"],
                                rj.parent, meta.get("target_set"), meta["pulled_at"]))
        return out


def check_store_root(root, instance_names) -> str | None:
    """Why `root` cannot serve as a store root, or None when it can."""
    p = Path(root)
    if _SET_DIR.search(p.name):
        return f"{p} is an update-set folder, not a store root"
    if p.name in instance_names or p.name in (REF_DIR, ATTIC_DIR):
        return f"{p} is an instance folder inside a store; use its parent"
    if not p.is_dir():
        return None
    for child in sorted(c for c in p.iterdir() if c.is_dir()):
        if _SET_DIR.search(child.name):
            return (f"{p} is a legacy per-task root (it holds set folder {child.name}); "
                    f"point sndeck at ~/.sn-scratch and run `sndeck migrate`")
    if any(p.glob(f"*/{RECORD_JSON}")) or any(p.glob(f"*/*/{RECORD_JSON}")):
        return f"{p} holds record folders directly, so it is not a store root"
    return None


def count_legacy(root) -> int:
    """Record folders under `root` whose _meta has no instance — the old layout."""
    n = 0
    for rj in Path(root).rglob(RECORD_JSON):
        if "instance" not in json.loads(rj.read_text()).get("_meta", {}):
            n += 1
    return n
