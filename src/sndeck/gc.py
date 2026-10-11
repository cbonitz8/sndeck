"""Store cleanup. A pure planner sorts every copy into a class, and a thin applier
acts on the classes allowed for the call:

  C1 shipped clean           delete                 auto
  C2 shipped, already live   delete                 auto
  C3 shipped, real edit      move to _attic + diff  gc --apply
  C4 target set unknown      keep, warn             -
  C5 ref copy expired        delete                 gc --apply
  C6 attic day folder old    delete                 gc --apply --attic-older-than N"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .refresh import plan_refresh
from .snapshot import instance_fields, is_dirty, local_diff
from .store import ATTIC_DIR, EDIT, REF, Store, count_legacy

REF_TTL = timedelta(days=14)
SHIPPED = {"complete", "ignore"}
AUTO_CLASSES = {"C1", "C2"}


@dataclass(frozen=True)
class GcAction:
    cls: str
    path: Path
    label: str
    detail: str = ""
    table: str = ""
    name: str = ""
    sys_id: str = ""
    diff: str = ""


def _edit_actions(store, instance, set_states, fetch) -> list[GcAction]:
    out: list[GcAction] = []
    for c in store.iter_copies(instance, EDIT):
        label = f"{c.table}/{c.name}"
        if c.target_set is None:
            out.append(GcAction("C4", c.path, label, "edit copy has no target set"))
            continue
        state = set_states.get(c.target_set["sys_id"])
        if state is None:
            out.append(GcAction("C4", c.path, label,
                                f"target set '{c.target_set['name']}' not found on {instance}"))
            continue
        if state not in SHIPPED:
            continue
        if not is_dirty(c.path):
            out.append(GcAction("C1", c.path, label))
            continue
        live = fetch(c.table, c.sys_id)
        if live is None:
            out.append(GcAction("C3", c.path, label, "record no longer exists on the instance",
                                c.table, c.name, c.sys_id, local_diff(c.path, {})))
            continue
        fields = instance_fields(live)
        if plan_refresh(c.path, fields).local_diverged:
            out.append(GcAction("C3", c.path, label, f"set {state}; local differs from instance",
                                c.table, c.name, c.sys_id, local_diff(c.path, fields)))
        else:
            out.append(GcAction("C2", c.path, label))
    return out


def plan_gc(store: Store, instance: str, set_states: dict[str, str], fetch, now: datetime,
            *, attic_older_than_days: int | None = None) -> list[GcAction]:
    """Classify every copy for `instance`. An empty `set_states` means the state query
    told us nothing, so no set-based class is planned at all."""
    actions: list[GcAction] = []
    if set_states:
        actions.extend(_edit_actions(store, instance, set_states, fetch))
    for c in store.iter_copies(instance, REF):
        if now - datetime.fromisoformat(c.pulled_at) > REF_TTL:
            actions.append(GcAction("C5", c.path, f"{c.table}/{c.name}"))
    if attic_older_than_days is not None:
        attic = store.instance_dir(instance) / ATTIC_DIR
        if attic.is_dir():
            for d in sorted(p for p in attic.iterdir() if p.is_dir()):
                if (now.date() - date.fromisoformat(d.name)).days > attic_older_than_days:
                    actions.append(GcAction("C6", d, f"attic {d.name}"))
    return actions


def apply_gc(actions: list[GcAction], store: Store, instance: str, today: date, *,
             auto: bool) -> list[GcAction]:
    applied: list[GcAction] = []
    for a in actions:
        if a.cls == "C4" or (auto and a.cls not in AUTO_CLASSES):
            continue
        if a.cls == "C3":
            dest = store.attic_path(instance, today, a.table, a.name, a.sys_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(a.path), str(dest))
            (dest / "local.diff").write_text(a.diff, encoding="utf-8")
        else:
            shutil.rmtree(a.path)
        applied.append(a)
    return applied


def _plural(n: int, word: str, plural: str | None = None) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {plural}" if plural is not None else f"{n} {word}s"


def format_gc_report(actions: list[GcAction], applied: list[GcAction], instance: str) -> list[str]:
    lines: list[str] = []
    gone = [a for a in applied if a.cls in AUTO_CLASSES]
    if gone:
        lines.append(f"gc: removed {_plural(len(gone), 'copy', 'copies')} whose set shipped: "
                     + ", ".join(a.label for a in gone))
    waiting = [a for a in actions if a not in applied and a.cls in ("C3", "C5", "C6")]
    if waiting:
        parts = []
        for cls, word in (("C3", "attic"), ("C5", "expired ref"), ("C6", "old attic")):
            n = sum(1 for a in waiting if a.cls == cls)
            if n:
                parts.append(f"{word} {n}")
        lines.append(f"gc: {_plural(len(waiting), 'action')} "
                     f"{'waits' if len(waiting) == 1 else 'wait'} for `sndeck gc --apply` "
                     f"({', '.join(parts)})")
    for a in actions:
        if a.cls == "C4":
            lines.append(f"⚠ {a.label}: {a.detail} — kept")
    return lines


def _legacy_line(store: Store) -> list[str]:
    n = count_legacy(store.root)
    if not n:
        return []
    return [f"{_plural(n, 'legacy record folder')} under {store.root} — run `sndeck migrate`"]


def reconcile_and_report(client, store: Store) -> list[str]:
    """Best-effort auto cleanup for the client's instance. Never raises; returns report
    lines. The legacy pointer is reported even when the network part fails."""
    lines: list[str] = []
    try:
        from .updatesets import update_set_states
        instance = client.instance.name
        ids = sorted({c.target_set["sys_id"] for c in store.iter_copies(instance, EDIT)
                      if c.target_set is not None})
        metas = update_set_states(client, ids) if ids else {}
        states = {sid: m.state for sid, m in metas.items()}
        actions = plan_gc(store, instance, states,
                          lambda t, s: client.get_record(t, s, display_value="false"),
                          datetime.now(timezone.utc))
        applied = apply_gc(actions, store, instance, date.today(), auto=True)
        lines.extend(format_gc_report(actions, applied, instance))
    except Exception:
        pass
    try:
        lines.extend(_legacy_line(store))
    except Exception:
        pass
    return lines
