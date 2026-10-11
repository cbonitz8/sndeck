"""Non-interactive sndeck subcommands (us/pull/retarget/status/push/refresh/gc/migrate)
for headless/agent use."""
from __future__ import annotations

import argparse
import json
import os
import sys

from dataclasses import asdict
from datetime import date

from .auth import AuthExpiredError, TokenProvider
from .config import instance_names, load_instance
from .gc import apply_gc, plan_for, reconcile_and_report
from .migrate import ClientLookups, apply_migration, find_legacy, plan_migration
from .push import push_all
from .records import default_kind, pull, retarget
from .refresh import apply_refresh, missing_outcome
from .rest import TableClient
from .snapshot import read_meta
from .settings import load_sndeck_config, resolve_instance, resolve_store_root
from .store import EDIT, REF, Store, StoreInvariantError, check_store_root
from .sync import is_dirty, local_field_changes
from .updatesets import (list_update_sets, resolve_current_set, sets_holding,
                         switch_current_set, update_set_meta, update_set_states)

GC_VERBS = {"C1": "delete", "C2": "delete", "C3": "move to attic", "C4": "keep",
            "C5": "delete", "C6": "delete"}
NO_TARGET_LABEL = "(no target set — run `sndeck retarget`)"


def _emit(human: str, obj, as_json: bool) -> None:
    print(json.dumps(obj) if as_json else human)


def _fail(msg: str, *, as_json: bool) -> int:
    print(json.dumps({"error": msg}) if as_json else f"sndeck: {msg}", file=sys.stderr)
    return 1


def _run_reconcile(client, store) -> None:
    """Best-effort auto cleanup (gc C1/C2) of the store. Prints report to stderr; never
    raises."""
    for line in reconcile_and_report(client, store):
        print(line, file=sys.stderr)


def cmd_us_get(client, *, as_json: bool) -> int:
    _user, cur = resolve_current_set(client)
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


def cmd_us_ls(client, store, *, as_json: bool) -> int:
    _run_reconcile(client, store)
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
    if not switch_current_set(client, sys_id):
        return _fail("could not resolve current ServiceNow user", as_json=as_json)
    obj = {"sys_id": meta.sys_id, "name": meta.name, "state": meta.state, "scope": meta.scope}
    _emit(f"Switched to {meta.name} [{meta.scope}]", obj, as_json)
    return 0


def cmd_pull(client, store, table, sys_id, *, ref: bool, edit: bool,
             overwrite_local: bool, as_json: bool) -> int:
    if ref and edit:
        return _fail("--ref and --edit are exclusive", as_json=as_json)
    instance = client.instance.name
    kind = REF if ref else EDIT if edit else default_kind(instance)
    target = None
    if kind == EDIT:
        _user, cur = resolve_current_set(client)
        if cur is None:
            return _fail("no current update set; run 'sndeck us set <sys_id>' first",
                         as_json=as_json)
        target = {"sys_id": cur.sys_id, "name": cur.name}
    try:
        o = pull(client, store, table, sys_id, kind=kind, target_set=target,
                 overwrite_local=overwrite_local)
    except (LookupError, StoreInvariantError) as e:
        return _fail(str(e), as_json=as_json)
    files = sorted(p.name for p in o.path.iterdir()
                   if p.is_file() and p.name not in ("record.json", ".snapshot.json"))
    obj = {"action": o.action, "table": o.table, "sys_id": o.sys_id, "name": o.name,
           "kind": o.kind, "folder": str(o.path), "files": files,
           "target_set": o.target_set, "reason": o.reason,
           "blocked_fields": list(o.blocked_fields)}
    if o.action == "refused":
        _emit(f"refused {table}/{sys_id} → {o.path}\n  {o.reason}", obj, as_json)
        return 1
    human = f"{o.action} {o.kind} copy {table}/{sys_id} → {o.path}"
    if files:
        human += "\n  " + "\n  ".join(files)
    if o.action == "refreshed" and o.kind == EDIT:
        if o.target_set is None:
            human += (f"\n  this copy has no target set; "
                      f"run `sndeck retarget {table} {sys_id}` to give it one")
        elif o.target_set["sys_id"] != target["sys_id"]:
            human += (f"\n  target set stays '{o.target_set['name']}'; "
                      f"run `sndeck retarget {table} {sys_id}` to move it")
    _emit(human, obj, as_json)
    return 0


