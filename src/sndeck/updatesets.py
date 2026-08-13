"""Read update-set state from ServiceNow (current set, drift feed, per-record capture)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_NAME_KEY = re.compile(r"^(?P<table>.+)_(?P<sysid>[0-9a-f]{32})$")


@dataclass(frozen=True)
class UpdateSet:
    sys_id: str
    name: str
    state: str


@dataclass(frozen=True)
class Capture:
    created: str
    type: str
    target: str
    set_name: str
    set_id: str


def current_update_set(client, username: str) -> UpdateSet | None:
    prefs = client.query(
        "sys_user_preference",
        query=f"name=sys_update_set^user.user_name={username}",
        fields=["value"], limit=1)
    if not prefs or not prefs[0].get("value"):
        return None
    set_id = prefs[0]["value"]
    rows = client.query("sys_update_set", query=f"sys_id={set_id}",
                        fields=["sys_id", "name", "state"], limit=1)
    if not rows:
        return None
    r = rows[0]
    return UpdateSet(_raw(r, "sys_id"), _dv(r, "name") or _raw(r, "name"),
                     _dv(r, "state") or _raw(r, "state"))


def _cap(row: dict) -> Capture:
    def dv(f):
        v = row.get(f, {})
        return v.get("display_value", "") if isinstance(v, dict) else (v or "")
    def raw(f):
        v = row.get(f, {})
        return v.get("value", "") if isinstance(v, dict) else (v or "")
    return Capture(created=raw("sys_created_on"), type=dv("type"),
                   target=dv("target_name"), set_name=dv("update_set"), set_id=raw("update_set"))


def recent_captures(client, username: str, hours: int = 2, limit: int = 50) -> list[Capture]:
    q = (f"sys_created_by={username}"
         f"^sys_created_on>=javascript:gs.hoursAgoStart({hours})"
         f"^ORDERBYDESCsys_created_on")
    rows = client.query("sys_update_xml", query=q,
                        fields=["sys_created_on", "type", "target_name", "update_set"],
                        display_value="all", limit=limit)
    return [_cap(r) for r in rows]


def whoami(client) -> str | None:
    """Return the current (token) user's user_name via a read-only query."""
    rows = client.query("sys_user", query="sys_id=javascript:gs.getUserID()",
                        fields=["user_name"], limit=1)
    return rows[0].get("user_name") if rows else None


def update_set_records(client, set_sys_id: str) -> tuple[int, list[tuple[str, str]]]:
    """Return (total change count, unique (table, sys_id) for ALL parseable records in the set)."""
    rows = client.query("sys_update_xml", query=f"update_set={set_sys_id}",
                        fields=["name"], limit=1000)
    total = len(rows)
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        m = _NAME_KEY.match(r.get("name", ""))
        if m:
            key = (m.group("table"), m.group("sysid"))
            if key not in seen:
                seen.add(key)
                out.append(key)
    return total, out


def update_set_summary(client, set_sys_id: str) -> tuple[int, list[tuple[str, str]]]:
    """Return (total change count, unique code-artifact (table, sys_id) records) for a set."""
    from .registry import CODE_ARTIFACTS
    total, recs = update_set_records(client, set_sys_id)
    return total, [(t, s) for (t, s) in recs if t in CODE_ARTIFACTS]


def records_in_update_set(client, set_sys_id: str) -> list[tuple[str, str]]:
    """Return unique (table, sys_id) for code-artifact records captured in a set.
    sys_update_xml.name is '<table>_<sys_id>'; filter to CODE_ARTIFACTS tables."""
    return update_set_summary(client, set_sys_id)[1]


def capture_for_record(client, table: str, sys_id: str) -> Capture | None:
    # NB: assumes the sys_update_xml update name is "<table>_<sys_id>" — holds for the
    # v1 code-artifact registry; verify per new table when the registry grows.
    q = f"name={table}_{sys_id}^ORDERBYDESCsys_created_on"
    rows = client.query("sys_update_xml", query=q,
                        fields=["sys_created_on", "type", "target_name", "update_set"],
                        display_value="all", limit=1)
    return _cap(rows[0]) if rows else None


