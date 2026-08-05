from sndeck.registry import CODE_ARTIFACTS, field_extension, ArtifactType


def test_core_script_tables_registered():
    for t in ("sys_script", "sys_script_include", "sys_script_client",
              "sys_ui_action", "sys_ui_script", "sys_ws_operation"):
        assert t in CODE_ARTIFACTS
        assert isinstance(CODE_ARTIFACTS[t], ArtifactType)


def test_business_rule_extracts_script_field():
    assert CODE_ARTIFACTS["sys_script"].script_fields == ("script",)
    assert CODE_ARTIFACTS["sys_script"].folder_record is False


def test_widget_is_folder_record_with_many_fields():
    w = CODE_ARTIFACTS["sp_widget"]
    assert w.folder_record is True
    assert "template" in w.script_fields and "client_script" in w.script_fields


def test_ng_template_extracts_template_field_as_html():
    art = CODE_ARTIFACTS["sp_ng_template"]
    assert art.table == "sp_ng_template"
    assert art.script_fields == ("template",)
    assert field_extension("template") == ".html"


def test_field_extension_maps_by_name():
    assert field_extension("template") == ".html"
    assert field_extension("css") == ".scss"
    assert field_extension("client_script") == ".js"
    assert field_extension("script") == ".js"
    assert field_extension("option_schema") == ".json"
    assert field_extension("unknown_field") == ".txt"
