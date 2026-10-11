# sndeck — domain glossary

Names for the good seams. Architecture vocabulary (module, interface, depth, seam,
adapter, leverage, locality) lives in the `/codebase-design` skill; this file names the
*domain* concepts those seams are drawn around.

## Core concepts

- **Instance** — a ServiceNow environment (dev `ethosgroupdev`, prod `ethosgroup`),
  resolved from the happy-platform-mcp config registry with a keychain token.
- **Update set** — ServiceNow's change bucket. A record's edits capture into whichever
  update set is *current* for its scope. sndeck tracks/pins sets and can switch the current one.
- **Scope** — the application a record belongs to; the current update set is per-scope.
- **Code record / artifact** — a record on a table in the `CODE_ARTIFACTS` registry
  (business rule, script include, widget, …) whose script fields are extracted to their
  own editable files.

## The store (module: `store.py`)

The on-disk home of every pulled record, and the single owner of *where* a record folder
lives. There is **one copy per (instance, table, sys_id, kind)**, under one store root
(`~/.sn-scratch` by default):

```
<root>/<instance>/<table>/<name>__<sys_id>/         edit copy (writable, has a target set)
<root>/<instance>/_ref/<table>/<name>__<sys_id>/    reference copy (read-only, never pushed)
<root>/<instance>/_attic/<YYYY-MM-DD>/<table>/…     edits gc moved aside, with a local.diff
```

- **Kind** — `edit` or `ref`. `records.default_kind`: edit on dev, ref on every other
  instance. A second pull of the same copy refreshes it in place; two folders claiming the
  same key raise `StoreInvariantError`.
- **Target set** — the update set an edit copy pushes into, recorded in its `_meta`
  (`records.retarget` rewrites it). Ref copies have none.
- **`check_store_root`** — says why a path cannot be a store root (an update-set folder, an
  instance folder, a legacy per-task root, a folder of bare record folders), or `None`.
  sndeck refuses to start on a bad root rather than scatter copies into it.

## The snapshot (module: `snapshot.py`)

The on-disk representation of one pulled record, and the single owner of its shape. A
record folder holds:

- `record.json` — `{"_meta": {table, sys_id, name, pulled_at, instance, kind, target_set},
  **fields}`; identity + full non-underscore column dump. `target_set` (`{sys_id, name}`) is
  present on edit copies only.
- `.snapshot.json` — `{**fields}`; the **frozen baseline** a local edit is diffed against.
- `<field><ext>` — one file per code field; the editable surface.
- `local.diff` — only inside an attic folder: the unpushed edit gc preserved.

**Dirty** — a record is dirty when a code field's local file differs from its snapshot
baseline (newline-normalized). `snapshot.is_dirty` is the one predicate the staging pane,
push, gc, and refresh all share; a copy whose `.snapshot.json` cannot be read counts as
dirty (`is_dirty_or_unreadable`). `store.py` owns *where* a record folder lives;
`snapshot.py` owns *what* is inside it.

## Operations

- **pull** — SN read → the one local copy of that kind (`records.pull`). Never writes to
  ServiceNow. An edit pull needs a target set; a re-pull keeps the existing one and is
  refused when unpushed edits differ from the instance (unless `--overwrite-local`). Set
  pulls follow `default_kind`.
- **push** — diff local field files vs snapshot, drift-guard against the instance, PUT the
  changed fields so they capture into the copy's **target set** (`sync.build_push_plan` /
  `push.py`). Everything is refused before any write for: ref copies, legacy copies with no
  `kind`, a missing or malformed `target_set`, a target set that is not in progress or has
  no application scope, and a copy whose `_meta.instance` is not the client's. After a push
  the copy is re-baselined from the live record.
- **refresh** — re-baseline a record's snapshot from the live instance, decoupled from set
  state; the way out of "phantom dirty" (`refresh.py`).
- **retarget** — point an edit copy at another update set (default: the current one)
  without touching the record or its snapshot (`records.retarget`).
- **gc** — a pure planner (`gc.plan_gc`) sorts every copy of the instance into a class; the
  applier acts only on the classes allowed for the call (`gc.py`):

  | class | what | action | runs |
  |-------|------|--------|------|
  | C1 | edit copy, target set shipped (complete/ignore), clean | delete | auto |
  | C2 | shipped, dirty but every edit already live | delete | auto |
  | C3 | shipped, edit that differs from the instance | move to `_attic` + `local.diff` | `gc --apply` |
  | C4 | no target set, or target set not found | keep, warn | never |
  | C5 | ref copy older than 14 days | delete | `gc --apply` |
  | C6 | attic day folder older than N days | delete | `gc --apply --attic-older-than N` |

  The **auto subset** (C1, C2) runs best-effort on `status`/`ls`. `N` must be at least 1 and
  today's attic folder is never deleted. An unreadable `.snapshot.json` counts as dirty. A
  second attic move for the same record on the same day lands in a `-2` sibling folder.
- **migrate** — one-time conversion of the legacy per-task, per-set layout into the store
  (`migrate.py`; dry run without `--apply`). It always reads dev for set and record state,
  whatever `--instance` says. It never creates an edit copy on a non-dev instance: there a
  clean legacy copy is dropped and a dirty one goes to the attic (under its legacy parent
  path). Every destination is checked before anything moves, and a failed apply rolls back.
