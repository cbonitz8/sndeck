"""Build the scope → update set → table → file tree model. Pure data, no UI.
Content of files comes from the local scratch dir; set membership from the instance."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .auth import AuthExpiredError
from .registry import CODE_ARTIFACTS
from .scratch import set_workspaces
from .sync import is_dirty
from .updatesets import (
    UpdateSet, batch_members, current_update_set, current_user,
    update_set_entries, update_set_meta,
)

TABLE_LABELS = {
    "sys_script": "Business Rules",
    "sys_script_include": "Script Includes",
    "sys_script_client": "Client Scripts",
    "sys_ui_action": "UI Actions",
    "sys_ui_script": "UI Scripts",
    "sys_ws_operation": "Scripted REST",
    "sp_widget": "Widgets",
    "sp_header_footer": "Headers / Footers",
    "sys_ui_page": "UI Pages",
    "sys_properties": "System Properties",
    "oauth_entity": "OAuth",
}


def table_label(table: str) -> str:
    return TABLE_LABELS.get(table, table)


@dataclass(frozen=True)
class FileNode:
    table: str
    sys_id: str
    name: str
    in_current_set: bool
    tracked: bool          # in THIS set's server manifest
    local: bool
    dirty: bool
    record_path: Path | None


@dataclass(frozen=True)
class TableNode:
    table: str
    label: str
    files: list[FileNode]


@dataclass(frozen=True)
class SetNode:
    sys_id: str
    name: str
    state: str
    is_current: bool
    tables: list[TableNode]
    scope: str = "global"      # raw scope sys_id or 'global'
    is_base: bool = False
    members: list["SetNode"] = field(default_factory=list)


@dataclass(frozen=True)
class ScopeNode:
    name: str
    sets: list[SetNode]


@dataclass(frozen=True)
class TreeModel:
    scopes: list[ScopeNode]
    current_set: UpdateSet | None
    error: str | None = None


def _walk_set(scope: "ScopeNode", setn: "SetNode") -> Iterator[tuple["ScopeNode", "SetNode"]]:
    yield scope, setn
    for m in setn.members:
        yield from _walk_set(scope, m)


def iter_sets(model: "TreeModel | None") -> Iterator[tuple["ScopeNode", "SetNode"]]:
    """Every (scope, set) in the model — top-level sets AND their nested batch members —
    depth-first. The one traversal of the set hierarchy; find_set / owner_of_record and
    the model's own consumers walk through here instead of re-recursing by hand."""
    if model is None:
        return
    for scope in model.scopes:
        for setn in scope.sets:
            yield from _walk_set(scope, setn)


def find_set(model: "TreeModel | None", set_sys_id: str) -> tuple["SetNode", "ScopeNode"] | None:
    """The (SetNode, its ScopeNode) with this sys_id at ANY depth, or None. Lets a scoped
    batch member be resolved to its own scope, not a global fallback."""
    for scope, setn in iter_sets(model):
        if setn.sys_id == set_sys_id:
            return setn, scope
    return None


def owner_of_record(model: "TreeModel | None", table: str, sys_id: str) -> tuple[str, str] | None:
    """(raw scope sys_id, owning set sys_id) for the set that stages (table, sys_id), at
    any depth; None if unstaged. Drives push's per-record scope routing."""
    for _scope, setn in iter_sets(model):
        for tbl in setn.tables:
            for f in tbl.files:
                if f.table == table and f.sys_id == sys_id:
                    return (setn.scope, setn.sys_id)
    return None


