from datetime import date

import pytest

from sndeck import snapshot
from sndeck.migrate import apply_migration, find_legacy, plan_migration
from sndeck.store import EDIT, Store
from storekit import make_copy, make_legacy, sid

S_IP, S_IP2, S_DONE = sid("a"), sid("b"), sid("c")


class FakeLookups:
    def __init__(self, sets=None, records=None, states=None, capture=None, live=None):
        self.sets = sets or {}
        self.records = records or {}
        self.states = states or {}
        self.capture = capture or {}
        self.live = live or {}

    def set_homes(self, s):
        return self.sets.get(s, [])

    def record_homes(self, t, s):
        return self.records.get((t, s), [])

    def set_meta(self, i, s):
        return self.states.get((i, s))

    def newest_capture_set(self, i, t, s):
        return self.capture.get((i, t, s))

    def live_fields(self, i, t, s):
        return self.live.get((i, t, s))


def _root(tmp_path):
    return tmp_path / "store"


def _legacy(tmp_path, task, set_id, name="A", sys_id="111", **kw):
    return make_legacy(_root(tmp_path) / task / f"set__{set_id}", "sys_script", name, sys_id, **kw)


def _states():
    return {("dev", S_IP): ("in progress", "Live"), ("dev", S_IP2): ("in progress", "Live 2"),
            ("dev", S_DONE): ("complete", "Shipped")}


def test_find_legacy_reads_nearest_set_and_skips_store(tmp_path):
    a = _legacy(tmp_path, "t1", S_IP)
    nested = make_legacy(_root(tmp_path) / "t2" / f"o__{S_DONE}" / f"i__{S_IP}",
                         "sys_script", "B", "222")
    make_copy(_root(tmp_path), "dev", "sys_script", "S", sid("3"))
    make_legacy(_root(tmp_path) / "_attic" / "2026-10-01", "sys_script", "Z", sid("4"))
    found = {c.path: c.set_sys_id for c in find_legacy(_root(tmp_path), ["dev", "prod"])}
    assert found == {a: S_IP, nested: S_IP}


def test_clean_dupes_keep_one_targeting_newest_capture(tmp_path):
    _legacy(tmp_path, "t1", S_IP, pulled_at="2026-10-01T00:00:00+00:00")
    _legacy(tmp_path, "t2", S_IP2, pulled_at="2026-10-05T00:00:00+00:00")
    lk = FakeLookups(sets={S_IP: ["dev"], S_IP2: ["dev"]}, states=_states(),
                     capture={("dev", "sys_script", "111"): S_IP})
    steps = plan_migration(find_legacy(_root(tmp_path), ["dev"]), lk)
    kept = [s for s in steps if s.action == "store"]
    assert len(kept) == 1 and kept[0].target_set == {"sys_id": S_IP, "name": "Live"}
    assert sorted(s.action for s in steps) == ["drop", "store"]


def test_clean_only_shipped_drops_all(tmp_path):
    _legacy(tmp_path, "t1", S_DONE)
    lk = FakeLookups(sets={S_DONE: ["dev"]}, states=_states())
    assert [s.action for s in plan_migration(find_legacy(_root(tmp_path), ["dev"]), lk)] == ["drop"]


def test_single_dirty_in_progress_is_stored_dirty(tmp_path):
    _legacy(tmp_path, "t1", S_IP, dirty=True)
    _legacy(tmp_path, "t2", S_IP2)
    lk = FakeLookups(sets={S_IP: ["dev"], S_IP2: ["dev"]}, states=_states())
    steps = plan_migration(find_legacy(_root(tmp_path), ["dev"]), lk)
    store_step = next(s for s in steps if s.action == "store")
    assert store_step.copy.dirty and store_step.target_set["sys_id"] == S_IP


