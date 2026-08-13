"""Textual browser over the sndeck core: scope→set→table→file tree + code preview."""
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static, TextArea, Tree

from .auth import AuthExpiredError
from .deeplinks import instance_url_for
from .preview import Preview, PreviewField, build_preview, read_field
from .prune import reconcile_and_report
from .records import pull_record, pull_set, scan_scratch, scan_workspace, set_workspace, delete_record_folders, dirty_files_from_disk
from .registry import CODE_ARTIFACTS
from .state import load_state
from .sync import is_dirty
from .theme import LATTE, MACCHIATO, THEMES, next_theme
from .tree import (
    FileNode, ScopeNode, SetNode, TableNode, TreeModel, build_tree,
)
from .updatesets import (
    list_update_sets, resolve_current_set, switch_current_set, update_set_meta,
    update_set_states,
)
from .watcher import ScratchChanged, watch_scratch
from . import state as _state
from .widgets import SplitHandle


class SetPickerScreen(ModalScreen):
    BINDINGS = [
        ("escape", "close", "Close"),
        ("space", "toggle", "Pin/unpin"),
        ("n", "next", "Next page"),
        ("b", "prev", "Prev page"),
    ]

    def __init__(self, client):
        super().__init__()
        self._client = client
        self._offset = 0
        self._limit = 25
        self._rows: list = []
        self._new_pins: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static("Update sets — space to pin, n/b to page, esc to close",
                         id="picker-title")
            yield DataTable(id="picker-table", cursor_type="row")
            yield Static(id="picker-page")

    def on_mount(self) -> None:
        self.query_one("#picker-table", DataTable).add_columns("PIN", "NAME", "STATE")
        self._load()

    def _load(self) -> None:
        self._rows = list_update_sets(self._client, offset=self._offset, limit=self._limit)
        tracked = set(_state.load_state().tracked_sets)
        tbl = self.query_one("#picker-table", DataTable)
        tbl.clear()
        for m in self._rows:
            pin = "📌" if m.sys_id in tracked else " "
            tbl.add_row(pin, m.name, m.state, key=m.sys_id)
        page = self._offset // self._limit + 1
        self.query_one("#picker-page", Static).update(f"page {page}")

    def action_toggle(self) -> None:
        tbl = self.query_one("#picker-table", DataTable)
        if tbl.row_count == 0:
            return
        m = self._rows[tbl.cursor_row]
        sys_id = m.sys_id
        tracked = set(_state.load_state().tracked_sets)
        if sys_id in tracked:
            _state.unpin(sys_id)
            self._new_pins.discard(sys_id)
        else:
            _state.pin(sys_id, name=m.name)
            self._new_pins.add(sys_id)
        self._load()

    def action_next(self) -> None:
        self._offset += self._limit
        self._load()

    def action_prev(self) -> None:
        self._offset = max(0, self._offset - self._limit)
        self._load()

    def action_close(self) -> None:
        self.dismiss(sorted(self._new_pins))


class LegendScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("question_mark", "dismiss", "Close"),
                ("q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="legend-box"):
            yield Static("Legend", id="legend-title")
            yield Static("◌   local only — not in set yet (staged via g)", classes="legend-warn")
            yield Static("◇   tracked · not pulled", classes="legend-warn")
            yield Static("✓   tracked · local", classes="legend-ok")
            yield Static("✎   local edits not pushed", classes="legend-update")
            yield Static("enter open/switch · P push all · s pinned sets · a add · o browser",
                         classes="legend-keys")
            yield Static("p pull current set · g get record (stage into current set) · r refresh · t theme",
                         classes="legend-keys")
            yield Static("[ / ] prev/next field tab · ? legend", classes="legend-keys")
            yield Static("esc / q to close", classes="legend-dismiss")


class SwitchConfirmScreen(ModalScreen):
    BINDINGS = [("y", "confirm", "Confirm"), ("escape", "cancel", "Cancel")]

    def __init__(self, set_name: str):
        super().__init__()
        self._name = set_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(f"Switch current update set to '{self._name}'?", classes="confirm-title")
            yield Static("This WRITES to ServiceNow (your session preference).", classes="confirm-warn")
            yield Static("y = confirm · esc = cancel", classes="confirm-keys")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PushAllConfirmScreen(ModalScreen):
    """Single combined confirm for pushing every staged record. The clobber-guard
    runs per record at apply time (§7d) — blocked records are skipped and reported,
    so this screen only needs the yes/no gate, not per-record drift detail."""
    BINDINGS = [("y", "confirm", "Confirm"), ("escape", "cancel", "Cancel")]

    def __init__(self, records: list[tuple[str, str]]):
        super().__init__()
        self._records = records  # [(name, table), ...]

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(f"Push {len(self._records)} record(s) → ServiceNow",
                         classes="confirm-title")
            for name, table in self._records[:12]:
                yield Static(f"• {name} ({table})", classes="confirm-title")
            if len(self._records) > 12:
                yield Static(f"…and {len(self._records) - 12} more", classes="confirm-keys")
            yield Static("This WRITES to ServiceNow. y = confirm · esc = cancel",
                         classes="confirm-warn")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DeleteFilesConfirmScreen(ModalScreen):
    BINDINGS = [("y", "confirm", "Confirm"), ("escape", "cancel", "Cancel")]

    def __init__(self, count: int):
        super().__init__()
        self._count = count

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(f"{self._count} local file(s) have unsaved edits.",
                         classes="confirm-title")
            yield Static("Delete them anyway? (tracking is already removed)",
                         classes="confirm-warn")
            yield Static("y = delete · esc = keep files", classes="confirm-keys")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class GetRecordScreen(ModalScreen):
    """Collect (table, sys_id) from the user then dismiss with the pair."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="get-record-box"):
            yield Static("Get record — stage into current set", id="get-record-title")
            yield Input(placeholder="table  (e.g. sys_script_include)", id="get-record-table")
            yield Input(placeholder="sys_id  (32-char hex)", id="get-record-sys-id")
            yield Static("enter to confirm · esc to cancel", classes="confirm-keys")

    def on_mount(self) -> None:
        self.query_one("#get-record-table", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        table = self.query_one("#get-record-table", Input).value.strip()
        sys_id = self.query_one("#get-record-sys-id", Input).value.strip()
        if not table or not sys_id:
            self.notify("Both table and sys_id are required.", severity="warning")
            return
        self.dismiss((table, sys_id))


_STATE_BADGE = {"in progress": "● in progress", "complete": "✓ complete", "ignore": "⊘ ignored"}



def _pinned_badge(meta) -> str:
    if meta is None:
        return "⚠ gone"
    return _STATE_BADGE.get((meta.state or "").lower(), meta.state or "? unknown")


class PinnedSetsScreen(ModalScreen):
    """Manage pinned sets: switch to an in-progress one (enter) or unpin any
    (space/x), regardless of instance state. Reads state.tracked_sets directly
    so completed/ignored/gone pins always appear."""
    BINDINGS = [
        ("escape", "close", "Close"),
        ("space", "unpin", "Unpin"),
        ("x", "unpin", "Unpin"),
    ]
    # NB: switching is wired via on_data_table_row_selected (Enter), NOT a key
    # binding — a focused DataTable consumes Enter and emits RowSelected.

    def __init__(self, client, scratch_dir):
        super().__init__()
        self._client = client
        self._scratch = scratch_dir
        self._rows: list = []          # [(sys_id, meta_or_None)]
        self._states_ok = True

    def compose(self) -> ComposeResult:
        with Vertical(id="pinned-box"):
            yield Static("Pinned sets — enter switch · space/x unpin · esc close",
                         id="pinned-title")
            yield DataTable(id="pinned-table", cursor_type="row")
            yield Static(id="pinned-hint")

    def on_mount(self) -> None:
        self.query_one("#pinned-table", DataTable).add_columns("STATE", "NAME", "SCOPE")
        self._load()

    def _load(self) -> None:
        s = _state.load_state()
        tracked = s.tracked_sets
        try:
            states = update_set_states(self._client, tracked)
            self._states_ok = True
            names = {sid: m.name for sid, m in states.items()}
            if names:
                _state.remember_pin_names(names)
                s = _state.load_state()
        except Exception:
            states, self._states_ok = {}, False
        self._rows = [(sid, states.get(sid)) for sid in tracked]
        tbl = self.query_one("#pinned-table", DataTable)
        tbl.clear()
        for sid, meta in self._rows:
            badge = "? unknown" if not self._states_ok else _pinned_badge(meta)
            name = meta.name if meta else s.pin_names.get(sid, sid)
            scope = meta.scope if meta else "—"
            tbl.add_row(badge, name, scope, key=sid)
        self.query_one("#pinned-hint", Static).update(
            "no pinned sets — press a to add" if not tracked else "")

    def _selected(self):
        tbl = self.query_one("#pinned-table", DataTable)
        if tbl.row_count == 0:
            return None
        return self._rows[tbl.cursor_row]

    def on_data_table_row_selected(self, event) -> None:
        # fires on Enter (or click) on a row
        sel = self._selected()
        if not sel:
            return
        sid, meta = sel
        if not self._states_ok or meta is None:
            self.app.notify("Can't switch — set state unknown or no longer exists.",
                            severity="warning")
            return
        if (meta.state or "").lower() != "in progress":
            self.app.notify(f"'{meta.name}' is {meta.state} — can't switch.",
                            severity="warning")
            return
        self.dismiss(("switch", sid, meta.name))

    def action_unpin(self) -> None:
        sel = self._selected()
        if not sel:
            return
        sid, meta = sel
        _state.unpin(sid)                       # tracking removed either way
        # Find the per-set workspace dir by sys_id suffix (robust to server-side rename).
        set_dir = next((p for p in Path(self._scratch).glob(f"*__{sid}") if p.is_dir()), None)
        if set_dir is None:
            self._load()
            return
        paths = [ref.path for ref in scan_scratch(set_dir)]
        dirty = [p for p in paths if is_dirty(p)]
        if dirty:
            def after(confirmed: bool) -> None:
                if confirmed:
                    delete_record_folders([set_dir])
                self._load()
            self.app.push_screen(DeleteFilesConfirmScreen(len(dirty)), after)
        else:
            delete_record_folders([set_dir])
            self._load()

    def action_close(self) -> None:
        self.dismiss(None)


class SndeckApp(App):
    CSS_PATH = "sndeck.tcss"
    BINDINGS = [
        ("a", "add_set", "Add set"),
        ("g", "get_record", "Get record"),
        ("o", "open_browser", "Browser"),
        ("p", "pull_set", "Pull"),
        ("P", "push", "Push all"),
        ("r", "refresh", "Refresh"),
        ("s", "switch_set", "Switch set"),
        ("t", "toggle_theme", "Theme"),
        ("[", "prev_field", "Prev field"),
        ("]", "next_field", "Next field"),
        ("?", "legend", "Legend"),
        ("q", "quit", "Quit"),
    ]

    def get_theme_variable_defaults(self) -> dict[str, str]:
        from .theme import THEMES, MACCHIATO
        by = {t.name: t for t in THEMES}
        th = by.get(getattr(self, "_initial_theme", MACCHIATO)) or by[MACCHIATO]
        return dict(th.variables)

    def __init__(self, client, scratch_dir, theme_name: str = MACCHIATO):
        super().__init__()
        self._client = client
        self._scratch = Path(scratch_dir)
        self._initial_theme = theme_name
        self._last_model: TreeModel | None = None
        self._node_files: dict = {}  # tree node id -> FileNode
        self._preview: Preview | None = None
        self._preview_key: tuple | None = None  # (table, sys_id) of currently-shown record
        self._selected_field_key: str | None = None  # currently-viewed field key within preview
        self._ratio: float = 0.4
        self._staging_files: dict = {}  # staging row key -> FileNode

    def compose(self) -> ComposeResult:
        with Horizontal(id="info-bar"):
            yield Static(id="info-main")
            yield Static("[READ-ONLY]", id="info-app")
        with Vertical(id="split"):
            with Horizontal(id="top"):
                yield Tree("scopes", id="tree")
                with Vertical(id="staging"):
                    yield Static("No local changes", id="staging-header")
                    yield DataTable(id="staging-table", cursor_type="row")
            yield SplitHandle(id="divider")
            with Vertical(id="preview"):
                yield Static(id="preview-header")
                yield Horizontal(id="preview-tabs")
                yield TextArea("", id="preview-body", read_only=True, show_line_numbers=True)
        yield Footer()

    def on_mount(self) -> None:
        for th in THEMES:
            self.register_theme(th)
        self.theme = self._initial_theme
        tree = self.query_one("#tree", Tree)
        tree.show_root = False
        tree.guide_depth = 3
        self.query_one("#staging-table", DataTable).add_columns("TABLE", "NAME")
        self._apply_ratio(load_state().split_ratio)
        self._reconcile_scratch_once()
        self.action_refresh()
        self._start_watcher()

    def _reconcile_scratch_once(self) -> None:
        """Best-effort scratch prune on startup; surface report via log, never raise."""
        for line in reconcile_and_report(self._client, self._scratch):
            self.log(line)

    def _start_watcher(self) -> None:
        t = threading.Thread(
            target=watch_scratch,
            args=(self, str(self._scratch)),
            kwargs={"stop": lambda: not self.is_running},
            daemon=True,
            name="sndeck-watcher",
        )
        t.start()

    async def on_scratch_changed(self, event: ScratchChanged) -> None:
        # Reload preview and recompute local badges from disk — no network call.
        node = self.query_one("#tree", Tree).cursor_node
        f = self._node_files.get(node.id) if node else None
        if f is not None:
            await self._show_preview(f)
        self._refresh_local()

    def _refresh_local(self) -> None:
        """Update file-leaf badges in place from disk — no tree rebuild, no preview touch."""
        self._render_staging()          # disk-sourced; safe with any/no model
        model = self._last_model
        if model is None or model.error:
            return

        local_map = {(w.set_sys_id, w.ref.table, w.ref.sys_id): w.ref
                     for w in scan_workspace(self._scratch)}

        new_scopes = []
        for scope in model.scopes:
            new_sets = []
            # NOTE: nested batch-member leaves (setn.members) are NOT live-refreshed here
            # on ScratchChanged ticks — only the top-level sets in scope.sets are walked.
            # Member leaves are refreshed only on a full build_tree rebuild (known follow-up).
            for setn in scope.sets:
                new_tables = []
                for tbl in setn.tables:
                    new_files = []
                    for f in tbl.files:
                        ref = local_map.get((setn.sys_id, f.table, f.sys_id))
                        new_files.append(FileNode(
                            f.table, f.sys_id, f.name,
                            in_current_set=f.in_current_set,
                            tracked=f.tracked,
                            local=ref is not None,
                            dirty=bool(ref) and is_dirty(ref.path),
                            record_path=ref.path if ref else None,
                        ))
                    new_tables.append(TableNode(tbl.table, tbl.label, new_files))
                new_sets.append(SetNode(setn.sys_id, setn.name, setn.state,
                                        setn.is_current, new_tables,
                                        scope=setn.scope, is_base=setn.is_base,
                                        members=setn.members))
            new_scopes.append(ScopeNode(scope.name, new_sets))

        new_model = TreeModel(new_scopes, model.current_set)
        self._last_model = new_model

        fresh = {
            (f.table, f.sys_id): f
            for scope in new_model.scopes
            for setn in scope.sets
            for tbl in setn.tables
            for f in tbl.files
        }

        tree = self.query_one("#tree", Tree)
        for node in self._iter_tree_nodes(tree.root):
            old = self._node_files.get(node.id)
            if old is None:
                continue
            nf = fresh.get((old.table, old.sys_id))
            if nf is not None:
                self._node_files[node.id] = nf
                node.set_label(self._styled_file_label(nf))

    def _render_staging(self) -> None:
        """Rebuild the staging pane from disk so newly pulled/added records appear live
        on a watcher tick. Preserves the highlighted row across rebuilds."""
        files = dirty_files_from_disk(self._scratch)
        try:
            header = self.query_one("#staging-header", Static)
            tbl = self.query_one("#staging-table", DataTable)
        except Exception:
            return
        header.update(f"✎ {len(files)} changed" if files else "No local changes")

        prev_key = None
        try:
            if tbl.row_count and tbl.cursor_row is not None:
                prev_key = tbl.coordinate_to_cell_key((tbl.cursor_row, 0)).row_key.value
        except Exception:
            prev_key = None

        self._staging_files = {}
        tbl.clear()
        restore_row = None
        for i, f in enumerate(files):
            key = f"{f.table}:{f.sys_id}"
            self._staging_files[key] = f
            tbl.add_row(f.table, f.name, key=key)
            if key == prev_key:
                restore_row = i
        if restore_row is not None:
            try:
                tbl.move_cursor(row=restore_row)
            except Exception:
                pass

    async def on_data_table_row_highlighted(self, event) -> None:
        # Only the staging table drives the preview, and only on genuine user
        # navigation (has_focus) — not the programmatic rebuild on every watcher tick.
        table = getattr(event, "data_table", None)
        if table is None or table.id != "staging-table" or not table.has_focus:
            return
        key = event.row_key.value if getattr(event, "row_key", None) else None
        f = self._staging_files.get(key)
        if f is not None:
            await self._show_preview(f)

    @staticmethod
    def _iter_tree_nodes(node):
        """Depth-first iteration over all tree nodes."""
        yield node
        for child in node.children:
            yield from SndeckApp._iter_tree_nodes(child)

    def _apply_ratio(self, ratio: float, persist: bool = False) -> None:
        self._ratio = max(0.15, min(0.85, ratio))
        self.query_one("#top").styles.height = f"{self._ratio * 100:.0f}%"
        if persist:
            from .state import set_split_ratio
            set_split_ratio(round(self._ratio, 2))

    def _persist_ratio(self) -> None:
        from .state import set_split_ratio
        set_split_ratio(round(getattr(self, "_ratio", 0.4), 2))

    def _set_loading(self, on: bool) -> None:
        try:
            self.query_one("#tree", Tree).loading = on
        except Exception:
            pass

    def _badges(self, f: FileNode) -> str:
        if not f.tracked and f.local:
            state = "◌"                       # local only — not in set yet
        elif f.tracked and not f.local:
            state = "◇"                       # tracked, not pulled
        else:
            state = "✓"                       # tracked · local
        return f"{state}{' ✎' if f.dirty else ''}"

    def _styled_badges(self, f: FileNode) -> Text:
        """Return a Rich Text with each badge glyph individually colored."""
        th = self.current_theme
        fg = th.foreground or "default"
        success = th.success or fg
        warning = th.warning or fg
        t = Text()
        if not f.tracked and f.local:
            t.append("◌", style=warning)      # staged, not yet a set member
        elif f.tracked and not f.local:
            t.append("◇", style="dim")        # in set, not pulled
        else:
            t.append("✓", style=success)      # tracked · local
        if f.dirty:
            t.append(" ✎", style=warning)
        return t

    def _styled_file_label(self, f: FileNode) -> Text:
        """Return a styled Rich Text for a file leaf: colored badges + plain name."""
        t = self._styled_badges(f)
        t.append("  ")
        t.append(f.name)
        return t

    def _set_label(self, setn, styles, is_member=False):
        """The label for a set node: name + CURRENT/BATCH/UPDATE SET markers.
        Shared by _add_set_node (build) and _reconcile_set (in-place relabel)."""
        secondary, success, accent = styles
        label = Text()
        if is_member:
            label.append("◈ ", style=f"bold {accent}")
        if setn.is_current:
            label.append(setn.name, style=f"bold {success}")
            label.append("  CURRENT", style=f"bold {success}")
        else:
            label.append(setn.name, style=f"bold {secondary}")
        if setn.is_base:
            label.append("  BATCH", style="dim")
        if is_member:
            label.append("  UPDATE SET", style="dim")
        return label

    def _add_set_node(self, parent, setn, styles, prev_key, restore_holder, is_member=False):
        """Render a set label into parent — nested member update sets FIRST (at the very
        top), then the set's own tables/files.

        A batch base can hold both its own records AND child update sets. Rendering the
        children above the base's own files keeps them from being buried, and `is_member`
        marks them (glyph + "UPDATE SET" tag) so a nested set doesn't read as a table row.

        restore_holder is a one-element list used as a mutable out-param so the
        caller can capture the leaf that matches prev_key from any depth of recursion.
        """
        _, _, accent = styles
        node = parent.add(self._set_label(setn, styles, is_member), ("set", setn), expand=True)
        # Nested update sets first, so they sit above the base set's own files.
        for m in setn.members:
            self._add_set_node(node, m, styles, prev_key, restore_holder, is_member=True)
        for tbl in setn.tables:
            tnode = node.add(Text(tbl.label, style=accent), expand=True)
            for f in tbl.files:
                leaf = tnode.add_leaf(self._styled_file_label(f), ("file", f))
                self._node_files[leaf.id] = f
                if prev_key is not None and (f.table, f.sys_id) == prev_key:
                    restore_holder[0] = leaf
        return node

    def _render_tree(self, model: TreeModel) -> None:
        # Try a surgical in-place reconcile; fall back to full rebuild when the
        # tree is empty, the model errored, or structure changed in ways the
        # reconcile doesn't yet handle.
        tree = self.query_one("#tree", Tree)
        scroll_before = tree.scroll_offset
        if not self._reconcile_tree(model):
            self._full_render_tree(model)
        else:
            self._last_model = model
            self._set_loading(False)
            self._render_staging()
        try:
            tree.scroll_to(y=scroll_before.y, animate=False)
        except Exception:
            pass

    def _full_render_tree(self, model: TreeModel) -> None:
        self._last_model = model
        # Capture current selection so we can restore it after clear().
        prev_sel = self._selected_file()
        prev_key = (prev_sel.table, prev_sel.sys_id) if prev_sel is not None else None

        self._node_files.clear()
        name = model.current_set.name if model.current_set else "(none)"
        self.query_one("#info-main", Static).update(
            f"◈ [dim]CURRENT SET[/] [b]{name}[/]   [dim]SCRATCH[/] {self._scratch}")

        tree = self.query_one("#tree", Tree)
        th = self.current_theme
        primary = th.primary or th.foreground or "default"
        secondary = getattr(th, "secondary", None) or th.foreground or "default"
        accent = getattr(th, "accent", None) or th.foreground or "default"
        success = th.success or th.foreground or "default"

        tree.clear()
        if model.error:
            tree.root.add_leaf(Text(f"✗  {model.error}", style=f"bold {th.error}"))
            tree.root.add_leaf(Text("press r to retry", style="dim"))
            self._set_loading(False)
            return
        if not model.scopes:
            tree.root.add_leaf(Text("No tracked sets. Press a to add one.", style="dim"))
            self._set_loading(False)
            return

        restore_holder = [None]   # mutable out-param for _add_set_node
        styles = (secondary, success, accent)
        for scope in model.scopes:
            scope_label = Text(scope.name, style=f"bold {primary}")
            snode = tree.root.add(scope_label, expand=True)
            for setn in scope.sets:
                self._add_set_node(snode, setn, styles, prev_key, restore_holder)
        self._set_loading(False)

        # Restore cursor to the previously-selected file node if still present.
        restore_node = restore_holder[0]
        if restore_node is not None:
            try:
                tree.select_node(restore_node)
            except Exception:
                pass

        self._render_staging()

    def _reconcile_tree(self, model: TreeModel) -> bool:
        """Surgically add/remove/relabel sets, tables, and file leaves in place when
        the scope-name list is unchanged vs _last_model. Returns True if fully handled;
        False to signal the caller to full-rebuild (scope-list change / error / empty)."""
        tree = self.query_one("#tree", Tree)
        if self._last_model is None or not tree.root.children or model.error:
            return False
        if [s.name for s in model.scopes] != [s.name for s in self._last_model.scopes]:
            return False   # scope add/remove/reorder → full rebuild (rare)

        # Scope list matches: refresh info bar, then reconcile each scope's set list.
        name = model.current_set.name if model.current_set else "(none)"
        self.query_one("#info-main", Static).update(
            f"◈ [dim]CURRENT SET[/] [b]{name}[/]   [dim]SCRATCH[/] {self._scratch}")
        scope_nodes = [n for n in tree.root.children]   # one per scope, in order
        for scope, snode in zip(model.scopes, scope_nodes):
            self._reconcile_set_list(snode, scope.sets)
        return True

    def _reconcile_set_list(self, scope_node, sets):
        # Set nodes are matched POSITIONALLY, not by sys_id: a scope can legitimately
        # hold several sets that share a sys_id (a batch base plus its own member view),
        # so a sys_id key would collapse them. build_tree emits a stable order, so
        # zip the existing set children against the wanted sets, reconcile the common
        # prefix, remove leftover existing nodes, and add any new tail sets.
        styles = self._tree_styles()
        existing = [n for n in scope_node.children
                    if n.data and n.data[0] == "set"]
        for node, setn in zip(existing, sets):
            self._reconcile_set(node, setn, styles)
        for node in existing[len(sets):]:
            self._remove_node(node)
        for setn in sets[len(existing):]:
            self._add_set_node(scope_node, setn, styles, None, [None])

    def _reconcile_set(self, set_node, setn, styles, is_member=False):
        # Member sets first (match _add_set_node order), then tables. Both are matched
        # positionally for the same reason as _reconcile_set_list (duplicate member
        # sys_ids and repeated table labels both occur in real trees).
        set_node.set_label(self._set_label(setn, styles, is_member))
        member_nodes = [n for n in set_node.children
                        if n.data and n.data[0] == "set"]
        for mnode, m in zip(member_nodes, setn.members):
            self._reconcile_set(mnode, m, styles, is_member=True)
        for mnode in member_nodes[len(setn.members):]:
            self._remove_node(mnode)
        _, _, accent = styles
        for m in setn.members[len(member_nodes):]:
            self._add_set_node(set_node, m, styles, None, [None], is_member=True)
        # Tables: nodes carry no ("set", …) data; matched positionally against setn.tables.
        table_nodes = [n for n in set_node.children
                       if not (n.data and n.data[0] == "set")]
        for tnode, tbl in zip(table_nodes, setn.tables):
            if str(tnode.label) != tbl.label:
                tnode.set_label(Text(tbl.label, style=accent))
            self._reconcile_files(tnode, tbl.files)
        for tnode in table_nodes[len(setn.tables):]:
            self._remove_node(tnode)
        for tbl in setn.tables[len(table_nodes):]:
            tnode = set_node.add(Text(tbl.label, style=accent), expand=True)
            self._reconcile_files(tnode, tbl.files)

    def _reconcile_files(self, table_node, files):
        existing = {}
        for leaf in table_node.children:
            f = self._node_files.get(leaf.id)
            if f is not None:
                existing[(f.table, f.sys_id)] = leaf
        wanted = {(f.table, f.sys_id) for f in files}
        for key, leaf in list(existing.items()):
            if key not in wanted:
                self._node_files.pop(leaf.id, None)
                self._remove_node(leaf)
        for f in files:
            leaf = existing.get((f.table, f.sys_id))
            if leaf is None:
                leaf = table_node.add_leaf(self._styled_file_label(f), ("file", f))
                self._node_files[leaf.id] = f
            else:
                self._node_files[leaf.id] = f
                leaf.set_label(self._styled_file_label(f))

    def _remove_node(self, node):
        for sub in SndeckApp._iter_tree_nodes(node):
            self._node_files.pop(sub.id, None)
        node.remove()

    def _tree_styles(self):
        th = self.current_theme
        secondary = getattr(th, "secondary", None) or th.foreground or "default"
        success = th.success or th.foreground or "default"
        accent = getattr(th, "accent", None) or th.foreground or "default"
        return (secondary, success, accent)

    @work(thread=True, exclusive=True, group="fetch")
    def _fetch(self) -> None:
        tracked = load_state().tracked_sets
        model = build_tree(self._client, self._scratch, tracked)
        self.call_from_thread(self._render_tree, model)

    def action_refresh(self) -> None:
        self._set_loading(True)
        self._fetch()

    def _selected_file(self) -> FileNode | None:
        node = self.query_one("#tree", Tree).cursor_node
        if node is None:
            return None
        return self._node_files.get(node.id)

    def action_open_browser(self) -> None:
        node = self.query_one("#tree", Tree).cursor_node
        f = self._node_files.get(node.id) if node else None
        if f is not None:
            url = instance_url_for(self._client.instance, kind="record",
                                   table=f.table, sys_id=f.sys_id)
            webbrowser.open(url)
            return
        data = getattr(node, "data", None)
        if data and data[0] == "set":
            url = instance_url_for(self._client.instance, kind="update_set", sys_id=data[1].sys_id)
            webbrowser.open(url)
            return
        self.notify("Select a file or update set to open.", severity="warning")

    @work(thread=True, exclusive=True, group="pull")
    def _pull(self) -> None:
        try:
            user, cur = resolve_current_set(self._client)
            if not user:
                self.call_from_thread(self.notify, "Could not resolve current ServiceNow user.",
                                      severity="error")
                self.call_from_thread(self._set_loading, False)
                return
            if not cur:
                self.call_from_thread(self.notify, "No current update set to pull.",
                                      severity="warning")
                self.call_from_thread(self._set_loading, False)
                return
            summary = pull_set(self._client, self._scratch, cur.sys_id, cur.name)
            msg = (f"Pulled {summary.pulled} record(s) from '{cur.name}'"
                   + (f"  ({summary.skipped} skipped: deleted/missing)" if summary.skipped else "")) \
                  if summary.pulled else f"'{cur.name}' — nothing to pull."
            self.call_from_thread(self.notify, msg,
                                  severity="information" if summary.pulled else "warning")
            tracked = load_state().tracked_sets
            model = build_tree(self._client, self._scratch, tracked)
            self.call_from_thread(self._render_tree, model)
        except AuthExpiredError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            self.call_from_thread(self._set_loading, False)

    def action_pull_set(self) -> None:
        self._set_loading(True)
        self._pull()

    def _pull_one(self, table: str, sys_id: str) -> None:
        """Pull a single record into the current set's workspace.

        Pure logic — no Textual worker or notify calls so it is unit-testable
        synchronously.  Raises on failure:
          - ValueError("no-current-set") if there is no active update set.
          - LookupError (propagated from pull_record) if the record is not found.
        Callers that need a tree refresh (e.g. _pull_one_worker) are responsible
        for triggering it after this returns.
        """
        _user, cur = resolve_current_set(self._client)
        if not cur:
            raise ValueError("no-current-set")
        ws = set_workspace(self._scratch, cur.sys_id, cur.name)
        pull_record(self._client, table, sys_id, ws)

    @work(thread=True, group="pull")
    def _pull_one_worker(self, table: str, sys_id: str) -> None:
        try:
            self._pull_one(table, sys_id)
            model = build_tree(self._client, self._scratch, load_state().tracked_sets)
            self.call_from_thread(self._render_tree, model)
        except ValueError:
            self.call_from_thread(self.notify, "No current set to stage into.",
                                  severity="warning")
        except LookupError:
            self.call_from_thread(self.notify, f"{table}/{sys_id} not found.",
                                  severity="error")
        except AuthExpiredError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            self.call_from_thread(self._set_loading, False)

    def action_get_record(self) -> None:
        def after(result) -> None:
            if result is not None:
                table, sys_id = result
                self._set_loading(True)
                self._pull_one_worker(table, sys_id)
        self.push_screen(GetRecordScreen(), after)

    def _set_write_indicator(self, on: bool) -> None:
        self.query_one("#info-app", Static).update("[WRITE]" if on else "[READ-ONLY]")

    def action_switch_set(self) -> None:
        def after(result) -> None:
            if result and result[0] == "switch":
                _, sid, name = result
                self._open_switch_confirm(sid, name)
            else:
                self.action_refresh()   # pins/files may have changed -> rebuild tree
        self.push_screen(PinnedSetsScreen(self._client, self._scratch), after)

    def _open_switch_confirm(self, set_sys_id: str, set_name: str) -> None:
        self._set_write_indicator(True)

        def after(confirmed: bool) -> None:
            self._set_write_indicator(False)
            if confirmed:
                self._do_switch(set_sys_id, set_name)

        self.push_screen(SwitchConfirmScreen(set_name), after)

    def _scope_for_set(self, set_sys_id: str) -> str | None:
        """Raw scope of the SetNode with this sys_id in the current model — searching
        top-level sets AND nested batch members — so any set (base, member, or
        standalone) switches into its own scope. None if not found."""
        model = self._last_model
        if model is None:
            return None

        def _find(setn):
            if setn.sys_id == set_sys_id:
                return setn.scope
            for m in setn.members:
                found = _find(m)
                if found is not None:
                    return found
            return None

        for sc in model.scopes:
            for setn in sc.sets:
                found = _find(setn)
                if found is not None:
                    return found
        return None

    def _activate_or_switch(self, set_sys_id: str, set_name: str, scope: str) -> None:
        """Switch the current update set — nothing more. Points the chosen set's prefs
        at it (sys_update_set + its own scope pointer + the recents header, via
        set_current_update_set) and aligns the active application scope to it.

        Deliberately relationship-blind: a set's parent/child (batch) membership is a
        commit-time grouping in ServiceNow, not a 'current set' concept, so switching
        never touches any other set's pointers. Per-scope routing for multi-member
        batches lives in the push path (_do_push_all), which is the only place batch
        membership actually affects capture. The write itself lives in
        updatesets.switch_current_set — testable without Textual."""
        switch_current_set(self._client, set_sys_id, scope)

    @work(thread=True, exclusive=True, group="write")
    def _do_switch(self, set_sys_id: str, set_name: str) -> None:
        try:
            # Look up the SetNode (top-level OR nested member) to get its scope, so a
            # scoped member switches into its own scope. A not-found node degrades
            # gracefully to scope="global".
            scope = self._scope_for_set(set_sys_id) or "global"
            self._activate_or_switch(set_sys_id, set_name, scope)
            self.call_from_thread(self.notify, f"Switched to '{set_name}'.",
                                  severity="information")
            model = build_tree(self._client, self._scratch, load_state().tracked_sets)
            self.call_from_thread(self._render_tree, model)
        except AuthExpiredError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
        except Exception as e:  # write failure -> surface, no state change
            self.call_from_thread(self.notify, f"Switch failed: {e}", severity="error")

    def action_push(self) -> None:
        files = dirty_files_from_disk(self._scratch)
        if not files:
            self.notify("Nothing to push — no local edits.", severity="information")
            return
        self._set_write_indicator(True)
        records = [(f.name, f.table) for f in files]
        items = [(f.name, f.table, str(f.record_path)) for f in files]

        def after(confirmed: bool) -> None:
            self._set_write_indicator(False)
            if confirmed:
                self._do_push_all(items)

        self.push_screen(PushAllConfirmScreen(records), after)

    @work(thread=True, exclusive=True, group="write")
    def _do_push_all(self, items: list) -> None:
        """Push every staged record via push.push_all (per-record scope routing),
        then surface notifications and refresh the tree. Write logic lives in push.py."""
        from .push import push_all
        try:
            outcomes = push_all(self._client, self._last_model, [p for (_n, _t, p) in items])
        except AuthExpiredError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        for o in outcomes:
            if o.warning:
                self.call_from_thread(self.notify, f"Warning: {o.warning}", severity="warning")
            elif o.routed_scope:
                self.call_from_thread(self.notify,
                                      f"Aligned scope → {o.routed_scope} before push",
                                      severity="information")
        pushed = sum(1 for o in outcomes if o.pushed)
        skipped = [o.name for o in outcomes if not o.pushed]
        msg = f"Pushed {pushed}"
        if skipped:
            msg += f" · skipped {len(skipped)} ({', '.join(skipped)})"
        self.call_from_thread(self.notify, msg,
                              severity="information" if pushed else "warning")
        model = build_tree(self._client, self._scratch, load_state().tracked_sets)
        self.call_from_thread(self._render_tree, model)

    def _lang_for(self, path) -> str | None:
        ext = str(path).rsplit(".", 1)[-1].lower()
        return {"js": "javascript", "json": "json", "html": "html",
                "scss": "css", "css": "css"}.get(ext)

    async def _show_preview(self, node) -> None:
        new_key = (node.table, node.sys_id) if node is not None else None
        same_record = (new_key is not None and new_key == self._preview_key
                       and self._preview is not None and not self._preview.placeholder)
        if same_record:
            # Same record shown again (e.g. post-push re-pull fires ScratchChanged twice,
            # or local refresh). Tabs are already correct — reload the currently-selected
            # field (or fall back to default) in case content changed on disk.
            reload_key = (
                self._selected_field_key
                if self._selected_field_key is not None
                   and self._preview is not None
                   and any(f.key == self._selected_field_key for f in self._preview.fields)
                else (self._preview.default_key if self._preview else None)
            )
            if reload_key:
                self._load_field(reload_key)
            return

        # Record changed — reset field tracking.
        self._selected_field_key = None
        self._preview_key = new_key
        self._preview = build_preview(node) if node else None
        header = self.query_one("#preview-header", Static)
        tabs = self.query_one("#preview-tabs", Horizontal)
        body = self.query_one("#preview-body", TextArea)
        await tabs.remove_children()
        if self._preview is None:
            header.update("")
            body.text = ""
            return
        header.update(self._preview.header)
        if self._preview.placeholder:
            body.text = self._preview.placeholder
            return
        for f in self._preview.fields:
            cls = "field-tab selected" if f.key == self._preview.default_key else "field-tab"
            tab_id = f"tab-{f.key}"
            tabs.mount(Static(f.label, classes=cls, id=tab_id))
        self._load_field(self._preview.default_key)

    def _apply_editor_theme(self) -> None:
        try:
            body = self.query_one("#preview-body", TextArea)
            body.theme = "github_light" if self.theme == LATTE else "vscode_dark"
        except Exception:
            pass

    def _load_field(self, key: str) -> None:
        if not self._preview:
            return
        field = next((f for f in self._preview.fields if f.key == key), None)
        if not field:
            return
        self._selected_field_key = key
        body = self.query_one("#preview-body", TextArea)
        try:
            body.text = read_field(field)
        except OSError as e:
            body.text = f"(could not read {field.path}: {e})"
        lang = self._lang_for(field.path)
        if lang:
            try:
                body.language = lang
            except Exception:
                pass
        self._apply_editor_theme()
        for tab in self.query(".field-tab"):
            tab.set_class(tab.id == f"tab-{key}", "selected")

    def _field_keys(self) -> list[str]:
        if self._preview is None or self._preview.placeholder or not self._preview.fields:
            return []
        return [f.key for f in self._preview.fields]

    def action_next_field(self) -> None:
        keys = self._field_keys()
        if not keys:
            return
        if self._selected_field_key is None or self._selected_field_key not in keys:
            self._load_field(keys[0])
        else:
            idx = keys.index(self._selected_field_key)
            self._load_field(keys[(idx + 1) % len(keys)])

    def action_prev_field(self) -> None:
        keys = self._field_keys()
        if not keys:
            return
        if self._selected_field_key is None or self._selected_field_key not in keys:
            self._load_field(keys[-1])
        else:
            idx = keys.index(self._selected_field_key)
            self._load_field(keys[(idx - 1) % len(keys)])

    async def on_tree_node_highlighted(self, event) -> None:
        await self._show_preview(self._node_files.get(event.node.id))

    async def on_tree_node_selected(self, event) -> None:
        data = getattr(event.node, "data", None)
        if data and data[0] == "set":
            self._open_switch_confirm(data[1].sys_id, data[1].name)

    def on_click(self, event) -> None:
        w = getattr(event, "widget", None)
        if w is not None and w.has_class("field-tab") and w.id:
            self._load_field(w.id.removeprefix("tab-"))

    def action_toggle_theme(self) -> None:
        self.theme = next_theme(self.theme)
        if self._last_model is not None:
            self._render_tree(self._last_model)
        self._apply_editor_theme()

    def action_legend(self) -> None:
        self.push_screen(LegendScreen())

    def action_add_set(self) -> None:
        def after(new_pins) -> None:
            self._set_loading(True)
            self._pull_sets(list(new_pins or []))
        self.push_screen(SetPickerScreen(self._client), after)

    @work(thread=True, exclusive=True, group="pull")
    def _pull_sets(self, sys_ids: list) -> None:
        try:
            for sid in sys_ids:
                meta = update_set_meta(self._client, sid)
                pull_set(self._client, self._scratch, sid, meta.name if meta else sid)
            model = build_tree(self._client, self._scratch, load_state().tracked_sets)
            self.call_from_thread(self._render_tree, model)
        except AuthExpiredError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            self.call_from_thread(self._set_loading, False)