def cmd_retarget(client, store, table, sys_id, set_id, *, as_json: bool) -> int:
    if set_id is None:
        _user, cur = resolve_current_set(client)
        if cur is None:
            return _fail("no current update set and no --set given", as_json=as_json)
        set_id = cur.sys_id
    meta = update_set_states(client, [set_id]).get(set_id)
    if meta is None:
        return _fail(f"update set {set_id} not found", as_json=as_json)
    if meta.state != "in progress":
        return _fail(f"update set '{meta.name}' is {meta.state}; pick an in-progress set",
                     as_json=as_json)
    target = {"sys_id": meta.sys_id, "name": meta.name}
    try:
        path = retarget(store, client.instance.name, table, sys_id, target)
    except LookupError as e:
        return _fail(str(e), as_json=as_json)
    _emit(f"{table}/{sys_id} now pushes to '{meta.name}' ({meta.sys_id})",
          {"folder": str(path), "target_set": target}, as_json)
    return 0


def cmd_status(client, store, *, as_json: bool) -> int:
    _run_reconcile(client, store)
    instance = client.instance.name
    copies = store.iter_copies(instance, EDIT)
    _user, cur = resolve_current_set(client)
    holding = sets_holding(client, [(c.table, c.sys_id) for c in copies])
    all_sets = sorted({s for v in holding.values() for s in v}
                      | {c.target_set["sys_id"] for c in copies if c.target_set is not None})
    states = update_set_states(client, all_sets) if all_sets else {}
    groups: dict[str, dict] = {}
    untargeted: list[dict] = []
    for c in copies:
        holders = holding[(c.table, c.sys_id)]
        target_id = None if c.target_set is None else c.target_set["sys_id"]
        if is_dirty(c.path):
            state = "dirty"
        elif target_id is not None and target_id in holders:
            state = "clean"
        else:
            state = "local-only"
        rec = {"table": c.table, "sys_id": c.sys_id, "name": c.name, "state": state,
               "other_in_progress_sets": [states[s].name for s in holders
                                          if s != target_id and s in states
                                          and states[s].state == "in progress"]}
        if state == "dirty":
            rec["fields_changed"] = [ch.field for ch in local_field_changes(c.path)]
        if c.target_set is None:
            untargeted.append(rec)
        else:
            groups.setdefault(target_id, {"set": c.target_set, "records": []})["records"].append(rec)
    order = sorted(groups, key=lambda s: (cur is None or s != cur.sys_id, groups[s]["set"]["name"]))
    shown = [groups[s] for s in order]
    if untargeted:
        shown.append({"set": None, "label": NO_TARGET_LABEL, "records": untargeted})
    obj = {"instance": instance,
           "current_set": None if cur is None else {"sys_id": cur.sys_id, "name": cur.name},
           "groups": shown}
    if as_json:
        print(json.dumps(obj))
        return 0
    print(f"Current set: {cur.name} ({cur.sys_id})" if cur else "No current update set.")
    glyph = {"dirty": "✎", "local-only": "◌", "clean": "✓"}
    for g in shown:
        if g["set"] is None:
            print(f"→ {g['label']}")
        else:
            print(f"→ {g['set']['name']} ({g['set']['sys_id']})")
        for r in g["records"]:
            line = f"  {glyph[r['state']]} {r['state']:<11} {r['table']}  {r['name']}  {r['sys_id']}"
            if r["other_in_progress_sets"]:
                line += "  ⚠ also in: " + ", ".join(r["other_in_progress_sets"])
            print(line)
    n_dirty = sum(1 for g in shown for r in g["records"] if r["state"] == "dirty")
    print(f"{n_dirty} dirty" if n_dirty else "clean — nothing to push")
    return 0


