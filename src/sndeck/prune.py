"""Prune local scratch: remove a set's workspace once it leaves 'in progress' and
all its records are clean; warn-and-keep on any dirty record. Also sweeps legacy
flat-root orphans. Pure planners (disk + given states, no network) + a thin
network+disk orchestrator. UI-free — shared by cli.py and app.py."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .scratch import set_workspaces, orphans, delete_record_folders
from .sync import is_dirty
from .updatesets import update_set_states

KEEP_STATE = "in progress"


@dataclass(frozen=True)
class PruneWarning:
    """A skip-and-keep decision, presentation-free. scope='set' → label is the set
    slug, state is its update-set state (None = not found on the instance), detail is
    the dirty record names.
    scope='orphan' → label is 'table/name', state is None, detail is ''."""
    scope: str            # "set" | "orphan"
    label: str
    state: str | None
    detail: str


def plan_set_prune(root, set_states: dict[str, str]) -> tuple[list[Path], list[PruneWarning]]:
    """For each on-disk set workspace whose set is known and not 'in progress': delete
    if every record is clean; else warn-and-keep. A set absent from set_states is
    warned about and kept. Returns (dirs, warnings)."""
    dels: list[Path] = []
    warns: list[PruneWarning] = []
    workspaces = set_workspaces(root)
    if workspaces and not set_states:
        # Guard: an on-disk set exists but the state query came back completely empty.
        # That's a signal we couldn't determine anyone's state (spurious/transient query
        # failure), not that every set is gone -- treating it that way would wipe all
        # clean scratch in one pass. Skip set-pruning entirely for this call.
        return dels, warns
    for ws in workspaces:
        state = set_states.get(ws.set_sys_id)
        if state == KEEP_STATE:
            continue
        if state is None:
            # Absent from a populated response is not proof the set was deleted (a
            # partial or fake response looks identical), so never reap on absence.
            warns.append(PruneWarning("set", ws.slug, None, ""))
            continue
        dirty = [r for r in ws.records if is_dirty(r.path)]
        if dirty:
            names = ", ".join(f"{r.table}/{r.name}" for r in dirty)
            warns.append(PruneWarning("set", ws.slug, state, names))
        else:
            dels.append(ws.dir)
    return dels, warns


def plan_orphan_prune(root) -> tuple[list[Path], list[PruneWarning]]:
    """Legacy flat-root records (no enclosing set dir). scratch.orphans returns only
    these, never records nested inside set workspaces. Clean → delete; dirty →
    warn-and-keep."""
    dels: list[Path] = []
    warns: list[PruneWarning] = []
    for ref in orphans(root):
        if is_dirty(ref.path):
            warns.append(PruneWarning("orphan", f"{ref.table}/{ref.name}", None, ""))
        else:
            dels.append(ref.path)
    return dels, warns


@dataclass(frozen=True)
class PruneResult:
    pruned_sets: list[Path]
    pruned_orphans: list[Path]
    warnings: list[PruneWarning]


def reconcile_scratch(client, root) -> PruneResult:
    """Network + disk orchestrator. One batched state query, then prune. Best-effort
    callers should go through reconcile_and_report, which owns the never-raise
    contract."""
    ids = [ws.set_sys_id for ws in set_workspaces(root)]
    metas = update_set_states(client, ids) if ids else {}
    states = {sid: m.state for sid, m in metas.items()}
    set_dels, set_warns = plan_set_prune(root, states)
    orph_dels, orph_warns = plan_orphan_prune(root)
    delete_record_folders(set_dels)
    delete_record_folders(orph_dels)
    return PruneResult(set_dels, orph_dels, set_warns + orph_warns)


def _render_missing(labels: list[str], instance: str) -> str:
    # Update sets are never deleted, only ignored, so a missing set means the reply
    # was wrong for this scratch dir, not that the set is gone.
    if len(labels) == 1:
        return (f"⚠ set '{labels[0]}' not found on {instance} — kept "
                f"(wrong instance, or no read access to the set)")
    return (f"⚠ {len(labels)} sets not found on {instance} — kept (wrong instance, "
            f"or no read access to the sets): {', '.join(labels)}")


def _render_warning(w: PruneWarning) -> str:
    """The only place the per-item '⚠ ...' strings are built."""
    if w.scope == "set":
        return (f"⚠ set '{w.label}' is {w.state} but has unpushed edits "
                f"({w.detail}) — not pruned")
    return f"⚠ orphan {w.label} has unpushed edits — not pruned"


def format_prune_report(result: PruneResult, instance: str) -> list[str]:
    lines: list[str] = []
    if result.pruned_sets:
        names = ", ".join(p.name.rsplit("__", 1)[0] for p in result.pruned_sets)
        lines.append(f"pruned {len(result.pruned_sets)} shipped set workspace(s): {names}")
    if result.pruned_orphans:
        lines.append(f"pruned {len(result.pruned_orphans)} orphaned record folder(s)")
    missing = [w.label for w in result.warnings if w.scope == "set" and w.state is None]
    if missing:
        lines.append(_render_missing(missing, instance))
    lines.extend(_render_warning(w) for w in result.warnings
                 if not (w.scope == "set" and w.state is None))
    return lines


def reconcile_and_report(client, root) -> list[str]:
    """Best-effort: never raises. Returns report lines ([] on any failure). Owns the
    single best-effort contract shared by cli._run_reconcile and
    app._reconcile_scratch_once."""
    try:
        return format_prune_report(reconcile_scratch(client, root), client.instance.name)
    except Exception:
        return []
