"""Builders for on-disk record folders in the store layout and the legacy layout."""
import json
from pathlib import Path

HEX = "0123456789abcdef0123456789abcdef"


def sid(ch: str) -> str:
    """A 32-hex sys_id that differs from the others by its first character."""
    return ch + HEX[1:]


SET_A = {"sys_id": sid("a"), "name": "Set A"}


def _write(folder, meta, fields, script, dirty):
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps({"_meta": meta, **fields}))
    (folder / ".snapshot.json").write_text(json.dumps(fields))
    (folder / "script.js").write_text(script + (" // EDITED" if dirty else ""))
    return folder


def make_copy(root, instance, table, name, sys_id, *, kind="edit", target_set=SET_A,
              script="gs.info('x');", dirty=False,
              pulled_at="2026-10-01T00:00:00+00:00"):
    base = Path(root) / instance
    if kind == "ref":
        base = base / "_ref"
    meta = {"table": table, "sys_id": sys_id, "name": name, "instance": instance,
            "kind": kind, "pulled_at": pulled_at}
    if kind == "edit":
        meta["target_set"] = target_set
    fields = {"sys_id": sys_id, "name": name, "script": script}
    return _write(base / table / f"{name}__{sys_id}", meta, fields, script, dirty)


def make_legacy(parent, table, name, sys_id, *, script="gs.info('x');", dirty=False,
                pulled_at="2026-10-01T00:00:00+00:00"):
    meta = {"table": table, "sys_id": sys_id, "name": name, "pulled_at": pulled_at}
    fields = {"sys_id": sys_id, "name": name, "script": script}
    return _write(Path(parent) / table / f"{name}__{sys_id}", meta, fields, script, dirty)
