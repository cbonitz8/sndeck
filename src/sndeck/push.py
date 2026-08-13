"""UI-free push orchestration: scope routing + apply. Shared by app.py and cli.py."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .auth import AuthExpiredError
from .records import pull_record
from .sync import build_push_plan, apply_push
from .tree import owner_of_record
from .updatesets import current_user, set_scope_pointer, set_current_application


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


def _current_scope_pref(client, user_sys_id: str) -> str | None:
    prefs = client.query("sys_user_preference",
                         query=f"name=apps.current_app^user={user_sys_id}",
                         fields=["value"], limit=1)
    return prefs[0].get("value") if prefs else None


def push_all(client, model, record_paths: list) -> list["PushOutcome"]:
    """Push every staged record path. Per-record: build plan, route the record's scope
    pointer to its owning batch member, align the active scope only when it changes,
    apply, then re-pull to refresh the snapshot. A failed record becomes a not-pushed
    outcome and the rest still push. AuthExpiredError propagates (never swallowed)."""
    user = current_user(client)
    aligned = _current_scope_pref(client, user.sys_id) if user else None
    outcomes: list[PushOutcome] = []
    for path in record_paths:
        table = sys_id = name = ""
        routed = warning = reason = None
        pushed = False
        try:
            plan = build_push_plan(client, path)
            table, sys_id, name = plan.table, plan.sys_id, plan.name
            if user:
                try:
                    owner = set_for_record(model, plan.table, plan.sys_id)
                    if owner is not None:
                        rec_scope, owner_set = owner
                        set_scope_pointer(client, user.sys_id, rec_scope, owner_set)
                        if rec_scope != aligned:
                            set_current_application(client, user.sys_id, rec_scope)
                            routed = rec_scope
                            aligned = rec_scope
                except Exception:
                    warning = f"could not route scope before pushing {plan.table}/{plan.sys_id}"
            apply_push(client, plan)
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
        outcomes.append(PushOutcome(table, sys_id, name, pushed, reason, routed, warning))
    return outcomes


def push_one(client, model, record_path) -> "PushOutcome":
    return push_all(client, model, [record_path])[0]
