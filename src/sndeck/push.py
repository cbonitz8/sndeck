"""UI-free push orchestration: scope routing + apply. Shared by app.py and cli.py."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .auth import AuthExpiredError
from .records import pull_record
from .sync import build_push_plan, apply_push
from .tree import owner_of_record
from .updatesets import (capture_for_record, current_user, read_pref, set_scope_pointer,
                         set_current_application, update_set_meta)


def set_for_record(model, table: str, sys_id: str) -> tuple[str, str] | None:
    """(raw scope sys_id, owning set sys_id) for the set that stages (table, sys_id) —
    the record-owner lookup push routing needs. The model traversal itself lives in
    tree.owner_of_record; this is the push-side name for it."""
    return owner_of_record(model, table, sys_id)


def scope_for_record(model, table: str, sys_id: str) -> str | None:
    """Raw scope sys_id of the set that stages (table, sys_id); None if unstaged."""
    found = set_for_record(model, table, sys_id)
    return found[0] if found is not None else None


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


def push_all(client, model, record_paths: list) -> list["PushOutcome"]:
    """Push every staged record path. Per-record: build plan, route the record's scope
    pointer to its owning batch member, align the active scope only when it changes,
    start a fresh server session if either pref moved (an open session keeps the prefs
    it read at open, so its PUT would capture by the old ones), apply, verify the capture
    landed in the intended set, then re-pull to refresh the snapshot. A failed record
    becomes a not-pushed outcome and the rest still push; a miscapture is a pushed
    outcome carrying capture_error. AuthExpiredError propagates (never swallowed)."""
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
            if user:
                try:
                    owner = set_for_record(model, plan.table, plan.sys_id)
                    if owner is not None:
                        rec_scope, intended = owner
                        moved = set_scope_pointer(client, user.sys_id, rec_scope, intended)
                        if rec_scope != aligned:
                            set_current_application(client, user.sys_id, rec_scope)
                            routed = rec_scope
                            aligned = rec_scope
                        if moved or routed:
                            client.reset_session()
                    else:
                        intended = read_pref(client, user.sys_id, "sys_update_set")
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
            try:
                pull_record(client, plan.table, plan.sys_id, Path(path).parents[1])
            except LookupError:
                pass
            pushed = True
        except AuthExpiredError:
            raise
        except Exception as e:
            reason = str(e) or "push failed"
            if not name:
                name = Path(path).name
        outcomes.append(PushOutcome(table, sys_id, name, pushed, reason, routed, warning,
                                    capture_error))
    return outcomes


def push_one(client, model, record_path) -> "PushOutcome":
    return push_all(client, model, [record_path])[0]