def dirty_files(model: TreeModel) -> list[FileNode]:
    """Every staged (locally-edited, unpushed) record, deduped by (table, sys_id),
    in tree order. Feeds the staging pane and push-all — one definition of 'staged'."""
    seen: set[tuple[str, str]] = set()
    out: list[FileNode] = []
    for scope in model.scopes:
        for setn in scope.sets:
            for tbl in setn.tables:
                for f in tbl.files:
                    if not (f.dirty and f.record_path is not None):
                        continue
                    key = (f.table, f.sys_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(f)
    return out


def _build_set_node(meta, entries, ws_records, is_cur) -> SetNode:
    # ws_records: dict[(table, sys_id)] -> RecordRef  (records physically in THIS set's dir)
    manifest = {(e.table, e.sys_id): e for e in entries}
    keys = list(manifest) + [k for k in ws_records if k not in manifest]
    by_table: dict[str, list[FileNode]] = {}
    for (table, sys_id) in keys:
        e = manifest.get((table, sys_id))
        ref = ws_records.get((table, sys_id))
        name = e.name if e else (ref.name if ref else sys_id)
        by_table.setdefault(table, []).append(FileNode(
            table, sys_id, name,
            in_current_set=is_cur,
            tracked=e is not None,
            local=ref is not None,
            dirty=bool(ref) and is_dirty(ref.path),
            record_path=ref.path if ref else None,
        ))
    table_nodes = [TableNode(t, table_label(t), sorted(fs, key=lambda f: f.name.lower()))
                   for t, fs in by_table.items()]
    table_nodes.sort(key=lambda tn: (tn.table not in CODE_ARTIFACTS, tn.label.lower()))
    return SetNode(meta.sys_id, meta.name, meta.state, is_cur, table_nodes,
                   scope=meta.scope, is_base=False, members=[])


def build_tree(client, scratch_dir, tracked_ids: list[str]) -> TreeModel:
    try:
        user = current_user(client)
        if not user or not user.user_name:
            return TreeModel([], None, error="Could not resolve current ServiceNow user.")
        cur = current_update_set(client, user.user_name)
        set_ids: list[str] = []
        seen_ids: set[str] = set()
        for sid in ([cur.sys_id] if cur else []) + list(tracked_ids):
            if sid not in seen_ids:
                seen_ids.add(sid)
                set_ids.append(sid)

        ws_by_set: dict[str, dict] = {}
        for ws in set_workspaces(scratch_dir):   # one entry per on-disk set workspace
            d = ws_by_set.setdefault(ws.set_sys_id, {})
            for ref in ws.records:
                d[(ref.table, ref.sys_id)] = ref

        scope_map: dict[str, list[SetNode]] = {}
        rendered: set[str] = set()
        for sid in set_ids:
            if sid in rendered:
                continue
            members = batch_members(client, sid)   # base first; [self] if standalone
            base_meta = members[0] if members else update_set_meta(client, sid)
            if not base_meta:
                continue
            member_nodes: list[SetNode] = []
            for m in members:
                is_cur = cur is not None and m.sys_id == cur.sys_id
                node = _build_set_node(
                    m, update_set_entries(client, m.sys_id),
                    ws_by_set.get(m.sys_id, {}), is_cur)
                member_nodes.append(node)
                rendered.add(m.sys_id)
            if not member_nodes:
                # Defense-in-depth: batch_members guarantees a non-empty result for an
                # existing set, so this only fires if the set vanished mid-build. Skip
                # it rather than crash the whole launch on member_nodes[0].
                continue
            base_node = member_nodes[0]
            is_batch = len(member_nodes) > 1
            top = SetNode(base_node.sys_id, base_node.name, base_node.state,
                          base_node.is_current, base_node.tables,
                          scope=base_node.scope, is_base=is_batch,
                          members=member_nodes[1:] if is_batch else [])
            # scope display name for grouping: use meta.scope display via update_set_meta
            disp = update_set_meta(client, base_node.sys_id)
            scope_name = disp.scope if disp else "Global"
            scope_map.setdefault(scope_name, []).append(top)

        scopes = [ScopeNode(name, sets)
                  for name, sets in sorted(scope_map.items(), key=lambda kv: kv[0].lower())]
        return TreeModel(scopes, cur)
    except AuthExpiredError as e:
        return TreeModel([], None, error=str(e))
