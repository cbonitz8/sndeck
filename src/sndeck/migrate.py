"""One-time conversion of the legacy per-task, per-set layout into the store.

find_legacy reads the disk, plan_migration decides (pure, given lookups), and
apply_migration writes: copies first, then deletes the legacy folders, then removes
directories left empty. Files that are not record folders are never touched."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .registry import CODE_ARTIFACTS
from .snapshot import RECORD_JSON, _norm, field_file, is_dirty_or_unreadable, local_diff, write_meta_extra
from .store import ATTIC_DIR, EDIT, Store

_SET_DIR = re.compile(r"__(?P<sysid>[0-9a-f]{32})$")
IN_PROGRESS = "in progress"


@dataclass(frozen=True)
class LegacyCopy:
    path: Path
    table: str
    sys_id: str
    name: str
    set_sys_id: str | None
    dirty: bool
    pulled_at: str


@dataclass(frozen=True)
class MigrationStep:
    action: str                 # "store" | "attic" | "drop"
    copy: LegacyCopy
    instance: str | None
    target_set: dict | None
    reason: str
    diff: str = ""


@dataclass
class MigrationReport:
    stored: int = 0
    atticked: int = 0
    dropped: int = 0
    leftovers: dict[str, list[str]] = field(default_factory=dict)


def find_legacy(root, instance_names) -> list[LegacyCopy]:
    root = Path(root)
    skip = [root / n for n in instance_names] + [root / ATTIC_DIR]
    out: list[LegacyCopy] = []
    for rj in sorted(root.rglob(RECORD_JSON)):
        if any(s in rj.parents for s in skip):
            continue
        meta = json.loads(rj.read_text())["_meta"]
        if "instance" in meta or "table" not in meta or "sys_id" not in meta:
            continue
        folder = rj.parent
        set_id = None
        for anc in folder.parents:
            if anc == root:
                break
            m = _SET_DIR.search(anc.name)
            if m:
                set_id = m.group("sysid")
                break
        out.append(LegacyCopy(folder, meta["table"], meta["sys_id"],
                              meta.get("name", folder.name.rsplit("__", 1)[0]),
                              set_id, is_dirty_or_unreadable(folder), meta.get("pulled_at", "")))
    return out


def _resolve_instance(c: LegacyCopy, lookups) -> tuple[str | None, str]:
    if c.set_sys_id is not None:
        homes = lookups.set_homes(c.set_sys_id)
        if homes:
            return homes[0], f"set found on {homes[0]}"
    homes = lookups.record_homes(c.table, c.sys_id)
    if len(homes) == 1:
        return homes[0], f"record found on {homes[0]}"
    if not homes:
        return None, "no instance holds this record or its set"
    return None, f"record found on {' and '.join(homes)}"


def _bodies(c: LegacyCopy) -> tuple[str, ...]:
    art = CODE_ARTIFACTS.get(c.table)
    if not art:
        return ()
    out = []
    for f in art.script_fields:
        fp = c.path / field_file(f)
        out.append(_norm(fp.read_text(encoding="utf-8")) if fp.exists() else "")
    return tuple(out)


def _plan_group(instance: str, group: list[LegacyCopy], lookups) -> list[MigrationStep]:
    def meta(c):
        return lookups.set_meta(instance, c.set_sys_id) if c.set_sys_id else None

    def in_progress(c):
        m = meta(c)
        return m is not None and m[0] == IN_PROGRESS

    def target_of(c):
        state_name = meta(c)
        return {"sys_id": c.set_sys_id, "name": state_name[1]}

    newest = sorted(group, key=lambda c: c.pulled_at, reverse=True)
    dirty = [c for c in newest if c.dirty]
    steps: list[MigrationStep] = []

    if not dirty:
        live_sets = [c for c in newest if in_progress(c)]
        if not live_sets:
            return [MigrationStep("drop", c, instance, None,
                                  "clean, and its set is shipped or unknown") for c in group]
        keeper = live_sets[0]
        target = target_of(keeper)
        cap = lookups.newest_capture_set(instance, keeper.table, keeper.sys_id)
        if cap is not None:
            cap_meta = lookups.set_meta(instance, cap)
            if cap_meta is not None and cap_meta[0] == IN_PROGRESS:
                target = {"sys_id": cap, "name": cap_meta[1]}
        for c in group:
            if c is keeper:
                steps.append(MigrationStep("store", c, instance, target, "newest clean copy"))
            else:
                steps.append(MigrationStep("drop", c, instance, None, "duplicate of kept copy"))
        return steps

    if len({_bodies(c) for c in dirty}) == 1:
        rep = next((c for c in dirty if in_progress(c)), dirty[0])
        for c in group:
            if c is not rep:
                steps.append(MigrationStep("drop", c, instance, None, "duplicate of kept copy"))
        if in_progress(rep):
            steps.append(MigrationStep("store", rep, instance, target_of(rep), "unpushed edit"))
            return steps
        live = lookups.live_fields(instance, rep.table, rep.sys_id)
        if live is None:
            steps.append(MigrationStep("attic", rep, instance, None,
                                       "record no longer exists on the instance",
                                       local_diff(rep.path, {})))
        elif local_diff(rep.path, live) == "":
            steps.append(MigrationStep("drop", rep, instance, None, "edit already live"))
        else:
            steps.append(MigrationStep("attic", rep, instance, None,
                                       "set shipped; local differs from instance",
                                       local_diff(rep.path, live)))
        return steps

    live = lookups.live_fields(instance, group[0].table, group[0].sys_id)
    for c in group:
        if c.dirty:
            steps.append(MigrationStep("attic", c, instance, None,
                                       f"{len(dirty)} conflicting unpushed edits",
                                       local_diff(c.path, {} if live is None else live)))
        else:
            steps.append(MigrationStep("drop", c, instance, None, "clean beside conflicting edits"))
    return steps


def plan_migration(copies: list[LegacyCopy], lookups) -> list[MigrationStep]:
    steps: list[MigrationStep] = []
    groups: dict[tuple[str, str, str], list[LegacyCopy]] = {}
    for c in copies:
        instance, why = _resolve_instance(c, lookups)
        if instance is None:
            steps.append(MigrationStep("attic", c, None, None, why))
            continue
        groups.setdefault((instance, c.table, c.sys_id), []).append(c)
    for (instance, _t, _s), group in sorted(groups.items()):
        steps.extend(_plan_group(instance, group, lookups))
    return steps


def _prune_empty_dirs(top: Path) -> None:
    for dirpath, _dirs, _files in os.walk(top, topdown=False):
        p = Path(dirpath)
        if not any(p.iterdir()):
            p.rmdir()


def apply_migration(steps: list[MigrationStep], store: Store, today: date) -> MigrationReport:
    report = MigrationReport()
    dests: list[tuple[MigrationStep, Path]] = []
    for s in steps:
        c = s.copy
        if s.action == "store":
            dests.append((s, store.new_path(s.instance, EDIT, c.table, c.name, c.sys_id)))
        elif s.action == "attic":
            sub = c.path.parent.parent.relative_to(store.root)
            dests.append((s, store.attic_path(s.instance, today, c.table, c.name, c.sys_id) / sub))
    seen: set[Path] = set()
    clashes = []
    for s, dest in dests:
        if dest in seen or dest.exists() or (
                s.action == "store" and store.locate(s.instance, s.copy.table, s.copy.sys_id, EDIT)):
            clashes.append(str(dest))
        seen.add(dest)
    if clashes:
        raise RuntimeError("destinations already exist or are shared; nothing was migrated: "
                           + ", ".join(clashes))
    created: list[Path] = []
    try:
        for s, dest in dests:
            c = s.copy
            dest.parent.mkdir(parents=True, exist_ok=True)
            created.append(dest)
            shutil.copytree(c.path, dest)
            if s.action == "store":
                write_meta_extra(dest, instance=s.instance, kind=EDIT, target_set=s.target_set)
                report.stored += 1
            else:
                (dest / "local.diff").write_text(s.diff, encoding="utf-8")
                (dest / "reason.txt").write_text(s.reason + "\n", encoding="utf-8")
                report.atticked += 1
    except BaseException:
        for dest in created:
            shutil.rmtree(dest, ignore_errors=True)
        raise
    report.dropped = sum(1 for s in steps if s.action == "drop")
    tops = sorted({store.root / s.copy.path.relative_to(store.root).parts[0] for s in steps})
    for s in steps:
        shutil.rmtree(s.copy.path)
    for top in tops:
        _prune_empty_dirs(top)
        if top.exists():
            report.leftovers[top.name] = sorted(
                str(p.relative_to(top)) for p in top.rglob("*") if p.is_file())
    return report


class ClientLookups:
    """Lookups over real TableClients, tried in list order (dev first). Read-only.
    Every GET is memoized by (instance, table, sys_id), so a record or set shared by
    many legacy copies costs one request per instance."""

    def __init__(self, clients):
        self._clients = list(clients)
        self._by_name = {c.instance.name: c for c in self._clients}
        self._cache: dict[tuple[str, str, str], dict | None] = {}

    def _get(self, instance, table, sys_id):
        key = (instance, table, sys_id)
        if key not in self._cache:
            self._cache[key] = self._by_name[instance].get_record(
                table, sys_id, display_value="false")
        return self._cache[key]

    def set_homes(self, set_sys_id):
        return [c.instance.name for c in self._clients
                if self._get(c.instance.name, "sys_update_set", set_sys_id) is not None]

    def record_homes(self, table, sys_id):
        return [c.instance.name for c in self._clients
                if self._get(c.instance.name, table, sys_id) is not None]

    def set_meta(self, instance, set_sys_id):
        rec = self._get(instance, "sys_update_set", set_sys_id)
        return None if rec is None else (rec["state"], rec["name"])

    def newest_capture_set(self, instance, table, sys_id):
        from .updatesets import capture_for_record
        cap = capture_for_record(self._by_name[instance], table, sys_id)
        return None if cap is None else cap.set_id

    def live_fields(self, instance, table, sys_id):
        from .snapshot import instance_fields
        rec = self._get(instance, table, sys_id)
        return None if rec is None else instance_fields(rec)
