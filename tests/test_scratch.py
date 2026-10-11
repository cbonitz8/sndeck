from sndeck.scratch import folder_name


def test_folder_name_is_sanitized_name_plus_sys_id():
    assert folder_name("My BR", "a" * 32) == "My BR__" + "a" * 32


def test_folder_name_sanitizes_slashes():
    assert folder_name("a/b:c", "x") == "a_b_c__x"


def test_folder_name_falls_back_when_blank():
    assert folder_name("", "sid") == "unnamed__sid"
    assert folder_name("  ", "sid") == "unnamed__sid"