@dataclass(frozen=True)
class CurrentUser:
    sys_id: str
    user_name: str


@dataclass(frozen=True)
class UpdateSetMeta:
    sys_id: str
    name: str
    state: str
    scope: str


@dataclass(frozen=True)
class Entry:
    table: str
    sys_id: str
    name: str
    type: str


def _raw(row: dict, f: str) -> str:
    v = row.get(f, {})
    return v.get("value", "") if isinstance(v, dict) else (v or "")


def _dv(row: dict, f: str) -> str:
    v = row.get(f, {})
    return v.get("display_value", "") if isinstance(v, dict) else (v or "")


def current_user(client) -> CurrentUser | None:
    rows = client.query("sys_user", query="sys_id=javascript:gs.getUserID()",
                        fields=["sys_id", "user_name"], limit=1)
    if not rows:
        return None
    r = rows[0]
    return CurrentUser(r.get("sys_id", ""), r.get("user_name", ""))


def resolve_current_set(client) -> tuple["CurrentUser | None", "UpdateSet | None"]:
    """The token user and their current update set — the resolution every set-scoped
    pull/push begins with, in both the CLI and the TUI (was copy-pasted at 5 call sites).

    Returns (None, None) when the user can't be resolved, (user, None) when there is a
    user but no current set, and (user, set) otherwise — so callers can word the two
    guard failures distinctly."""
    user = current_user(client)
    if user is None:
        return None, None
    return user, current_update_set(client, user.user_name)


def update_set_meta(client, set_sys_id: str) -> UpdateSetMeta | None:
    rows = client.query("sys_update_set", query=f"sys_id={set_sys_id}",
                        fields=["sys_id", "name", "state", "application"],
                        display_value="all", limit=1)
    if not rows:
        return None
    r = rows[0]
    return UpdateSetMeta(_raw(r, "sys_id"), _dv(r, "name") or _raw(r, "name"),
                         _dv(r, "state") or _raw(r, "state"), _dv(r, "application") or "Global")


def update_set_states(client, sys_ids: list[str]) -> dict[str, UpdateSetMeta]:
    """Resolve name/state/scope for many update sets in ONE query.

    Keyed by sys_id; unresolved ids are absent (treat as gone). `state` is the
    RAW value ('in progress'/'complete'/'ignore') so callers can compare it
    directly; `scope` is the display name.
    """
    ids = [s for s in sys_ids if s]
    if not ids:
        return {}
    q = "sys_idIN" + ",".join(ids)
    rows = client.query("sys_update_set", query=q,
                        fields=["sys_id", "name", "state", "application"],
                        display_value="all", limit=len(ids))
    out: dict[str, UpdateSetMeta] = {}
    for r in rows:
        sid = _raw(r, "sys_id")
        out[sid] = UpdateSetMeta(sid, _dv(r, "name") or _raw(r, "name"),
                                 _raw(r, "state"), _dv(r, "application") or "Global")
    return out


def update_set_entries(client, set_sys_id: str) -> list[Entry]:
    rows = client.query("sys_update_xml", query=f"update_set={set_sys_id}^ORDERBYname",
                        fields=["name", "target_name", "type"], display_value="all", limit=1000)
    out: list[Entry] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        m = _NAME_KEY.match(_raw(r, "name"))
        if not m:
            continue
        key = (m.group("table"), m.group("sysid"))
        if key in seen:
            continue
        seen.add(key)
        name = _dv(r, "target_name") or _raw(r, "target_name") or key[1]
        out.append(Entry(key[0], key[1], name, _dv(r, "type") or _raw(r, "type")))
    return out


def read_pref(client, user_sys_id: str, name: str) -> str | None:
    """The value of the user's preference of this name, or None if unset. The read
    counterpart to _upsert_pref — the single owner of the sys_user_preference query
    shape (push previously hand-built its own copy)."""
    prefs = client.query("sys_user_preference",
                         query=f"name={name}^user={user_sys_id}",
                         fields=["value"], limit=1)
    return prefs[0].get("value") if prefs else None