def cmd_push(client, store, table, sys_id, all_: bool, *, as_json: bool) -> int:
    instance = client.instance.name
    if all_:
        paths = [c.path for c in store.iter_copies(instance, EDIT) if is_dirty(c.path)]
        if not paths:
            _emit("clean — nothing to push", [], as_json)
            return 0
    else:
        if not (table and sys_id):
            return _fail("push requires <table> <sys_id> or --all", as_json=as_json)
        path = store.locate(instance, table, sys_id, EDIT)
        if path is None:
            if store.locate(instance, table, sys_id, REF) is not None:
                return _fail(f"only a reference copy of {table}/{sys_id} exists; "
                             f"pull --edit first", as_json=as_json)
            return _fail(f"no edit copy of {table}/{sys_id} on {instance}", as_json=as_json)
        if not is_dirty(path):
            _emit(f"nothing to push for {table}/{sys_id}", [], as_json)
            return 0
        paths = [path]
    try:
        outcomes = push_all(client, paths)
    except AuthExpiredError as e:
        return _fail(str(e), as_json=as_json)

    pushed = sum(1 for o in outcomes if o.pushed)
    skipped = [o for o in outcomes if not o.pushed]
    human = f"Pushed {pushed}"
    if skipped:
        human += f" · skipped {len(skipped)} ({', '.join(o.name for o in skipped)})"
    _emit(human, [asdict(o) for o in outcomes], as_json)
    for o in outcomes:
        if o.warning:
            print(f"warning: {o.warning}", file=sys.stderr)
        if o.capture_error:
            print(f"MISCAPTURED {o.table}/{o.name}: {o.capture_error}", file=sys.stderr)
    return 1 if any(o.capture_error for o in outcomes) else 0


def _refresh_human(o) -> str:
    if o.missing:
        return f"✗ {o.table}/{o.name}  {o.folder} — no longer exists on the instance"
    if o.refused:
        return f"✗ {o.table}/{o.name}  {o.folder} — {o.reason}"
    base = ", ".join(o.snapshot_changed) if o.snapshot_changed else "already current"
    line = f"✓ refreshed {o.table}/{o.name}  {o.folder} — snapshot: {base}"
    if o.local_changed:
        line += f"; local overwritten: {', '.join(o.local_changed)}"
    line += "; clean" if o.clean_after else ""
    return line


def cmd_refresh(client, store, table, sys_id, all_, overwrite_local, *,
                as_json: bool) -> int:
    """Re-read live records and rebase their edit copies' .snapshot.json, regardless of
    the target set's update-set state."""
    instance = client.instance.name
    if all_:
        folders = [c.path for c in store.iter_copies(instance, EDIT)]
        if not folders:
            _emit(f"no edit copies on {instance} to refresh", [], as_json)
            return 0
    else:
        if not (table and sys_id):
            return _fail("refresh requires <table> <sys_id> or --all", as_json=as_json)
        path = store.locate(instance, table, sys_id, EDIT)
        if path is None:
            return _fail(f"no edit copy of {table}/{sys_id} on {instance}", as_json=as_json)
        folders = [path]

    outcomes = []
    for folder in folders:
        t, s, name = read_meta(folder)
        rec = client.get_record(t, s, display_value="false")
        if rec is None:
            outcomes.append(missing_outcome(folder, t, s, name))
        else:
            outcomes.append(apply_refresh(folder, rec, overwrite_local=overwrite_local))

    if as_json:
        print(json.dumps([asdict(o) for o in outcomes]))
    else:
        for o in outcomes:
            print(_refresh_human(o))
        refreshed = sum(1 for o in outcomes if o.refreshed)
        stuck = [o for o in outcomes if o.refused or o.missing]
        summary = f"refreshed {refreshed}"
        if stuck:
            summary += f" · {len(stuck)} not refreshed"
        print(summary)

    # A targeted single refresh that resolved nothing is an error (exit 1) so scripts
    # notice; --all is a best-effort sweep and always exits 0.
    if not all_ and outcomes and (outcomes[0].refused or outcomes[0].missing):
        return 1
    return 0