def test_single_dirty_shipped_live_equal_drops(tmp_path):
    _legacy(tmp_path, "t1", S_DONE, dirty=True)
    lk = FakeLookups(sets={S_DONE: ["dev"]}, states=_states(),
                     live={("dev", "sys_script", "111"): {"script": "gs.info('x'); // EDITED"}})
    steps = plan_migration(find_legacy(_root(tmp_path), ["dev"]), lk)
    assert [s.action for s in steps] == ["drop"] and "already live" in steps[0].reason


def test_single_dirty_shipped_differing_goes_to_attic(tmp_path):
    _legacy(tmp_path, "t1", S_DONE, dirty=True)
    lk = FakeLookups(sets={S_DONE: ["dev"]}, states=_states(),
                     live={("dev", "sys_script", "111"): {"script": "other"}})
    steps = plan_migration(find_legacy(_root(tmp_path), ["dev"]), lk)
    assert [s.action for s in steps] == ["attic"] and "+gs.info" in steps[0].diff


def test_two_different_dirty_copies_both_attic(tmp_path):
    _legacy(tmp_path, "t1", S_IP, dirty=True)
    _legacy(tmp_path, "t2", S_IP2, dirty=True, script="other")
    lk = FakeLookups(sets={S_IP: ["dev"], S_IP2: ["dev"]}, states=_states(),
                     live={("dev", "sys_script", "111"): {"script": "live"}})
    steps = plan_migration(find_legacy(_root(tmp_path), ["dev"]), lk)
    assert sorted(s.action for s in steps) == ["attic", "attic"]


def test_unknown_instance_goes_to_attic(tmp_path):
    _legacy(tmp_path, "t1", S_IP)
    steps = plan_migration(find_legacy(_root(tmp_path), ["dev"]), FakeLookups())
    assert steps[0].action == "attic" and steps[0].instance is None
    assert "no instance" in steps[0].reason


def test_record_lookup_on_both_instances_is_ambiguous(tmp_path):
    _legacy(tmp_path, "t1", S_IP)
    lk = FakeLookups(records={("sys_script", "111"): ["dev", "prod"]})
    steps = plan_migration(find_legacy(_root(tmp_path), ["dev"]), lk)
    assert steps[0].action == "attic" and "dev and prod" in steps[0].reason


def test_apply_writes_store_attic_and_removes_legacy(tmp_path):
    root = _root(tmp_path)
    keep = _legacy(tmp_path, "t1", S_IP, dirty=True)
    gone = _legacy(tmp_path, "t2", S_DONE, name="B", sys_id="222", dirty=True)
    (root / "t1" / "set-map.json").write_text("{}")
    lk = FakeLookups(sets={S_IP: ["dev"], S_DONE: ["dev"]}, states=_states(),
                     live={("dev", "sys_script", "222"): {"script": "other"}})
    s = Store(root)
    report = apply_migration(plan_migration(find_legacy(root, ["dev"]), lk), s, date(2026, 10, 20))
    stored = s.locate("dev", "sys_script", "111", EDIT)
    assert snapshot.read_meta_extra(stored)["target_set"] == {"sys_id": S_IP, "name": "Live"}
    assert snapshot.is_dirty(stored)
    attic = root / "dev" / "_attic" / "2026-10-20" / "sys_script" / "B__222" / "t2" / f"set__{S_DONE}"
    assert (attic / "local.diff").exists() and (attic / "reason.txt").exists()
    assert not keep.exists() and not gone.exists()
    assert report.leftovers == {"t1": ["set-map.json"]}
    assert (root / "t2").is_dir() and not any((root / "t2").iterdir())


def test_corrupt_snapshot_is_dirty_and_never_dropped(tmp_path):
    c = _legacy(tmp_path, "t1", S_DONE)
    (c / ".snapshot.json").write_text('{"scr')
    found = find_legacy(_root(tmp_path), ["dev"])
    assert [f.dirty for f in found] == [True]
    lk = FakeLookups(sets={S_DONE: ["dev"]}, states=_states(),
                     live={("dev", "sys_script", "111"): {"script": "other"}})
    steps = plan_migration(found, lk)
    assert [s.action for s in steps] == ["attic"]