def _upsert_pref(client, user_sys_id: str, name: str, value: str) -> None:
    """PATCH the user's preference of this name if it exists, else POST a new one."""
    prefs = client.query("sys_user_preference",
                         query=f"name={name}^user={user_sys_id}",
                         fields=["sys_id"], limit=1)
    if prefs:
        client.patch("sys_user_preference", prefs[0]["sys_id"], {"value": value})
    else:
        client.post("sys_user_preference",
                    {"name": name, "user": user_sys_id, "value": value})


_RECENT_ITEMS_PREF = "glide.ui.concourse_picker.recent_items"


def _bump_recent_set(data: dict, set_sys_id: str, display_name: str, limit: int = 5) -> dict:
    """Move the selected set to the front (MRU) of every update-set recents bucket.

    The picker's displayed label is the first entry of the `update-set` recents list,
    so switching the pointer prefs alone leaves the old set showing. Buckets are keyed
    by domain (literally "undefined" when domain separation is off); we bump the set in
    each existing bucket, deduping by sysId and capping the list to mirror the UI.

    Defensive: the pref is written by the real SN UI, not by sndeck, so its shape is
    not under our control. Non-conforming shapes (non-dict top-level, non-dict bucket
    map, non-list bucket items, non-dict entries) are coerced to empty rather than
    raising so a malformed pref never prevents a pointer-pref switch from completing.
    """
    if not isinstance(data, dict):
        data = {}
    entry = {"name": display_name, "sysId": set_sys_id, "path": ""}
    buckets = data.setdefault("update-set", {})
    if not isinstance(buckets, dict):
        buckets = data["update-set"] = {}
    if not buckets:
        buckets["undefined"] = []
    for key, items in list(buckets.items()):
        if not isinstance(items, list):
            items = []
        kept = [e for e in items if isinstance(e, dict) and e.get("sysId") != set_sys_id]
        buckets[key] = [entry] + kept[: limit - 1]
    return data


def _write_recent_items(client, user_sys_id: str, set_sys_id: str, display_name: str) -> None:
    """Update the concourse-picker recents pref so the header displays the switched set."""
    prefs = client.query("sys_user_preference",
                         query=f"name={_RECENT_ITEMS_PREF}^user={user_sys_id}",
                         fields=["sys_id", "value"], limit=1)
    try:
        data = json.loads(prefs[0].get("value") or "{}") if prefs else {}
    except (ValueError, TypeError):
        data = {}
    data = _bump_recent_set(data, set_sys_id, display_name)
    # Compact + non-ASCII preserved, matching how the ServiceNow UI writes this pref.
    value = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if prefs:
        client.patch("sys_user_preference", prefs[0]["sys_id"], {"value": value})
    else:
        client.post("sys_user_preference",
                    {"name": _RECENT_ITEMS_PREF, "user": user_sys_id, "value": value})


def set_current_update_set(client, user_sys_id: str, set_sys_id: str) -> None:
    """Switch the current update set for the given user via sys_user_preference.

    Writes all three things the ServiceNow UI maintains so REST capture and the picker
    agree:
      - `sys_update_set` — what REST-created records capture against.
      - `updateSetForScope<scope>` — the per-scope pointer (`updateSetForScopeglobal`
        for Global, `updateSetForScope<scopeSysId>` for a scoped app).
      - `glide.ui.concourse_picker.recent_items` — the recents list whose front entry
        is the label the header picker displays.
    Pointer prefs are PATCHed if present, else POSTed. Attribution is the token owner —
    no explicit user override is added.
    """
    rows = client.query("sys_update_set", query=f"sys_id={set_sys_id}",
                        fields=["name", "application"], display_value="all", limit=1)
    row = rows[0] if rows else {}
    scope_id = _raw(row, "application") or "global"
    scope_name = _dv(row, "application") or "Global"
    set_name = _dv(row, "name") or _raw(row, "name") or set_sys_id
    _upsert_pref(client, user_sys_id, "sys_update_set", set_sys_id)
    _upsert_pref(client, user_sys_id, f"updateSetForScope{scope_id}", set_sys_id)
    _write_recent_items(client, user_sys_id, set_sys_id, f"{set_name} [{scope_name}]")


