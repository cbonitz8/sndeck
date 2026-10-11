from sndeck.records import dirty_files_from_disk


def test_dirty_files_from_disk_lists_only_dirty_edit_copies_of_the_instance(tmp_path):
    from sndeck.store import Store
    from storekit import make_copy
    d1 = make_copy(tmp_path, "dev", "sys_script_include", "Alpha", "r1", dirty=True)
    d2 = make_copy(tmp_path, "dev", "sys_script", "Beta", "r2", dirty=True)
    make_copy(tmp_path, "dev", "sys_script", "Zeta", "r9")
    make_copy(tmp_path, "dev", "sys_script", "Ref", "r3", kind="ref", dirty=True)
    make_copy(tmp_path, "prod", "sys_script", "Other", "r4", dirty=True)

    out = dirty_files_from_disk(Store(tmp_path), "dev")

    assert [(f.table, f.sys_id) for f in out] == [("sys_script", "r2"),
                                                 ("sys_script_include", "r1")]
    assert [f.record_path for f in out] == [d2, d1]
    assert all(f.dirty and f.local and not f.tracked for f in out)


def test_dirty_files_from_disk_empty(tmp_path):
    from sndeck.store import Store
    assert dirty_files_from_disk(Store(tmp_path), "dev") == []