def _two_conflicting(tmp_path):
    a = _legacy(tmp_path, "t1", S_IP, dirty=True)
    b = _legacy(tmp_path, "t1", S_IP2, dirty=True, script="other")
    lk = FakeLookups(sets={S_IP: ["dev"], S_IP2: ["dev"]}, states=_states(),
                     live={("dev", "sys_script", "111"): {"script": "live"}})
    return a, b, plan_migration(find_legacy(_root(tmp_path), ["dev"]), lk)


def test_conflicting_copies_in_one_task_root_get_separate_attic_folders(tmp_path):
    _a, _b, steps = _two_conflicting(tmp_path)
    apply_migration(steps, Store(_root(tmp_path)), date(2026, 10, 20))
    base = _root(tmp_path) / "dev" / "_attic" / "2026-10-20" / "sys_script" / "A__111" / "t1"
    for set_id in (S_IP, S_IP2):
        d = base / f"set__{set_id}"
        assert (d / "local.diff").exists() and (d / "reason.txt").exists()


def test_failed_apply_rolls_back_and_keeps_legacy(tmp_path, monkeypatch):
    import sndeck.migrate as migrate
    a, b, steps = _two_conflicting(tmp_path)
    real = migrate.shutil.copytree
    calls = []

    def flaky(src, dst, *args, **kw):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(src, dst, *args, **kw)

    monkeypatch.setattr(migrate.shutil, "copytree", flaky)
    with pytest.raises(OSError):
        apply_migration(steps, Store(_root(tmp_path)), date(2026, 10, 20))
    assert a.exists() and b.exists()
    base = _root(tmp_path) / "dev" / "_attic" / "2026-10-20" / "sys_script" / "A__111" / "t1"
    assert not any(base.glob("set__*"))
    monkeypatch.setattr(migrate.shutil, "copytree", real)
    apply_migration(steps, Store(_root(tmp_path)), date(2026, 10, 20))


def test_existing_destination_refuses_before_writing(tmp_path):
    a, b, steps = _two_conflicting(tmp_path)
    s = Store(_root(tmp_path))
    pre = s.attic_path("dev", date(2026, 10, 20), "sys_script", "A", "111") / "t1" / f"set__{S_IP}"
    pre.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="nothing was migrated"):
        apply_migration(steps, s, date(2026, 10, 20))
    assert a.exists() and b.exists()
    assert not (pre.parent / f"set__{S_IP2}").exists()


def test_group_resolved_to_prod_never_becomes_an_edit_copy(tmp_path):
    _legacy(tmp_path, "t1", S_IP)
    _legacy(tmp_path, "t2", S_IP2, dirty=True)
    lk = FakeLookups(sets={S_IP: ["prod"], S_IP2: ["prod"]},
                     states={("prod", S_IP): ("in progress", "Live"),
                             ("prod", S_IP2): ("in progress", "Live 2")},
                     live={("prod", "sys_script", "111"): {"script": "live"}})
    steps = plan_migration(find_legacy(_root(tmp_path), ["dev", "prod"]), lk)
    by_action = {s.action: s for s in steps}
    assert sorted(by_action) == ["attic", "drop"]
    assert not by_action["drop"].copy.dirty
    assert "read copy from prod" in by_action["drop"].reason
    assert "sndeck pull --ref" in by_action["drop"].reason
    assert by_action["attic"].copy.dirty and by_action["attic"].instance == "prod"
    assert "unpushed edit against prod" in by_action["attic"].reason
    assert "+gs.info" in by_action["attic"].diff and "-live" in by_action["attic"].diff


def _apply(tmp_path, lk, day=date(2026, 10, 20)):
    root = _root(tmp_path)
    return apply_migration(plan_migration(find_legacy(root, ["dev"]), lk), Store(root), day)