def cmd_gc(client, store, *, apply: bool, attic_older_than: int | None, as_json: bool) -> int:
    if attic_older_than is not None and attic_older_than < 1:
        return _fail("--attic-older-than must be at least 1 day", as_json=as_json)
    instance = client.instance.name
    actions = plan_for(client, store, attic_older_than_days=attic_older_than)
    applied = apply_gc(actions, store, instance, date.today(), auto=False) if apply else []
    rows = [{"class": a.cls, "action": GC_VERBS[a.cls], "path": str(a.path),
             "label": a.label, "detail": a.detail, "applied": a in applied}
            for a in actions]
    if as_json:
        print(json.dumps(rows))
        return 0
    for r in rows:
        mark = "done" if r["applied"] else "plan"
        print(f"{mark}  {r['class']}  {r['action']:<13} {r['label']}  {r['path']}"
              + (f"  ({r['detail']})" if r["detail"] else ""))
    if not rows:
        print("gc: nothing to do")
    elif not apply:
        print("dry run — re-run with --apply")
    return 0


def cmd_migrate(store, clients, *, apply: bool, as_json: bool) -> int:
    """Convert legacy per-task folders under the store root into the store. `clients` are
    only read (GET); without `apply` nothing on disk changes."""
    names = [c.instance.name for c in clients]
    steps = plan_migration(find_legacy(store.root, names), ClientLookups(clients))
    rows = [{"action": s.action, "path": str(s.copy.path), "instance": s.instance,
             "target_set": s.target_set, "reason": s.reason} for s in steps]
    if not apply:
        if as_json:
            print(json.dumps(rows))
        else:
            for r in rows:
                print(f"{r['action']:<6} {r['instance']}  {r['path']}  — {r['reason']}")
            print(f"dry run: {len(rows)} legacy folders — re-run with --apply")
        return 0
    try:
        report = apply_migration(steps, store, date.today())
    except RuntimeError as e:
        return _fail(str(e), as_json=as_json)
    _emit(f"stored {report.stored} · attic {report.atticked} · dropped {report.dropped}"
          + "".join(f"\n  left in {k}: {', '.join(v)}" for k, v in report.leftovers.items()),
          {"stored": report.stored, "atticked": report.atticked, "dropped": report.dropped,
           "leftovers": report.leftovers}, as_json)
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

    pull_p = sub.add_parser("pull", parents=[g],
                            help="pull the record's one local copy")
    pull_p.add_argument("table")
    pull_p.add_argument("sys_id")
    pull_p.add_argument("--ref", action="store_true", dest="ref",
                        help="pull a read-only reference copy")
    pull_p.add_argument("--edit", action="store_true", dest="edit",
                        help="pull an edit copy targeting the current update set")
    pull_p.add_argument("--overwrite-local", action="store_true", dest="overwrite_local",
                        help="discard unpushed local edits that differ from the instance")

    rt = sub.add_parser("retarget", parents=[g],
                        help="point an edit copy's push at another update set")
    rt.add_argument("table")
    rt.add_argument("sys_id")
    rt.add_argument("--set", dest="set_id", default=None,
                    help="update set sys_id (default: the current update set)")

    sub.add_parser("status", parents=[g])

    push = sub.add_parser("push", parents=[g])
    push.add_argument("table", nargs="?")
    push.add_argument("sys_id", nargs="?")
    push.add_argument("--all", action="store_true", dest="all_")

    refresh = sub.add_parser(
        "refresh", parents=[g],
        help="rebase an edit copy's .snapshot.json from the live instance",
        description="Re-read the live record and rebase the matching edit copy's "
                    ".snapshot.json — regardless of the target set's update-set state. "
                    "Clears 'phantom dirty' copies (local matches the instance but the "
                    "snapshot is stale) so gc can reap them, and satisfies push's "
                    "'refresh first' clobber-guard. Snapshot-only by default; refuses "
                    "when the local files genuinely diverge from the instance unless "
                    "--overwrite-local is given.")
    refresh.add_argument("table", nargs="?", help="table name (omit with --all)")
    refresh.add_argument("sys_id", nargs="?", help="record sys_id (omit with --all)")
    refresh.add_argument("--all", action="store_true", dest="all_",
                         help="refresh every edit copy on this instance")
    refresh.add_argument("--overwrite-local", action="store_true", dest="overwrite_local",
                         help="replace local field files with the instance copy when they "
                              "diverge (discards the local edit)")

    gc_p = sub.add_parser("gc", parents=[g], help="clean the store (dry run without --apply)")
    gc_p.add_argument("--apply", action="store_true", dest="apply")
    gc_p.add_argument("--attic-older-than", type=int, default=None, dest="attic_older_than",
                      metavar="N", help="also delete attic day folders older than N days")

    mig = sub.add_parser("migrate", parents=[g],
                         help="convert legacy per-task folders into the store")
    mig.add_argument("--apply", action="store_true", dest="apply")
    return p


