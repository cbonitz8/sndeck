"""`python -m sndeck` / `sndeck [scratch_dir]` entrypoint."""
from __future__ import annotations

import os
import sys

from .app import SndeckApp
from .auth import TokenProvider
from .config import load_instance
from .rest import TableClient
from .settings import load_sndeck_config, resolve_scratch, resolve_instance, resolve_theme


_SUBCOMMANDS = {"us", "pull", "status", "push", "refresh"}
_HELP_FLAGS = {"-h", "--help", "help"}

_USAGE = """\
sndeck — ServiceNow update-set workbench

Usage:
  sndeck                       Launch the interactive TUI (needs a terminal)
  sndeck <scratch-dir>         Launch the TUI with a specific scratch root
  sndeck us get|ls|set <id>    Show / list / switch the current update set
  sndeck pull <table> <id>     Download a record to the current set's workspace
  sndeck status                Show the current set's staging area
  sndeck push <table> <id>     Push one staged record   (push --all for all)
  sndeck refresh <table> <id>  Rebase a record's snapshot from the instance
                               (refresh --all for every on-disk record;
                                --overwrite-local to also replace local files)

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
    scratch = resolve_scratch(sys.argv, os.environ, cfg, os.getcwd())
    instance_name = resolve_instance(os.environ, cfg)
    try:
        inst = load_instance(instance_name)
    except (OSError, KeyError) as e:
        print(f"sndeck: cannot load instance {instance_name!r}: {e}", file=sys.stderr)
        return 2
    client = TableClient(inst, TokenProvider(inst))
    theme = resolve_theme(os.environ, cfg)
    SndeckApp(client, scratch, theme).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