def test_check_store_root_refuses_task_folder_emptied_by_migrate():
    import os
    from pathlib import Path
    from sndeck.settings import DEFAULT_STORE_ROOT
    from sndeck.store import check_store_root
    root = Path(os.path.expanduser(DEFAULT_STORE_ROOT))
    task = root / "vendor-hotfix"
    make_legacy(task / f"rel__{S_DONE}", "sys_script", "A", "111")
    (task / "notes.md").write_text("notes")
    assert "legacy per-task root" in check_store_root(task, ["dev"])
    lk = FakeLookups(sets={S_DONE: ["dev"]}, states=_states())
    apply_migration(plan_migration(find_legacy(root, ["dev"]), lk), Store(root),
                    date(2026, 10, 20))
    assert sorted(p.name for p in task.iterdir()) == ["notes.md"]
    msg = check_store_root(task, ["dev"])
    assert msg is not None and "inside the default store root" in msg and str(root) in msg
    assert "inside the default store root" in check_store_root(root / "not-made-yet", ["dev"])
    assert check_store_root(root, ["dev"]) is None
    work = Path.home() / "work"
    (work / "dev").mkdir(parents=True)
    assert check_store_root(work, ["prod"]) is None


def test_count_legacy_after_attic_apply_is_zero_and_reconcile_is_quiet(tmp_path, sn_client):
    from sndeck.gc import reconcile_and_report
    from sndeck.store import count_legacy
    root = _root(tmp_path)
    _legacy(tmp_path, "t1", S_DONE, dirty=True)
    _legacy(tmp_path, "t2", sid("e"), name="U", sys_id="999")
    lk = FakeLookups(sets={S_DONE: ["dev"]}, states=_states(),
                     live={("dev", "sys_script", "111"): {"script": "other"}})
    report = _apply(tmp_path, lk)
    assert report.atticked == 2
    assert list((root / "_attic").rglob("record.json"))
    assert list((root / "dev" / "_attic").rglob("record.json"))
    assert count_legacy(root, ["dev"]) == 0
    lines = reconcile_and_report(sn_client(lambda table, params: []), Store(root))
    assert not any("legacy" in line for line in lines)


def test_apply_keeps_empty_user_folders_and_the_task_root(tmp_path):
    root = _root(tmp_path)
    _legacy(tmp_path, "t1", S_DONE)
    (root / "t1" / "backups").mkdir()
    (root / "t1" / f"set__{S_DONE}" / "drafts").mkdir()
    _apply(tmp_path, FakeLookups(sets={S_DONE: ["dev"]}, states=_states()))
    assert (root / "t1" / "backups").is_dir()
    assert (root / "t1" / f"set__{S_DONE}" / "drafts").is_dir()
    assert not (root / "t1" / f"set__{S_DONE}" / "sys_script").exists()


def test_apply_with_a_legacy_folder_nested_in_another(tmp_path):
    outer = _legacy(tmp_path, "t1", S_DONE)
    inner = make_legacy(outer, "sys_script", "B", "222")
    lk = FakeLookups(sets={S_DONE: ["dev"]}, states=_states(),
                     records={("sys_script", "222"): ["dev"]})
    report = _apply(tmp_path, lk)
    assert report.atticked == 1 and report.dropped == 1
    assert not outer.exists() and not inner.exists()


def test_clean_copy_with_user_files_goes_to_attic_not_dropped(tmp_path):
    c = _legacy(tmp_path, "t1", S_DONE)
    (c / "script.js.bak").write_text("old")
    lk = FakeLookups(sets={S_DONE: ["dev"]}, states=_states())
    steps = plan_migration(find_legacy(_root(tmp_path), ["dev"]), lk)
    assert [s.action for s in steps] == ["attic"]
    assert "script.js.bak" in steps[0].reason
    _apply(tmp_path, lk)
    kept = list((_root(tmp_path) / "dev" / "_attic").rglob("script.js.bak"))
    assert [p.read_text() for p in kept] == ["old"]
