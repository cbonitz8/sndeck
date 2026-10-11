from datetime import date

from sndeck import snapshot
from sndeck.migrate import apply_migration, find_legacy, plan_migration
from sndeck.store import EDIT, Store
from storekit import make_legacy, sid

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
    (_root(tmp_path) / "dev" / "sys_script").mkdir(parents=True)
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
    attic = root / "dev" / "_attic" / "2026-10-20" / "sys_script" / "B__222" / "t2"
    assert (attic / "local.diff").exists() and (attic / "reason.txt").exists()
    assert not keep.exists() and not gone.exists()
    assert report.leftovers == {"t1": ["set-map.json"]}
    assert not (root / "t2").exists()


def test_corrupt_snapshot_is_dirty_and_never_dropped(tmp_path):
    c = _legacy(tmp_path, "t1", S_DONE)
    (c / ".snapshot.json").write_text('{"scr')
    found = find_legacy(_root(tmp_path), ["dev"])
    assert [f.dirty for f in found] == [True]
    lk = FakeLookups(sets={S_DONE: ["dev"]}, states=_states(),
                     live={("dev", "sys_script", "111"): {"script": "other"}})
    steps = plan_migration(found, lk)
    assert [s.action for s in steps] != ["drop"]
