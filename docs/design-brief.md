# sndeck — Design Brief

> For a design pass on sndeck's terminal UI. Pair this with screenshots.
> Goal: a concrete restyle (theme, colors, layout, spacing, states) that stays
> within the TUI / Textual constraints below.

## What it is

A read-only **ServiceNow workbench** for developers. At a glance it shows:

- the current ServiceNow **update set** (a change bucket),
- a **drift feed** of my recent captured changes,
- a list of **code records** pulled locally — each flagged ✓ (in my current set)
  or ⚠ (in a different set / never captured).

Keys: `p` pull, `r` refresh, `q` quit. It's a monitoring/sync pane meant to sit
beside an editor or agent.

## Current layout (see screenshots)

- One-line **header**: current set + the scratch directory path.
- Two **side-by-side panels**: FILES (table: record / table / set / ✓) and
  DRIFT FEED (table: when / type / target / set).
- A **footer** bar listing keybindings.

It's the framework's default unstyled look (flat dark), and the two side-by-side
tables are cramped — headers truncate (e.g. "table" → "tabl").

## Tech & constraints (Python + the Textual TUI framework)

- **Terminal medium:** a monospace character grid, not pixels. No images. Visuals
  are text, box-drawing characters, color, and glyphs (emoji / nerd-font). Sizes
  are in character cells or fr/%, never px.
- **Textual styling (TCSS)** is a *subset* of web CSS: colors (truecolor if the
  terminal allows), named border styles (round/heavy/double/solid/…),
  padding/margin, `dock`, and grid/horizontal/vertical layout, plus theme
  variables (`$primary`, `$panel`, `$accent`, light/dark). It does **not** support
  custom fonts, box-shadow, or pixel border-radius; gradients and animation are
  limited.
- **Widgets available:** Static, DataTable, Tree, Header, Footer, Input, Tabs
  (`TabbedContent`/`TabPane`), etc.
- **Known rough edges:** cramped two-column tables; no visual hierarchy or theme;
  empty/error states are plain text; refresh is manual and network calls briefly
  block the UI (a known future fix).

## UI ideas under consideration

- **Tabs instead of side-by-side.** FILES and DRIFT FEED currently compete for
  horizontal space, which is what truncates the columns. Consider `TabbedContent`
  so each view gets the full width — or another layout (docked, stacked, resizable
  split) that gives each room. Open to the recommendation; the tables being
  readable matters more than showing both at once.
- **A legend command.** A keybinding (e.g. `?` or `l`) that shows a legend for the
  ✓/⚠ markers and column meanings. Partly a feature rather than pure styling, but
  it affects how self-explanatory the main view must be vs. what it can offload.

## What I want

A cohesive theme (palette, borders, header/footer treatment), a readable layout
that fixes the table truncation (tabs or otherwise), clear ✓/⚠ styling, and
polished empty / error / loading states — all achievable in Textual. Concrete
TCSS snippets and layout suggestions welcome.
