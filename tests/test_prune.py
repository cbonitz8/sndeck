import json
from pathlib import Path

import httpx

from sndeck.prune import (
    plan_set_prune,
    plan_orphan_prune,
    KEEP_STATE,
    reconcile_scratch,
    format_prune_report,
    PruneResult,
    PruneWarning,
)
from sndeck.rest import TableClient
from sndeck.config import Instance

HEX = "0123456789abcdef0123456789abcdef"  # 32 hex; vary first char per set


def _make_record(parent, table, name, sysid, *, script="gs.info('x');", dirty=False):
    """Create a pulled-record folder under parent/<table>/<name>__<sysid>."""
    folder = Path(parent) / table / f"{name}__{sysid}"
    folder.mkdir(parents=True)
    fields = {"sys_id": sysid, "name": name, "script": script}
    (folder / "record.json").write_text(json.dumps(
        {"_meta": {"table": table, "sys_id": sysid, "name": name, "pulled_at": "t"}, **fields}))
    (folder / ".snapshot.json").write_text(json.dumps(fields))
    (folder / "script.js").write_text(script + (" // EDITED" if dirty else ""))
    return folder


def _set_dir(root, slug, sysid):
    d = Path(root) / f"{slug}__{sysid}"
    d.mkdir(parents=True)
    return d


def test_plan_keeps_in_progress_and_prunes_complete_clean(tmp_path):
    sid_ip, sid_done = "a" + HEX[1:], "b" + HEX[1:]
    ip = _set_dir(tmp_path, "live", sid_ip);   _make_record(ip, "sys_script", "A", "111")
    done = _set_dir(tmp_path, "shipped", sid_done); _make_record(done, "sys_script", "B", "222")
    dels, warns = plan_set_prune(tmp_path, {sid_ip: "in progress", sid_done: "complete"})
    assert dels == [done] and warns == []


def test_plan_prunes_gone_set(tmp_path):
    sid = "c" + HEX[1:]
    d = _set_dir(tmp_path, "deleted upstream", sid); _make_record(d, "sys_script", "C", "333")
    # states is non-empty (a real response, this set's sid just isn't in it) so the
    # empty-states safety guard doesn't apply here -- absent-from-a-populated-map still
    # means "gone".
    other_sid = "e" + HEX[1:]
    dels, warns = plan_set_prune(tmp_path, {other_sid: "in progress"})  # sid absent = gone
    assert dels == [d] and warns == []


def test_plan_skips_pruning_when_states_query_returns_empty(tmp_path):
    """Guard: an empty set_states response against non-empty on-disk set workspaces means
    'couldn't determine state', not 'everything is gone' -- must not wipe all scratch on a
    spurious/transient empty query."""
    sid = "a" + HEX[1:]
    d = _set_dir(tmp_path, "live", sid)
    _make_record(d, "sys_script", "A", "111")  # clean

    # Spurious/empty states query: guard skips set-pruning entirely, nothing deleted.
    dels, warns = plan_set_prune(tmp_path, {})
    assert dels == [] and warns == []

    # Narrowness check: a populated states map marking the same set "complete" still
    # prunes its clean scratch -- the guard only fires on a wholly empty states map, not
    # as a blanket "never prune".
    dels2, warns2 = plan_set_prune(tmp_path, {sid: "complete"})
    assert dels2 == [d] and warns2 == []


def test_plan_warns_and_keeps_complete_with_dirty_record(tmp_path):
    sid = "d" + HEX[1:]
    d = _set_dir(tmp_path, "shipped but edited", sid)
    _make_record(d, "sys_script", "Clean", "444")
    _make_record(d, "sys_script", "Dirty", "555", dirty=True)
    dels, warns = plan_set_prune(tmp_path, {sid: "complete"})
    assert dels == []
    assert len(warns) == 1
    w = warns[0]
    assert w.scope == "set" and w.label == "shipped but edited" and w.state == "complete"
    assert "sys_script/Dirty" in w.detail


