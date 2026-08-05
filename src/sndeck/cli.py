"""Non-interactive sndeck subcommands (us/pull/status/push) for headless/agent use."""
from __future__ import annotations

import argparse
import json
import os
import sys

from dataclasses import asdict

from .auth import AuthExpiredError, TokenProvider
from .config import load_instance
from .prune import reconcile_and_report
from .push import push_all, push_one
from .records import pull_record, set_workspace
from .rest import TableClient
from .scratch import set_workspaces
from .settings import load_sndeck_config, resolve_instance, resolve_scratch
from .state import load_state
from .sync import is_dirty, local_field_changes
from .tree import build_tree
from .updatesets import (current_update_set, current_user, list_update_sets,
                         set_current_update_set, update_set_meta)


def _emit(human: str, obj, as_json: bool) -> None:
    print(json.dumps(obj) if as_json else human)


def _fail(msg: str, *, as_json: bool) -> int:
    print(json.dumps({"error": msg}) if as_json else f"sndeck: {msg}", file=sys.stderr)
    return 1


def _run_reconcile(client, scratch) -> None:
    """Best-effort scratch prune. Prints report to stderr; never raises."""
    for line in reconcile_and_report(client, scratch):
        print(line, file=sys.stderr)


def cmd_us_get(client, *, as_json: bool) -> int:
    user = current_user(client)
    cur = current_update_set(client, user.user_name) if user else None
    if cur is None:
        _emit("No current update set.", None, as_json)
        return 0
    meta = update_set_meta(client, cur.sys_id)
    scope = meta.scope if meta else "Global"
    state = meta.state if meta else cur.state
    obj = {"sys_id": cur.sys_id, "name": cur.name, "state": state, "scope": scope}
    _emit(f"Current update set:\n  {cur.sys_id}  {cur.name}  [{state}, {scope}]",
          obj, as_json)
    return 0


def cmd_us_ls(client, scratch, *, as_json: bool) -> int:
    _run_reconcile(client, scratch)
    sets = list_update_sets(client)
    rows = [{"sys_id": s.sys_id, "name": s.name, "state": s.state, "scope": s.scope}
            for s in sets]
    if as_json:
        print(json.dumps(rows))
    else:
        if not sets:
            print("No in-progress update sets.")
        for s in sets:
            print(f"{s.sys_id}  {s.name}  [{s.scope}]")
    return 0


def cmd_us_set(client, sys_id: str, *, as_json: bool) -> int:
    meta = update_set_meta(client, sys_id)
    if meta is None:
        return _fail(f"update set {sys_id} not found", as_json=as_json)
    user = current_user(client)
    if user is None:
        return _fail("could not resolve current ServiceNow user", as_json=as_json)
    set_current_update_set(client, user.sys_id, sys_id)
    obj = {"sys_id": meta.sys_id, "name": meta.name, "state": meta.state, "scope": meta.scope}
    _emit(f"Switched to {meta.name} [{meta.scope}]", obj, as_json)
    return 0


def cmd_pull(client, scratch, table: str, sys_id: str, *, as_json: bool) -> int:
    user = current_user(client)
    cur = current_update_set(client, user.user_name) if user else None
    if cur is None:
        return _fail("no current update set; run 'sndeck us set <sys_id>' first",
                     as_json=as_json)
    ws = set_workspace(scratch, cur.sys_id, cur.name)
    try:
        ref = pull_record(client, table, sys_id, ws)
    except LookupError as e:
        return _fail(str(e), as_json=as_json)
    files = sorted(p.name for p in ref.path.iterdir()
                   if p.is_file() and p.name not in ("record.json", ".snapshot.json"))
    obj = {"table": ref.table, "sys_id": ref.sys_id, "name": ref.name,
           "folder": str(ref.path), "files": files,
           "set": {"sys_id": cur.sys_id, "name": cur.name}}
    human = f"Pulled {table}/{sys_id} → {ref.path}"
    if files:
        human += "\n  " + "\n  ".join(files)
    _emit(human, obj, as_json)
    return 0


def _find_set_node(model, sys_id: str):
    """Return (SetNode, scope display name) for sys_id, searching top-level sets and
    their nested batch members; (None, None) if absent."""
    for scope in model.scopes:
        for setn in scope.sets:
            for cand in [setn, *setn.members]:
                if cand.sys_id == sys_id:
                    return cand, scope.name
    return None, None


def _record_state(f) -> str:
    if f.dirty:
        return "dirty"
    return "local-only" if not f.tracked else "clean"