def list_update_sets(client, *, state: str = "in progress", offset: int = 0,
                     limit: int = 25) -> list[UpdateSetMeta]:
    q = f"state={state}^ORDERBYDESCsys_updated_on"
    rows = client.query("sys_update_set", query=q,
                        fields=["sys_id", "name", "state", "application"],
                        display_value="all", offset=offset, limit=limit)
    return [UpdateSetMeta(_raw(r, "sys_id"), _dv(r, "name") or _raw(r, "name"),
                          _dv(r, "state") or _raw(r, "state"), _dv(r, "application") or "Global")
            for r in rows]


def batch_members(client, set_sys_id: str) -> list[UpdateSetMeta]:
    """Resolve the batch this set belongs to. Returns the base first, then members.
    A standalone set returns just itself — INCLUDING a set whose `base_update_set`
    field is empty (older sets that were never batched don't self-reference), for
    which the family query below finds nothing. A member's base is followed so
    passing any member yields the whole batch. Never returns [] for a set that
    exists — the set is always a member of its own batch."""
    rows = client.query("sys_update_set", query=f"sys_id={set_sys_id}",
                        fields=["sys_id", "name", "state", "application",
                                "base_update_set", "parent"],
                        display_value="all", limit=1)
    if not rows:
        return []
    self_meta = UpdateSetMeta(
        _raw(rows[0], "sys_id"), _dv(rows[0], "name") or _raw(rows[0], "name"),
        _raw(rows[0], "state"), _raw(rows[0], "application") or "global")
    base = _raw(rows[0], "base_update_set") or set_sys_id
    fam = client.query("sys_update_set", query=f"base_update_set={base}^ORDERBYparent",
                       fields=["sys_id", "name", "state", "application", "parent"],
                       display_value="all", limit=100)
    metas = [UpdateSetMeta(_raw(r, "sys_id"), _dv(r, "name") or _raw(r, "name"),
                           _raw(r, "state"), _raw(r, "application") or "global")
             for r in fam]
    # The family query misses a standalone set with an empty base_update_set (it
    # references no base, so nothing — not even itself — matches). Guarantee the
    # set's own meta is present so callers never get an empty batch for a real set.
    if not any(m.sys_id == set_sys_id for m in metas):
        metas.append(self_meta)
    # base first (parent empty), then members in query order
    metas.sort(key=lambda m: m.sys_id != base)
    return metas


def set_current_application(client, user_sys_id: str, scope_id: str) -> None:
    """Switch the user's active application scope. scope_id is a sys_scope sys_id
    or the literal 'global'. Mirrors set_current_update_set's pref-upsert."""
    _upsert_pref(client, user_sys_id, "apps.current_app", scope_id)


def switch_current_set(client, set_sys_id: str, scope: str = "global") -> bool:
    """Switch the current update set and align the active application scope to it.
    Returns False if the token user can't be resolved (nothing written), else True.

    Deliberately relationship-blind: a set's parent/child (batch) membership is a
    commit-time grouping in ServiceNow, not a 'current set' concept, so switching only
    ever points the chosen set's own prefs — never any sibling's. Per-scope routing for
    multi-member batches lives in the push path (set_scope_pointer), the one place batch
    membership actually affects capture. Pure domain write — no Textual."""
    user = current_user(client)
    if user is None:
        return False
    set_current_update_set(client, user.sys_id, set_sys_id)
    set_current_application(client, user.sys_id, scope)
    return True


def set_scope_pointer(client, user_sys_id: str, scope_id: str, set_sys_id: str) -> None:
    """Point ONE scope's current-update-set pointer at set_sys_id, without touching
    sys_update_set or the header recents.

    updateSetForScope<scopeSysId> ('...global' for Global). This is the per-scope
    capture pointer ServiceNow follows for a record in that scope. The push path uses
    it to route each staged record's capture into the batch member that owns the
    record's scope — this is the ONLY place batch membership legitimately affects
    capture. 'Switching' the current set (set_current_update_set) stays deliberately
    relationship-blind; a batch is only a commit-time grouping, not a current-set
    concept, so there is no 'activate the whole batch' operation."""
    _upsert_pref(client, user_sys_id, f"updateSetForScope{scope_id or 'global'}", set_sys_id)
