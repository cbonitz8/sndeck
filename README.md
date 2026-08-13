# sndeck

A zero-LLM-token **ServiceNow workbench TUI** — a top/bottom-split file browser and
update-set tracker. Reuses [happy-platform-mcp]'s cached keychain token, talks
to the Table API directly, and shows a scope → update set → table → file tree with an
inline code viewer, live file-watching, tracked/pinned sets, deep-links, and two
confirm-gated writes (switch current set, push local edits).

## Install

```
python -m pip install -e '.[dev]'   # or in a venv
```

sndeck requires `watchfiles>=0.21` for live file-watching of the scratch directory — it's declared as a runtime dependency in `pyproject.toml` and installed automatically with the above command.

## Run

```
sndeck [scratch_dir]   # or: python -m sndeck [scratch_dir]
```

The optional `scratch_dir` positional argument sets the **root** directory sndeck uses for pulled records; sndeck creates one workspace subdir per update set within it (`<root>/<set-name>__<setSysId>/…`). If omitted, the precedence order below applies.

Keys: `enter` open · `P` push · `s` switch set · `a` add set · `o` browser · `p` pull ·
`r` refresh · `t` theme · `?` legend · `q` quit.

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
sndeck pull <table> <id>      # download a record into the current set's workspace
sndeck status                 # the current set's staging area (clean/dirty/local-only)
sndeck push <table> <id>      # push one staged record   (push --all for all dirty)
sndeck refresh <table> <id>   # rebase a record's snapshot from the instance
```

The normal loop is **pull → edit → push**. `status`/`ls` also run a best-effort prune:
a set's workspace is deleted once the set leaves *in progress* and every record in it is
clean.

**`refresh` — clearing "phantom dirty".** A record is *dirty* purely when its local field
files differ from its frozen `.snapshot.json` baseline. If a record's local files end up
matching the instance while the snapshot does not — e.g. it was pushed via a *different*
update set, or its set was marked complete before the push — the folder stays dirty
forever: prune keeps it (warning `⚠ set '…' is complete but has unpushed edits — not
pruned`), and it can't be re-pulled because pull needs the set to be current and a
complete set won't stay current. `refresh` is the way out — it re-reads the live record
and rebases that folder's `.snapshot.json` **regardless of the enclosing set's state and
without needing it to be current**. Once the snapshot matches, the record goes clean and
prune reaps it on the next `status`. Use this instead of hand-editing `.snapshot.json` or
deleting the folder by hand.

- `sndeck refresh <table> <id>` — snapshot-only. Finds the folder(s) by scanning every
  workspace for a matching `record.json`; rebases the snapshot. This is also the fix for
  push's `instance changed since pull … — refresh first` clobber-guard error.
- `sndeck refresh --all` — do it for every record in every on-disk workspace (best-effort;
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
scratch_dir = "~/.sn-scratch"      # scratch ROOT; sndeck creates one workspace subdir per update set within it
instance    = "dev"                # ServiceNow instance name
theme       = "dark"               # "dark" (Macchiato) or "light" (Latte); default dark
```

Override the config file path with `SNDECK_CONFIG`.

### Themes

sndeck ships two Catppuccin themes — **Macchiato** (dark, default) and **Latte** (light). Select via the `theme` key in `~/.config/sndeck/config.toml` (`"dark"` or `"light"`), or the `SNDECK_THEME` environment variable (precedence `SNDECK_THEME` > config `theme` > dark). Press `t` to toggle live for the current session.

### Precedence

**Scratch dir** (highest → lowest):
1. CLI positional arg: `sndeck /path/to/scratch`
2. `SNDECK_SCRATCH` env var — the cockpit launcher sets this per task, keeping each cockpit isolated
3. `scratch_dir` in `~/.config/sndeck/config.toml`
4. Current working directory

**Instance** (highest → lowest):
1. `SNDECK_INSTANCE` env var
2. `instance` in `~/.config/sndeck/config.toml`
3. `"dev"` (built-in default)

### Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `SNDECK_INSTANCE` | `dev` | Instance name in the happy-platform-mcp config |
| `SNDECK_SCRATCH` | cwd | Scratch dir holding pulled record folders |
| `SNDECK_CONFIG` | `~/.config/sndeck/config.toml` | Path to the sndeck config file |
| `SNDECK_THEME` | `dark` | `"dark"` (Macchiato) or `"light"` (Latte) |
| `SNDECK_STATE` | `~/.config/sndeck/state.json` | Tracked sets and split pane ratio |
| `SNDECK_INSTANCES_CONFIG` | auto: `~/.config/happy-platform-mcp/instances.json` then `~/.config/sndeck/instances.json` | Explicit path to the instance-config JSON; overrides auto-detection. sndeck also honors the MCP's `HAPPY_CONFIG_PATH`. (`SNDECK_FORK_CONFIG` still works as a legacy alias.) |
| `SNDECK_ACCOUNT` | the instance name (e.g. `dev`) | Keychain account key for the refresh token — happy-platform-mcp stores it under `currentInstanceName`, not `<user>@<instance>` (see Auth) |

## Auth

sndeck does **not** mint tokens. It reads the refresh token happy-platform-mcp cached in the
macOS Keychain (service `happy-platform-mcp`) and replays the refresh grant. If no token
is present, sndeck fails loud with a visible banner — **sign in via happy-platform-mcp first**.

## In a cockpit

The `sn-cockpit.sh` launcher opens an sndeck monitor pane when run with `SNDECK=1`:

```
SNDECK=1 sn-cockpit.sh <task-label>
```

[happy-platform-mcp]: https://www.npmjs.com/package/happy-platform-mcp
