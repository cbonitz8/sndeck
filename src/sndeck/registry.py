"""Code-artifact table registry: which fields are extracted to their own files."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactType:
    table: str
    script_fields: tuple[str, ...]
    folder_record: bool = False


def _t(table, fields, folder=False):
    return table, ArtifactType(table, tuple(fields), folder)


CODE_ARTIFACTS: dict[str, ArtifactType] = dict([
    _t("sys_script", ["script"]),
    _t("sys_script_include", ["script"]),
    _t("sys_script_client", ["script"]),
    _t("sys_ui_action", ["script"]),
    _t("sys_ui_script", ["script"]),
    _t("sys_ws_operation", ["operation_script"]),
    _t("sp_widget",
       ["template", "css", "client_script", "script", "link", "option_schema", "demo_data"],
       folder=True),
    _t("sp_ng_template", ["template"]),
    _t("sp_header_footer", ["template", "css", "client_script", "script", "link"], folder=True),
    _t("sys_ui_page", ["html", "client_script", "processing_script"], folder=True),
])

_EXT = {
    "template": ".html", "html": ".html",
    "css": ".scss",
    "client_script": ".js", "script": ".js", "server_script": ".js",
    "operation_script": ".js", "processing_script": ".js", "link": ".js",
    "option_schema": ".json", "demo_data": ".json",
}


def field_extension(field: str) -> str:
    return _EXT.get(field, ".txt")
