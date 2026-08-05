import pytest
from sndeck.deeplinks import instance_url_for
from sndeck.config import Instance

INST = Instance("dev", "https://x.service-now.com/", "cid",
                "https://x.service-now.com/oauth_token.do", "dev")


def test_record_url():
    assert instance_url_for(INST, kind="record", table="sp_widget", sys_id="ABC") == \
        "https://x.service-now.com/sp_widget.do?sys_id=ABC"


def test_update_set_url():
    assert instance_url_for(INST, kind="update_set", sys_id="SET1") == \
        "https://x.service-now.com/sys_update_set.do?sys_id=SET1"


def test_record_requires_table():
    with pytest.raises(ValueError):
        instance_url_for(INST, kind="record", sys_id="ABC")


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        instance_url_for(INST, kind="bogus", sys_id="ABC")
