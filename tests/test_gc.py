from datetime import date, datetime, timezone

from sndeck.gc import apply_gc, format_gc_report, plan_for, plan_gc, reconcile_and_report
from sndeck.store import Store
from storekit import SET_A, make_copy, make_legacy, sid

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
SET_B = {"sys_id": sid("b"), "name": "Set B"}


def _live(script):
    return lambda table, sys_id: {"sys_id": sys_id, "script": script}


def _classes(actions):
    return sorted((a.cls, a.path.name) for a in actions)


def test_shipped_clean_is_c1_and_in_progress_kept(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "A", "111", target_set=SET_A)
    make_copy(tmp_path, "dev", "sys_script", "B", "222", target_set=SET_B)
    acts = plan_gc(Store(tmp_path), "dev",
                   {SET_A["sys_id"]: "in progress", SET_B["sys_id"]: "complete"},
                   _live("gs.info('x');"), NOW)
    assert _classes(acts) == [("C1", "B__222")]


def test_shipped_dirty_equal_to_live_is_c2(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "B", "222", target_set=SET_B, dirty=True)
    acts = plan_gc(Store(tmp_path), "dev", {SET_B["sys_id"]: "ignore"},
                   _live("gs.info('x'); // EDITED"), NOW)
    assert _classes(acts) == [("C2", "B__222")]


def test_shipped_dirty_differing_is_c3_with_diff(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "B", "222", target_set=SET_B, dirty=True)
    acts = plan_gc(Store(tmp_path), "dev", {SET_B["sys_id"]: "complete"},
                   _live("gs.info('x');"), NOW)
    assert _classes(acts) == [("C3", "B__222")]
    assert "+gs.info('x'); // EDITED" in acts[0].diff


def test_shipped_dirty_record_gone_is_c3(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "B", "222", target_set=SET_B, dirty=True)
    acts = plan_gc(Store(tmp_path), "dev", {SET_B["sys_id"]: "complete"},
                   lambda t, s: None, NOW)
    assert acts[0].cls == "C3" and "no longer exists" in acts[0].detail


def test_target_missing_from_populated_states_is_c4(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "B", "222", target_set=SET_B)
    acts = plan_gc(Store(tmp_path), "dev", {SET_A["sys_id"]: "in progress"},
                   _live(""), NOW)
    assert _classes(acts) == [("C4", "B__222")]


def test_plan_gc_empty_states_reaps_nothing(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "B", "222", target_set=SET_B)
    assert plan_gc(Store(tmp_path), "dev", {}, _live(""), NOW) == []


def test_expired_ref_is_c5(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "Old", "333", kind="ref",
              pulled_at="2026-10-01T00:00:00+00:00")
    make_copy(tmp_path, "dev", "sys_script", "New", "444", kind="ref",
              pulled_at="2026-10-15T00:00:00+00:00")
    acts = plan_gc(Store(tmp_path), "dev", {}, _live(""), NOW)
    assert _classes(acts) == [("C5", "Old__333")]


def test_old_attic_only_with_threshold(tmp_path):
    (tmp_path / "dev" / "_attic" / "2026-09-01").mkdir(parents=True)
    (tmp_path / "dev" / "_attic" / "2026-10-19").mkdir(parents=True)
    assert plan_gc(Store(tmp_path), "dev", {}, _live(""), NOW) == []
    acts = plan_gc(Store(tmp_path), "dev", {}, _live(""), NOW, attic_older_than_days=30)
    assert _classes(acts) == [("C6", "2026-09-01")]


def test_apply_auto_runs_c1_c2_only(tmp_path):
    s = Store(tmp_path)
    c1 = make_copy(tmp_path, "dev", "sys_script", "A", "111", target_set=SET_B)
    c3 = make_copy(tmp_path, "dev", "sys_script", "C", "333", target_set=SET_B, dirty=True)
    acts = plan_gc(s, "dev", {SET_B["sys_id"]: "complete"}, _live("gs.info('x');"), NOW)
    applied = apply_gc(acts, s, "dev", date(2026, 10, 20), auto=True)
    assert [a.cls for a in applied] == ["C1"]
    assert not c1.exists() and c3.exists()


def test_apply_explicit_moves_c3_to_attic_with_diff(tmp_path):
    s = Store(tmp_path)
    c3 = make_copy(tmp_path, "dev", "sys_script", "C", "333", target_set=SET_B, dirty=True)
    acts = plan_gc(s, "dev", {SET_B["sys_id"]: "complete"}, _live("gs.info('x');"), NOW)
    apply_gc(acts, s, "dev", date(2026, 10, 20), auto=False)
    dest = tmp_path / "dev" / "_attic" / "2026-10-20" / "sys_script" / "C__333"
    assert not c3.exists() and (dest / "script.js").exists()
    assert "// EDITED" in (dest / "local.diff").read_text()


