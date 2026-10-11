"""`python -m sndeck` / `sndeck [store_root]` entrypoint."""
from __future__ import annotations

import os
import sys

from .app import SndeckApp
from .auth import TokenProvider
from .config import instance_names, load_instance
from .rest import TableClient
from .settings import load_sndeck_config, resolve_instance, resolve_store_root, resolve_theme
from .store import check_store_root


_SUBCOMMANDS = {"us", "pull", "status", "push", "refresh", "retarget", "gc", "migrate"}
_HELP_FLAGS = {"-h", "--help", "help"}

_USAGE = """\
sndeck — ServiceNow update-set workbench

Usage:
  sndeck                       Launch the interactive TUI (needs a terminal)
  sndeck <store-root>          Launch the TUI with a specific store root
  sndeck us get|ls|set <id>    Show / list / switch the current update set
  sndeck pull <table> <id>     Pull the record's one local copy (edit on dev, ref elsewhere;
                               --ref / --edit to choose, --overwrite-local to discard edits)
  sndeck retarget <table> <id> Point an edit copy's push at the current (or --set) set
  sndeck status                Edit copies grouped by target set
  sndeck push <table> <id>     Push one staged record   (push --all for all)
  sndeck refresh <table> <id>  Rebase a record's snapshot from the instance
                               (refresh --all for every on-disk record;
                                --overwrite-local to also replace local files)
  sndeck gc [--apply]          Clean the store (dry run without --apply)
  sndeck migrate [--apply]     Convert legacy per-task folders into the store

Add --json to any subcommand for machine-readable output.
Run `sndeck <subcommand> --help` for that subcommand's options.

The bare TUI is interactive and needs a terminal. In a headless or
non-interactive context, use the subcommands above — not bare `sndeck`."""


def _print_usage() -> None:
    print(_USAGE)


def main() -> int:
    argv = sys.argv
    if len(argv) > 1 and argv[1] in _SUBCOMMANDS:
        from .cli import dispatch
        return dispatch(argv[1:])
    if len(argv) > 1 and argv[1] in _HELP_FLAGS:
        _print_usage()
        return 0
    # Everything below launches the interactive TUI, which requires a terminal.
    # Headless (piped / no tty) it would hang — print usage instead of stalling.
    if not sys.stdout.isatty():
        _print_usage()
        return 0
    cfg = load_sndeck_config()
    root, source = resolve_store_root(sys.argv, os.environ, cfg)
    instance_name = resolve_instance(os.environ, cfg)
    try:
        names = instance_names()
    except FileNotFoundError:
        names = [instance_name]
    problem = check_store_root(root, names)
    if problem:
        print(f"sndeck: {problem} (store root from {source})", file=sys.stderr)
        return 2
    try:
        inst = load_instance(instance_name)
    except (OSError, KeyError) as e:
        print(f"sndeck: cannot load instance {instance_name!r}: {e}", file=sys.stderr)
        return 2
    client = TableClient(inst, TokenProvider(inst))
    theme = resolve_theme(os.environ, cfg)
    SndeckApp(client, root, theme).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