def cmd_status(client, scratch, *, as_json: bool) -> int:
    _run_reconcile(client, scratch)
    model = build_tree(client, scratch, load_state().tracked_sets)
    if model.error:
        return _fail(model.error, as_json=as_json)
    cur = model.current_set
    if cur is None:
        _emit("No current update set.", None, as_json)
        return 0
    node, scope_name = _find_set_node(model, cur.sys_id)
    scope_name = scope_name or "Global"
    files = [f for tbl in (node.tables if node else []) for f in tbl.files
             if f.record_path is not None]
    records = []
    for f in files:
        state = _record_state(f)
        rec = {"table": f.table, "sys_id": f.sys_id, "name": f.name, "state": state}
        if state == "dirty":
            rec["fields_changed"] = [c.field for c in local_field_changes(f.record_path)]
        records.append(rec)
    obj = {"set": {"sys_id": cur.sys_id, "name": cur.name, "scope": scope_name},
           "records": records}
    if as_json:
        print(json.dumps(obj))
    else:
        print(f"Current set: {cur.name} [{scope_name}] ({cur.sys_id})")
        glyph = {"dirty": "✎", "local-only": "◌", "clean": "✓"}
        print(f"Staging area ({len(records)}):")
        for r in records:
            print(f"  {glyph[r['state']]} {r['state']:<11} {r['table']}  "
                  f"{r['name']}  {r['sys_id']}")
        n_dirty = sum(1 for r in records if r["state"] == "dirty")
        print(f"{n_dirty} dirty" if n_dirty else "clean — nothing to push")
    return 0


def cmd_push(client, scratch, table, sys_id, all_: bool, *, as_json: bool) -> int:
    user = current_user(client)
    cur = current_update_set(client, user.user_name) if user else None
    if cur is None:
        return _fail("no current update set; run 'sndeck us set <sys_id>' first",
                     as_json=as_json)
    ws = next((w for w in set_workspaces(scratch) if w.set_sys_id == cur.sys_id), None)
    records = ws.records if ws else []
    if all_:
        paths = [r.path for r in records if is_dirty(r.path)]
        if not paths:
            _emit("clean — nothing to push", [], as_json)
            return 0
    else:
        if not (table and sys_id):
            return _fail("push requires <table> <sys_id> or --all", as_json=as_json)
        ref = next((r for r in records
                    if r.table == table and r.sys_id == sys_id), None)
        if ref is None:
            return _fail(f"{table}/{sys_id} not staged in the current set", as_json=as_json)
        if not is_dirty(ref.path):
            _emit(f"nothing to push for {table}/{sys_id}", [], as_json)
            return 0

    model = build_tree(client, scratch, load_state().tracked_sets)
    try:
        if all_:
            outcomes = push_all(client, model, paths)
        else:
            outcomes = [push_one(client, model, ref.path)]
    except AuthExpiredError as e:
        return _fail(str(e), as_json=as_json)

    pushed = sum(1 for o in outcomes if o.pushed)
    skipped = [o for o in outcomes if not o.pushed]
    human = f"Pushed {pushed}"
    if skipped:
        human += f" · skipped {len(skipped)} ({', '.join(o.name for o in skipped)})"
    _emit(human, [asdict(o) for o in outcomes], as_json)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--instance")
    g.add_argument("--scratch")
    g.add_argument("--json", action="store_true", dest="as_json")

    p = argparse.ArgumentParser(prog="sndeck")
    sub = p.add_subparsers(dest="cmd", required=True)

    us = sub.add_parser("us")
    us_sub = us.add_subparsers(dest="us_cmd", required=True)
    us_sub.add_parser("get", parents=[g])
    us_sub.add_parser("ls", parents=[g])
    s = us_sub.add_parser("set", parents=[g])
    s.add_argument("sys_id")

    pull = sub.add_parser("pull", parents=[g])
    pull.add_argument("table")
    pull.add_argument("sys_id")

    sub.add_parser("status", parents=[g])

    push = sub.add_parser("push", parents=[g])
    push.add_argument("table", nargs="?")
    push.add_argument("sys_id", nargs="?")
    push.add_argument("--all", action="store_true", dest="all_")
    return p


def _make_client(name: str):
    inst = load_instance(name)
    return TableClient(inst, TokenProvider(inst))


def dispatch(argv: list[str], *, client_factory=None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = load_sndeck_config()
    name = getattr(args, "instance", None) or resolve_instance(os.environ, cfg)
    scratch = (os.path.expanduser(args.scratch) if getattr(args, "scratch", None)
               else resolve_scratch([], os.environ, cfg, os.getcwd()))
    client = client_factory(name) if client_factory else _make_client(name)
    print(f"sndeck: instance {name}", file=sys.stderr)
    aj = getattr(args, "as_json", False)
    try:
        if args.cmd == "us":
            if args.us_cmd == "get":
                return cmd_us_get(client, as_json=aj)
            if args.us_cmd == "ls":
                return cmd_us_ls(client, scratch, as_json=aj)
            if args.us_cmd == "set":
                return cmd_us_set(client, args.sys_id, as_json=aj)
        if args.cmd == "pull":
            return cmd_pull(client, scratch, args.table, args.sys_id, as_json=aj)
        if args.cmd == "status":
            return cmd_status(client, scratch, as_json=aj)
        if args.cmd == "push":
            return cmd_push(client, scratch, args.table, args.sys_id, args.all_, as_json=aj)
    except AuthExpiredError as e:
        return _fail(str(e), as_json=aj)
    return 2