def test_report_lines(tmp_path):
    s = Store(tmp_path)
    make_copy(tmp_path, "dev", "sys_script", "A", "111", target_set=SET_B)
    make_copy(tmp_path, "dev", "sys_script", "C", "333", target_set=SET_B, dirty=True)
    acts = plan_gc(s, "dev", {SET_B["sys_id"]: "complete"}, _live("gs.info('x');"), NOW)
    applied = apply_gc(acts, s, "dev", date(2026, 10, 20), auto=True)
    lines = format_gc_report(acts, applied, "dev")
    assert lines[0] == "gc: removed 1 copy whose set shipped: sys_script/A"
    assert lines[1] == "gc: 1 action waits for `sndeck gc --apply` (attic 1)"


def test_reconcile_never_raises_and_reports_legacy(tmp_path, sn_client):
    make_legacy(tmp_path / "old" / f"s__{sid('c')}", "sys_script", "L", "555")
    def routes(table, params):
        raise RuntimeError("boom")
    lines = reconcile_and_report(sn_client(routes), Store(tmp_path))
    assert lines == ["1 legacy record folder under " + str(tmp_path)
                     + " — run `sndeck migrate`"]


def test_corrupt_snapshot_is_not_c1(tmp_path):
    c = make_copy(tmp_path, "dev", "sys_script", "B", "222", target_set=SET_B, dirty=True)
    (c / ".snapshot.json").write_text('{"scr')
    states = {SET_B["sys_id"]: "complete"}
    differing = plan_gc(Store(tmp_path), "dev", states, _live("gs.info('x');"), NOW)
    assert _classes(differing) == [("C3", "B__222")]
    equal = plan_gc(Store(tmp_path), "dev", states, _live("gs.info('x'); // EDITED"), NOW)
    assert _classes(equal) == [("C2", "B__222")]


def test_attic_threshold_must_be_positive(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        plan_gc(Store(tmp_path), "dev", {}, _live(""), NOW, attic_older_than_days=0)


def test_today_attic_folder_never_c6(tmp_path):
    (tmp_path / "dev" / "_attic" / "2026-10-20").mkdir(parents=True)
    assert plan_gc(Store(tmp_path), "dev", {}, _live(""), NOW, attic_older_than_days=1) == []


def test_same_day_c3_twice_keeps_both(tmp_path):
    s = Store(tmp_path)
    states = {SET_B["sys_id"]: "complete"}
    for script in ("one", "two"):
        make_copy(tmp_path, "dev", "sys_script", "C", "333", target_set=SET_B, dirty=True)
        acts = plan_gc(s, "dev", states, _live("gs.info('x');"), NOW)
        apply_gc(acts, s, "dev", date(2026, 10, 20), auto=False)
    day = tmp_path / "dev" / "_attic" / "2026-10-20" / "sys_script"
    assert sorted(p.name for p in day.iterdir()) == ["C__333", "C__333-2"]
    for p in day.iterdir():
        assert (p / "local.diff").exists() and not (p / "C__333").exists()


def test_non_date_attic_folder_skipped(tmp_path):
    (tmp_path / "dev" / "_attic" / "notes").mkdir(parents=True)
    (tmp_path / "dev" / "_attic" / "2026-09-01").mkdir()
    acts = plan_gc(Store(tmp_path), "dev", {}, _live(""), NOW, attic_older_than_days=30)
    assert _classes(acts) == [("C6", "2026-09-01")]


def test_plan_for_reads_set_states_and_attic_threshold_from_client(tmp_path, sn_client):
    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    (tmp_path / "dev" / "_attic" / "2026-01-01").mkdir(parents=True)

    def routes(table, params):
        if table == "sys_update_set":
            return [{"sys_id": {"value": SET_A["sys_id"]}, "name": {"value": "Set A"},
                     "state": {"value": "complete"}, "application": {"display_value": "Global"}}]
        return []
    actions = plan_for(sn_client(routes), Store(tmp_path), attic_older_than_days=7)
    assert sorted(a.cls for a in actions) == ["C1", "C6"]
    assert [a.cls for a in plan_for(sn_client(routes), Store(tmp_path))] == ["C1"]


def test_reconcile_and_report_auto_deletes_only_clean_shipped_copies(tmp_path, sn_client):
    clean = make_copy(tmp_path, "dev", "sys_script", "Clean", sid("1"), target_set=SET_B)
    dirty = make_copy(tmp_path, "dev", "sys_script", "Dirty", sid("2"), target_set=SET_B,
                      dirty=True)
    expired = make_copy(tmp_path, "dev", "sys_script", "Old", sid("3"), kind="ref",
                        pulled_at="2026-01-01T00:00:00+00:00")

    def routes(table, params):
        if table == "sys_update_set":
            return [{"sys_id": SET_B["sys_id"], "name": "Set B", "state": "complete",
                     "application": "Global"}]
        if table == "sys_script":
            return [{"sys_id": sid("2"), "script": "live differs"}]
        return []

    lines = reconcile_and_report(sn_client(routes), Store(tmp_path))

    assert not clean.exists()
    assert dirty.exists() and expired.exists()
    assert any("Clean" in line and "removed" in line for line in lines)
