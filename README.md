# sndeck

A zero-LLM-token **ServiceNow workbench TUI** — a top/bottom-split file browser and
update-set tracker. Reuses [happy-platform-mcp]'s cached refresh token, talks
to the Table API directly, and shows a scope → update set → table → file tree with an
inline code viewer, live file-watching, tracked/pinned sets, deep-links, and two
confirm-gated writes (switch current set, push local edits).

## Install

```
python -m pip install -e '.[dev]'   # or in a venv
```

sndeck requires `watchfiles>=0.21` for live file-watching of the store root — it's declared as a runtime dependency in `pyproject.toml` and installed automatically with the above command.

## Run

```
sndeck [store_root]   # or: python -m sndeck [store_root]
```

The optional `store_root` positional argument sets the **store root** sndeck keeps pulled records in. There is one copy of each record per instance: `<root>/<instance>/<table>/<name>__<sysId>/` for edit copies, `<root>/<instance>/_ref/…` for read-only reference copies, and `<root>/<instance>/_attic/<date>/…` for edits `gc` set aside. If omitted, the precedence order below applies. sndeck refuses a root that is really a legacy per-task or per-set folder; run `sndeck migrate` to convert one. It also refuses any folder inside the default `~/.sn-scratch` (such as a task folder left after `migrate`): use `~/.sn-scratch` itself.

Keys: `enter` open · `P` push · `s` switch set · `a` add set · `o` browser · `p` pull ·
`r` refresh · `t` theme · `?` legend · `q` quit.

In the TUI, `p` pulls follow the same default as the CLI (edit copies on dev, reference copies elsewhere), and a refused pull shows as a warning.

## Writes

sndeck is read-only except for two confirm-gated writes, both attributed to your token
owner: **switch current update set** (`s`) and **push local edits** (`P`). Push is
clobber-guarded — it refuses if the instance changed a field you're about to write since
you pulled. It never edits code (use your editor/agent) and never moves records.

## Headless / agent subcommands

For non-interactive use (agents, scripts, CI) sndeck exposes a small CLI. Add `--json`
to any subcommand for machine-readable output.

```
sndeck us get|ls|set <id>     # show / list / switch the current update set
sndeck pull <table> <id>      # download a record's one local copy (--edit / --ref; default by instance)
sndeck retarget <table> <id>  # point an edit copy at another update set (--set <id>, default: current)
sndeck status                 # edit copies grouped by target set (clean/dirty/local-only)
sndeck push <table> <id>      # push one staged record   (push --all for all dirty)
sndeck refresh <table> <id>   # rebase a record's snapshot from the instance
sndeck gc [--apply]           # clean the store; dry run without --apply
sndeck migrate [--apply]      # convert legacy per-task folders into the store
```

The normal loop is **pull → edit → push**. On dev, `pull` makes an *edit copy* that
remembers its **target update set**; push captures into that set, not into whichever set is
current. On any other instance `pull` makes a read-only *reference copy*, which is never
pushed (`pull --edit` / `--ref` override the default). `retarget` repoints an edit copy.
Push refuses, before writing anything, a ref copy, a legacy copy (run `migrate`), a missing
or malformed target set, a target set that is not in progress or has no application scope,
and a copy that belongs to a different instance. After a push the copy is re-baselined from
the live record when every pushed field reads back as sent. If the instance did not take a
pushed field (a field ACL, or a before business rule that rewrote it), the local files are
kept, the copy stays dirty, and push warns naming the field. `status` groups edit copies by target set, with a "no target set" group for
copies that need a `retarget`.

**`gc` — cleaning the store.** `status`, `us ls` and TUI start also run the *auto* part of `gc`: an edit copy
whose target set has shipped (complete/ignore) is deleted when it is clean or every edit is
already live. `sndeck gc` is a dry run that lists the rest; `--apply` also moves a shipped
copy with a real unpushed edit into `_attic/<date>/` with a `local.diff`, and deletes
reference copies older than 14 days. `--attic-older-than N` (N ≥ 1) also deletes attic day
folders older than N days; today's is never deleted. A copy with no target set, or whose set
cannot be found, is kept with a warning, and a copy whose `.snapshot.json` is unreadable counts
as dirty. A shipped copy whose folder holds a file sndeck did not write (anything besides
`record.json`, `.snapshot.json` and its code-field files, e.g. `script.js.bak` or an agent's
draft) is never deleted as clean: it waits for `gc --apply`, which moves it to the attic.
`--attic-older-than` covers `<root>/<instance>/_attic/` only; the root-level `_attic/`
(migrated copies whose instance could not be resolved) is cleaned by hand.

