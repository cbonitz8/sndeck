"""One-time conversion of the legacy per-task, per-set layout into the store.

find_legacy reads the disk, plan_migration decides (pure, given lookups), and
apply_migration writes: copies first, then deletes the legacy folders, then removes the
set and table folders those copies leave empty. Files that are not record folders, and
the top-level legacy folders themselves, are never touched. A copy that would be dropped
but holds files a record folder does not (snapshot.expected_files) goes to the attic.
Rollback covers the copy phase only: no legacy folder is deleted until every copy has
been written."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .registry import CODE_ARTIFACTS
from .snapshot import (RECORD_JSON, _norm, field_file, is_dirty_or_unreadable, local_diff,
                       unexpected_files, write_meta_extra)
from .records import default_kind
from .store import EDIT, Store, legacy_meta

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
    extra_files: tuple[str, ...]


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


@dataclass(frozen=True)
class RootSummary:
    name: str
    store: int
    attic: int
    drop: int
    left: list[str]


def find_legacy(root, instance_names) -> list[LegacyCopy]:
    root = Path(root)
    out: list[LegacyCopy] = []
    for rj in sorted(root.rglob(RECORD_JSON)):
        meta = legacy_meta(rj, root, instance_names)
        if meta is None:
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
                              set_id, is_dirty_or_unreadable(folder), meta.get("pulled_at", ""),
                              tuple(unexpected_files(folder))))
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


def _plan_non_edit_group(instance: str, group: list[LegacyCopy], lookups) -> list[MigrationStep]:
    """A group resolved to an instance whose copies are references: migrate never creates
    an edit copy there, so a clean copy is dropped and an unpushed edit goes to the attic."""
    live = lookups.live_fields(instance, group[0].table, group[0].sys_id)
    steps: list[MigrationStep] = []
    for c in group:
        if c.dirty:
            steps.append(MigrationStep(
                "attic", c, instance, None,
                f"unpushed edit against {instance}; edit copies on {instance} need `pull --edit`",
                local_diff(c.path, {} if live is None else live)))
        else:
            steps.append(MigrationStep(
                "drop", c, instance, None,
                f"read copy from {instance}; re-pull with `sndeck pull --ref`"))
    return steps


def _plan_group(instance: str, group: list[LegacyCopy], lookups) -> list[MigrationStep]:
    if default_kind(instance) != EDIT:
        return _plan_non_edit_group(instance, group, lookups)

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
    return [_keep_user_files(s) for s in steps]


def _keep_user_files(s: MigrationStep) -> MigrationStep:
    """A drop would delete the copy's folder; one holding user files goes to the attic."""
    if s.action != "drop" or not s.copy.extra_files:
        return s
    return MigrationStep("attic", s.copy, s.instance, None,
                         f"{s.reason}, but it holds files sndeck did not write: "
                         + ", ".join(s.copy.extra_files))


def destination(s: MigrationStep, store: Store, today: date) -> Path | None:
    """Where a store or attic step writes its copy; None for a drop, which writes nothing."""
    c = s.copy
    if s.action == "store":
        return store.new_path(s.instance, EDIT, c.table, c.name, c.sys_id)
    if s.action == "attic":
        sub = c.path.parent.parent.relative_to(store.root)
        return store.attic_path(s.instance, today, c.table, c.name, c.sys_id) / sub
    return None


def destinations(steps: list[MigrationStep], store: Store,
                 today: date) -> list[tuple[MigrationStep, Path]]:
    """(step, destination) for every step that writes."""
    pairs = [(s, destination(s, store, today)) for s in steps]
    return [(s, d) for s, d in pairs if d is not None]


def destination_clashes(dests: list[tuple[MigrationStep, Path]], store: Store) -> list[Path]:
    """Destinations that already exist, are shared by two steps, or would give a record a
    second edit copy. Reads the disk only; apply refuses when this is non-empty."""
    seen: set[Path] = set()
    clashes: list[Path] = []
    for s, dest in dests:
        if dest in seen or dest.exists() or (
                s.action == "store" and store.locate(s.instance, s.copy.table, s.copy.sys_id, EDIT)):
            clashes.append(dest)
        seen.add(dest)
    return clashes


def _top(store_root: Path, c: LegacyCopy) -> Path:
    return store_root / c.path.relative_to(store_root).parts[0]


def _files_outside(top: Path, folders: list[Path]) -> list[str]:
    return sorted(str(p.relative_to(top)) for p in top.rglob("*")
                  if p.is_file() and not any(f == p or f in p.parents for f in folders))


def summarize_roots(steps: list[MigrationStep], store_root) -> list[RootSummary]:
    """Per top-level legacy folder: how many copies each action takes, and the files that
    are not part of any record folder (migrate leaves them in place)."""
    store_root = Path(store_root)
    by_top: dict[Path, list[MigrationStep]] = {}
    for s in steps:
        by_top.setdefault(_top(store_root, s.copy), []).append(s)
    out: list[RootSummary] = []
    for top, group in sorted(by_top.items()):
        counts = {a: sum(1 for s in group if s.action == a) for a in ("store", "attic", "drop")}
        out.append(RootSummary(top.name, counts["store"], counts["attic"], counts["drop"],
                               _files_outside(top, [s.copy.path for s in group])))
    return out


def _prune_emptied_dirs(folders: list[Path], tops: set[Path]) -> None:
    """Remove the parents of the deleted record folders, below their top-level legacy
    folder, that are now empty. Deepest first, so a set folder empties after its table
    folders."""
    parents: set[Path] = set()
    for folder in folders:
        for anc in folder.parents:
            if anc in tops:
                break
            parents.add(anc)
    for d in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def apply_migration(steps: list[MigrationStep], store: Store, today: date) -> MigrationReport:
    report = MigrationReport()
    dests = destinations(steps, store, today)
    clashes = destination_clashes(dests, store)
    if clashes:
        raise RuntimeError("destinations already exist or are shared; nothing was migrated: "
                           + ", ".join(str(c) for c in clashes))
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
    tops = {_top(store.root, s.copy) for s in steps}
    for s in steps:
        if s.copy.path.exists():
            shutil.rmtree(s.copy.path)
    _prune_emptied_dirs([s.copy.path for s in steps], tops)
    for top in sorted(tops):
        left = _files_outside(top, [])
        if left:
            report.leftovers[top.name] = left
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
