import json
from datetime import date

import pytest

from sndeck.store import (EDIT, REF, Store, StoreInvariantError, check_store_root,
                          count_legacy)
from storekit import SET_A, make_copy, make_legacy, sid


def test_paths_for_each_kind(tmp_path):
    s = Store(tmp_path)
    assert s.new_path("dev", EDIT, "sys_script", "A b", "111") == \
        tmp_path / "dev" / "sys_script" / "A b__111"
    assert s.new_path("dev", REF, "sys_script", "A", "111") == \
        tmp_path / "dev" / "_ref" / "sys_script" / "A__111"
    assert s.attic_path("dev", date(2026, 10, 10), "sys_script", "A", "111") == \
        tmp_path / "dev" / "_attic" / "2026-10-10" / "sys_script" / "A__111"
    assert s.attic_path(None, date(2026, 10, 10), "sys_script", "A", "111") == \
        tmp_path / "_attic" / "2026-10-10" / "sys_script" / "A__111"


def test_kind_dir_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError, match="unknown copy kind"):
        Store(tmp_path).kind_dir("dev", "scratch")


def test_locate_by_sys_id_suffix_and_kind(tmp_path):
    edit = make_copy(tmp_path, "dev", "sys_script", "Old name", "111")
    ref = make_copy(tmp_path, "dev", "sys_script", "Old name", "111", kind="ref")
    s = Store(tmp_path)
    assert s.locate("dev", "sys_script", "111") == edit
    assert s.locate("dev", "sys_script", "111", REF) == ref
    assert s.locate("prod", "sys_script", "111") is None
    assert s.locate("dev", "sys_script", "999") is None


def test_locate_raises_on_two_folders(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    make_copy(tmp_path, "dev", "sys_script", "B", "111")
    with pytest.raises(StoreInvariantError, match="2 folders"):
        Store(tmp_path).locate("dev", "sys_script", "111")


def test_iter_copies_reads_meta_and_skips_underscore_dirs(tmp_path):
    a = make_copy(tmp_path, "dev", "sys_script", "A", "111")
    make_copy(tmp_path, "dev", "sys_script", "R", "222", kind="ref")
    copies = Store(tmp_path).iter_copies("dev", EDIT)
    assert [(c.table, c.sys_id, c.name, c.path, c.target_set) for c in copies] == \
        [("sys_script", "111", "A", a, SET_A)]
    assert [c.sys_id for c in Store(tmp_path).iter_copies("dev", REF)] == ["222"]
    assert Store(tmp_path).iter_copies("prod", EDIT) == []


def test_check_store_root_accepts_store_and_fresh_dir(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    assert check_store_root(tmp_path, ["dev", "prod"]) is None
    assert check_store_root(tmp_path / "missing", ["dev"]) is None


def test_check_store_root_refuses_set_folder(tmp_path):
    p = tmp_path / f"my set__{sid('a')}"
    assert "update-set folder" in check_store_root(p, ["dev"])


def test_check_store_root_refuses_instance_folder(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    assert "instance folder" in check_store_root(tmp_path / "dev", ["dev"])


def test_check_store_root_refuses_legacy_task_root(tmp_path):
    root = tmp_path / "vendor-hotfix"
    make_legacy(root / f"rel__{sid('b')}", "sys_script", "A", "111")
    msg = check_store_root(root, ["dev"])
    assert "legacy per-task root" in msg and "sndeck migrate" in msg


def test_check_store_root_refuses_records_near_root(tmp_path):
    make_legacy(tmp_path, "sys_script", "A", "111")
    assert "holds record folders" in check_store_root(tmp_path, ["dev"])


def test_count_legacy_ignores_store_copies(tmp_path):
    make_copy(tmp_path, "dev", "sys_script", "A", "111")
    make_legacy(tmp_path / "old" / f"s__{sid('c')}", "sys_script", "B", "222")
    assert count_legacy(tmp_path) == 1


def test_iter_copies_raises_on_record_without_meta(tmp_path):
    bad = tmp_path / "dev" / "sys_script" / "X__999"
    bad.mkdir(parents=True)
    (bad / "record.json").write_text("{}")
    with pytest.raises(KeyError):
        Store(tmp_path).iter_copies("dev")


def test_count_legacy_raises_on_record_without_meta(tmp_path):
    bad = tmp_path / "old" / "sys_script" / "X__999"
    bad.mkdir(parents=True)
    (bad / "record.json").write_text("{}")
    with pytest.raises(KeyError):
        count_legacy(tmp_path)