**`migrate` — one-time conversion.** Converts the old per-task, per-set folders into the
store. It is a dry run without `--apply`, always reads set and record state from dev whatever
`--instance` says, and checks every destination before moving anything. The dry run prints
one line per legacy copy (a `store` line names its target set and sys_id), a summary per old
task folder (store / attic / drop counts and the files left in place), and any destination
clash that would make `--apply` refuse. Rollback covers the copy phase: no legacy folder is
deleted until every copy has been written; a failure after that point is not rolled back.
It never creates an edit copy on a non-dev instance: there a clean legacy copy is dropped
and a dirty one goes to the attic. A copy that would be dropped but holds a file sndeck did
not write goes to the attic instead. After the copies, it removes only the set and table
folders the migrated copies leave empty; the task folders themselves, their notes,
`set-map.json` and any other folder you made stay in place.

**`refresh` — clearing "phantom dirty".** A record is *dirty* purely when its local field
files differ from its frozen `.snapshot.json` baseline. If a record's local files end up
matching the instance while the snapshot does not — e.g. it was pushed via a *different*
update set — the copy keeps showing as an unpushed edit while its target set is in progress.
Once the set ships, `gc` deletes it as already-live (class C2), and a re-pull also clears it
(pull blocks only fields that are dirty *and* differ from the instance). `refresh` is the
way out without waiting: it re-reads the live record and rebases that copy's
`.snapshot.json` **regardless of the target set's state and without needing it to be
current**. Once the snapshot matches, the record goes clean. Use this instead of
hand-editing `.snapshot.json` or deleting the folder by hand.

- `sndeck refresh <table> <id>` — snapshot-only. Finds the instance's edit copy and
  rebases its snapshot. This is also the fix for
  push's `instance changed since pull … — refresh first` clobber-guard error.
- `sndeck refresh --all` — do it for every edit copy on the instance (best-effort;
  skips-and-reports anything it can't safely resolve).
- `--overwrite-local` — if the local files genuinely diverge from the instance, a
  snapshot-only rebase is *refused* (it would leave the record dirty and retire the drift
  guard). Pass this to replace the local files with the instance copy, discarding the local
  edit (the discarded fields are printed).

## Configuration

### Config file — `~/.config/sndeck/config.toml`

sndeck reads a TOML config file on startup. All keys are optional.

```toml
# ~/.config/sndeck/config.toml
scratch_dir = "~/.sn-scratch"      # store root; copies live under <root>/<instance>/…
instance    = "dev"                # ServiceNow instance name
theme       = "dark"               # "dark" (Macchiato) or "light" (Latte); default dark
```

Override the config file path with `SNDECK_CONFIG`.

### Themes

sndeck ships two Catppuccin themes — **Macchiato** (dark, default) and **Latte** (light). Select via the `theme` key in `~/.config/sndeck/config.toml` (`"dark"` or `"light"`), or the `SNDECK_THEME` environment variable (precedence `SNDECK_THEME` > config `theme` > dark). Press `t` to toggle live for the current session.

### Precedence

**Store root** (highest → lowest):
1. CLI positional arg: `sndeck /path/to/store`
2. `SNDECK_SCRATCH` env var
3. `scratch_dir` in `~/.config/sndeck/config.toml`
4. `~/.sn-scratch`

**Instance** (highest → lowest):
1. `SNDECK_INSTANCE` env var
2. `instance` in `~/.config/sndeck/config.toml`
3. `"dev"` (built-in default)

### Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `SNDECK_INSTANCE` | `dev` | Instance name in the happy-platform-mcp config |
| `SNDECK_SCRATCH` | `~/.sn-scratch` | Store root holding pulled record folders |
| `SNDECK_CONFIG` | `~/.config/sndeck/config.toml` | Path to the sndeck config file |
| `SNDECK_THEME` | `dark` | `"dark"` (Macchiato) or `"light"` (Latte) |
| `SNDECK_STATE` | `~/.config/sndeck/state.json` | Tracked sets and split pane ratio |
| `SNDECK_INSTANCES_CONFIG` | auto: `~/.config/happy-platform-mcp/instances.json` then `~/.config/sndeck/instances.json` | Explicit path to the instance-config JSON; overrides auto-detection. sndeck also honors the MCP's `HAPPY_CONFIG_PATH`. (`SNDECK_FORK_CONFIG` still works as a legacy alias.) |
| `SNDECK_ACCOUNT` | the instance name (e.g. `dev`) | Account key for the refresh token file (`token-<account>`) — happy-platform-mcp stores it under `currentInstanceName`, not `<user>@<instance>` (see Auth) |

## Auth

sndeck does **not** mint tokens. It reads the refresh token happy-platform-mcp cached in
`~/.config/happy-platform-mcp/token-<account>` (a 0600 file in a 0700 dir; `XDG_CONFIG_HOME`
is honored) and replays the refresh grant. If no token
is present, sndeck fails loud with a visible banner — **sign in via happy-platform-mcp first**.

## In a cockpit

The `sn-cockpit.sh` launcher opens an sndeck monitor pane when run with `SNDECK=1`:

```
SNDECK=1 sn-cockpit.sh <task-label>
```

The cockpit task folder holds notes only; sndeck always uses the store root, so every
cockpit shares one copy of each record.

[happy-platform-mcp]: https://www.npmjs.com/package/happy-platform-mcp
