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

## The snapshot (module: `snapshot.py`)

The on-disk representation of one pulled record, and the single owner of its shape. A
record folder holds:

- `record.json` — `{"_meta": {table, sys_id, name, pulled_at}, **fields}`; identity + full
  non-underscore column dump.
- `.snapshot.json` — `{**fields}`; the **frozen baseline** a local edit is diffed against.
- `<field><ext>` — one file per code field; the editable surface.

**Dirty** — a record is dirty when a code field's local file differs from its snapshot
baseline (newline-normalized). `snapshot.is_dirty` is the one predicate the staging pane,
push, prune, and refresh all share. `scratch.py` owns *where* a record folder lives (folder
naming, enumeration); `snapshot.py` owns *what* is inside it.

## Operations

- **pull** — SN read → local write (`records.pull_record`). Never writes to ServiceNow.
- **push** — diff local field files vs snapshot, drift-guard against the instance, PUT the
  changed fields so they capture into the current set (`sync.build_push_plan` / `apply_push`).
- **refresh** — re-baseline a record's snapshot from the live instance, decoupled from set
  state; the way out of "phantom dirty" (`refresh.py`).
- **prune** — reap clean workspaces whose set has shipped; warn on dirty (`prune.py`).