def _make_client(name: str):
    inst = load_instance(name)
    return TableClient(inst, TokenProvider(inst))


def dispatch(argv: list[str], *, client_factory=None) -> int:
    """Run one subcommand. Refuses a store root that check_store_root rejects. Without an
    instance registry, the one instance being used is the only known instance name."""
    args = _build_parser().parse_args(argv)
    cfg = load_sndeck_config()
    name = getattr(args, "instance", None) or resolve_instance(os.environ, cfg)
    if getattr(args, "scratch", None):
        root, source = os.path.expanduser(args.scratch), "--scratch"
    else:
        root, source = resolve_store_root([], os.environ, cfg)
    try:
        names = instance_names()
    except FileNotFoundError:
        names = [name]
    problem = check_store_root(root, names)
    aj = getattr(args, "as_json", False)
    if problem:
        return _fail(f"{problem} (store root from {source})", as_json=aj)
    store = Store(root)
    make = client_factory if client_factory else _make_client
    try:
        if args.cmd == "migrate":
            known = names if name in names else names + [name]
            order = ([n for n in known if default_kind(n) == EDIT]
                     + [n for n in known if default_kind(n) != EDIT])
            print(f"sndeck: migrate reads instances {', '.join(order)}", file=sys.stderr)
            return cmd_migrate(store, [make(n) for n in order], apply=args.apply, as_json=aj)
        client = make(name)
        print(f"sndeck: instance {name}", file=sys.stderr)
        if args.cmd == "us":
            if args.us_cmd == "get":
                return cmd_us_get(client, as_json=aj)
            if args.us_cmd == "ls":
                return cmd_us_ls(client, store, as_json=aj)
            if args.us_cmd == "set":
                return cmd_us_set(client, args.sys_id, as_json=aj)
        if args.cmd == "pull":
            return cmd_pull(client, store, args.table, args.sys_id, ref=args.ref,
                            edit=args.edit, overwrite_local=args.overwrite_local, as_json=aj)
        if args.cmd == "retarget":
            return cmd_retarget(client, store, args.table, args.sys_id, args.set_id, as_json=aj)
        if args.cmd == "status":
            return cmd_status(client, store, as_json=aj)
        if args.cmd == "push":
            return cmd_push(client, store, args.table, args.sys_id, args.all_, as_json=aj)
        if args.cmd == "refresh":
            return cmd_refresh(client, store, args.table, args.sys_id,
                               args.all_, args.overwrite_local, as_json=aj)
        if args.cmd == "gc":
            return cmd_gc(client, store, apply=args.apply,
                          attic_older_than=args.attic_older_than, as_json=aj)
    except (AuthExpiredError, StoreInvariantError) as e:
        return _fail(str(e), as_json=aj)
    return 2
