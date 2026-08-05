import json
from pathlib import Path
from sndeck import preview
from sndeck.tree import FileNode
from sndeck.registry import field_extension


def _node(record_path, table, sys_id="a"*32, name="Rec", local=True):
    return FileNode(table, sys_id, name, in_current_set=True, tracked=True, local=local,
                    dirty=False, record_path=record_path)


def _folder(tmp_path, table, fields, record=None):
    folder = tmp_path / table / f"Rec__{'a'*32}"
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps(record or {"_meta": {"table": table}}))
    for f, v in fields.items():
        (folder / f"{f}{field_extension(f)}").write_text(v)
    return folder


def test_single_field_code_record(tmp_path):
    folder = _folder(tmp_path, "sys_script", {"script": "gs.info('x')"})
    p = preview.build_preview(_node(folder, "sys_script"))
    assert [f.label for f in p.fields] == ["Server"]
    assert p.default_key == "script"
    assert "sys_script" in p.header and "Rec" in p.header
    assert preview.read_field(p.fields[0]) == "gs.info('x')"


def test_widget_multi_field_sub_selector_default_server(tmp_path):
    folder = _folder(tmp_path, "sp_widget",
                     {"script": "S", "client_script": "C", "template": "<div/>", "css": "a{}"})
    p = preview.build_preview(_node(folder, "sp_widget"))
    assert p.default_key == "script"
    assert [f.label for f in p.fields] == ["Server", "Client", "HTML", "CSS"]


def test_ui_page_defaults_html(tmp_path):
    folder = _folder(tmp_path, "sys_ui_page", {"html": "<x/>", "client_script": "c"})
    p = preview.build_preview(_node(folder, "sys_ui_page"))
    assert p.default_key == "html"


def test_missing_field_files_omitted(tmp_path):
    folder = _folder(tmp_path, "sp_widget", {"script": "S"})  # only server present
    p = preview.build_preview(_node(folder, "sp_widget"))
    assert [f.key for f in p.fields] == ["script"]


def test_non_code_record_shows_record_json(tmp_path):
    folder = _folder(tmp_path, "sys_properties", {}, record={"_meta": {"table": "sys_properties"}, "value": "42"})
    p = preview.build_preview(_node(folder, "sys_properties"))
    assert [f.label for f in p.fields] == ["record.json"]
    assert p.default_key == "record"
    assert '"value": "42"' in preview.read_field(p.fields[0])


def test_remote_only_node_is_placeholder(tmp_path):
    p = preview.build_preview(_node(None, "sys_script", local=False))
    assert p.fields == [] and p.placeholder and "Pull" in p.placeholder or "pull" in p.placeholder
