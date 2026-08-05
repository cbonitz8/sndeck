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
