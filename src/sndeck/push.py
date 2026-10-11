"""UI-free push orchestration: scope routing + apply. Shared by app.py and cli.py."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .auth import AuthExpiredError
from .refresh import apply_refresh
from .snapshot import _body, _norm, instance_fields, read_meta_extra
from .store import EDIT, REF
from .sync import build_push_plan, apply_push
from .updatesets import (capture_for_record, current_user, read_pref, set_routing_meta,
                         set_scope_pointer, set_current_application, update_set_meta)


@dataclass(frozen=True)
class PushOutcome:
    table: str
    sys_id: str
    name: str
    pushed: bool
    reason: str | None = None        # why it was skipped/failed, else None
    routed_scope: str | None = None  # scope newly aligned to (only when it changed)
    warning: str | None = None       # scope-routing warning, else None
    capture_error: str | None = None  # pushed, but not captured into the intended set


def _set_label(client, set_sys_id: str) -> str:
    meta = update_set_meta(client, set_sys_id)
    return f"'{meta.name}' ({set_sys_id})" if meta is not None else f"({set_sys_id})"


def verify_capture(client, table: str, sys_id: str, intended: str | None) -> str | None:
    """None when the record's newest sys_update_xml row is in the intended set, else a
    message naming both sets."""
    if not intended:
        return "could not verify capture: no intended update set resolved"
    cap = capture_for_record(client, table, sys_id)
    if cap is None:
        return f"no sys_update_xml capture found; expected {_set_label(client, intended)}"
    if cap.set_id != intended:
        return (f"captured into '{cap.set_name}' ({cap.set_id}), "
                f"expected {_set_label(client, intended)}")
    return None


def _not_taken(changes, live: dict) -> list[str]:
    """Pushed fields whose live value is not what was PUT (newline-normalized): a field
    ACL or a before-business-rule kept or rewrote it."""
    fields = instance_fields(live)
    return [c.field for c in changes if _norm(c.local) != _norm(_body(fields, c.field))]


def push_all(client, record_paths: list) -> list["PushOutcome"]:
    """Push every staged record path. Per-record: build plan, route to the copy's
    `_meta.target_set` (refusing ref copies and targets that are not in progress), aim
    that scope's pointer at the target, align the active scope only when it changes,
    start a fresh server session if either pref moved (an open session keeps the prefs
    it read at open, so its PUT would capture by the old ones), apply, verify the capture
    landed in the target set, then re-baseline the copy from the live record: when every
    pushed field reads back as PUT, the live record replaces snapshot and files alike;
    when one does not (a field ACL or a before-BR), the local files are kept, the copy
    stays dirty and the outcome warns, naming those fields. A
    failed record becomes a not-pushed outcome and the rest still push; a miscapture is
    a pushed outcome carrying capture_error. AuthExpiredError propagates (never
    swallowed)."""
    user = current_user(client)
    aligned = read_pref(client, user.sys_id, "apps.current_app") if user else None
    outcomes: list[PushOutcome] = []
    for path in record_paths:
        table = sys_id = name = ""
        routed = warning = reason = capture_error = intended = None
        pushed = False
        try:
            plan = build_push_plan(client, path)
            table, sys_id, name = plan.table, plan.sys_id, plan.name
            extra = read_meta_extra(path)
            if "kind" not in extra:
                raise RuntimeError("legacy copy without store metadata; run `sndeck migrate`")
            copy_instance = extra.get("instance")
            if copy_instance != client.instance.name:
                raise RuntimeError(f"copy belongs to instance '{copy_instance}', "
                                   f"not '{client.instance.name}'")
            if extra["kind"] == REF:
                raise RuntimeError("reference copies are never pushed; pull --edit first")
            if extra["kind"] != EDIT:
                raise RuntimeError(f"unknown copy kind {extra['kind']!r} in record.json")
            target = extra.get("target_set")
            if target is None:
                raise RuntimeError("no target update set; run `sndeck retarget`")
            if not (isinstance(target, dict) and "sys_id" in target and "name" in target):
                raise RuntimeError("malformed target_set in record.json; run `sndeck retarget`")
            meta = set_routing_meta(client, target["sys_id"])
            if meta is None:
                raise RuntimeError(f"target set '{target['name']}' not found on "
                                   f"{client.instance.name}; run `sndeck retarget`")
            if meta.state != "in progress":
                raise RuntimeError(f"target set '{meta.name}' is {meta.state}; "
                                   f"run `sndeck retarget`")
            intended = meta.sys_id
            if user:
                try:
                    moved = set_scope_pointer(client, user.sys_id, meta.scope, intended)
                    if meta.scope != aligned:
                        set_current_application(client, user.sys_id, meta.scope)
                        routed = meta.scope
                        aligned = meta.scope
                    if moved or routed:
                        client.reset_session()
                except AuthExpiredError:
                    raise
                except Exception:
                    warning = f"could not route scope before pushing {plan.table}/{plan.sys_id}"
            apply_push(client, plan)
            try:
                capture_error = verify_capture(client, plan.table, plan.sys_id, intended)
            except AuthExpiredError:
                raise
            except Exception as e:
                capture_error = f"could not verify capture: {e}"
            pushed = True
            rebase = None
            try:
                live = client.get_record(plan.table, plan.sys_id, display_value="false")
                if live is not None:
                    untaken = _not_taken(plan.changes, live)
                    if untaken:
                        rebase = (f"pushed, but the instance did not take {untaken}; "
                                  f"local files kept, copy stays dirty")
                    else:
                        apply_refresh(path, live, overwrite_local=True)
            except AuthExpiredError:
                raise
            except Exception as e:
                rebase = f"pushed, but could not re-baseline the local copy: {e}"
            if rebase is not None:
                warning = rebase if warning is None else f"{warning}; {rebase}"
        except AuthExpiredError:
            raise
        except Exception as e:
            reason = str(e) or "push failed"
            if not name:
                name = Path(path).name
        outcomes.append(PushOutcome(table, sys_id, name, pushed, reason, routed, warning,
                                    capture_error))
    return outcomes


def push_one(client, record_path) -> "PushOutcome":
    return push_all(client, [record_path])[0]