def test_plan_set_prune_returns_structured_warning(tmp_path):
    sid = "f" + HEX[1:]
    d = _set_dir(tmp_path, "shipped but edited", sid)
    _make_record(d, "sys_script", "Dirty", "555", dirty=True)
    dels, warns = plan_set_prune(tmp_path, {sid: "complete"})
    assert dels == []
    assert warns[0].scope == "set" and warns[0].state == "complete"
    assert "Dirty" in warns[0].detail


def test_orphan_plan_prunes_clean_keeps_dirty_and_ignores_set_dirs(tmp_path):
    # flat-root orphans (directly under root/<table>/<rec>)
    _make_record(tmp_path, "sp_widget", "OldClean", "aaa")
    _make_record(tmp_path, "sp_widget", "OldDirty", "bbb", dirty=True)
    # a set-dir record must NOT be treated as an orphan
    sd = _set_dir(tmp_path, "live", "e" + HEX[1:])
    _make_record(sd, "sys_script", "InSet", "ccc")

    dels, warns = plan_orphan_prune(tmp_path)

    assert dels == [tmp_path / "sp_widget" / "OldClean__aaa"]
    assert len(warns) == 1
    assert warns[0].scope == "orphan" and warns[0].label == "sp_widget/OldDirty"


# Test helpers for reconcile_scratch
_INST = Instance(
    "dev",
    "https://x.service-now.com",
    "cid",
    "https://x.service-now.com/oauth_token.do",
    "cbonitz@x",
)


class _FakeToken:
    def access_token(self):
        return "AT"

    def invalidate(self):
        pass


def _client(rows_by_table):
    def handler(req):
        table = str(req.url.path).rsplit("/", 1)[-1]
        return httpx.Response(200, json={"result": rows_by_table.get(table, [])})

    return TableClient(
        _INST,
        _FakeToken(),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_reconcile_deletes_shipped_and_orphans_keeps_live_and_dirty(tmp_path):
    sid_ip, sid_done = "a" + HEX[1:], "b" + HEX[1:]
    ip = _set_dir(tmp_path, "live", sid_ip)
    _make_record(ip, "sys_script", "A", "111")
    done = _set_dir(tmp_path, "shipped", sid_done)
    _make_record(done, "sys_script", "B", "222")
    _make_record(tmp_path, "sp_widget", "OldClean", "aaa")  # orphan, clean

    # SN reports sid_ip in progress, sid_done complete
    client = _client(
        {
            "sys_update_set": [
                {"sys_id": sid_ip, "name": "live", "state": "in progress"},
                {"sys_id": sid_done, "name": "shipped", "state": "complete"},
            ]
        }
    )
    result = reconcile_scratch(client, tmp_path)

    assert done in result.pruned_sets and ip not in result.pruned_sets
    assert (tmp_path / "sp_widget" / "OldClean__aaa") in result.pruned_orphans
    assert not done.exists()  # actually deleted from disk
    assert ip.exists()  # live set kept
    assert not (tmp_path / "sp_widget" / "OldClean__aaa").exists()


def test_reconcile_empty_when_no_scratch(tmp_path):
    result = reconcile_scratch(_client({}), tmp_path)
    assert result == PruneResult([], [], [])
    assert format_prune_report(result) == []


def test_format_report_lists_counts_and_warnings():
    r = PruneResult(
        pruned_sets=[Path("/s/shipped__x")],
        pruned_orphans=[Path("/s/sp_widget/O__y")],
        warnings=[PruneWarning("set", "z", "complete", "a/b")],
    )
    lines = format_prune_report(r)
    assert any("shipped" in ln and "1" in ln for ln in lines)
    assert any("orphan" in ln.lower() and "1" in ln for ln in lines)
    assert "⚠ set 'z' is complete but has unpushed edits (a/b) — not pruned" in lines


def test_format_prune_report_renders_warning_string():
    from sndeck.prune import PruneResult, PruneWarning, format_prune_report
    r = PruneResult([], [], [PruneWarning("set", "shipped but edited", "complete", "sys_script/Dirty")])
    lines = format_prune_report(r)
    assert "⚠ set 'shipped but edited' is complete but has unpushed edits (sys_script/Dirty) — not pruned" in lines
