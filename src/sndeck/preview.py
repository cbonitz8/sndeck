"""Resolve a selected tree FileNode into previewable fields (pure, no UI).
Code records expose their extracted script files (widgets = multiple); non-code
records fall back to record.json."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .registry import CODE_ARTIFACTS, field_extension
from .tree import FileNode

FIELD_LABELS = {
    "script": "Server",
    "client_script": "Client",
    "template": "HTML",
    "html": "HTML",
    "css": "CSS",
    "link": "Link",
    "option_schema": "Options",
    "demo_data": "Demo",
    "processing_script": "Processing",
    "operation_script": "Script",
}

DEFAULT_FIELD = {
    "sp_widget": "script",
    "sp_header_footer": "script",
    "sys_ui_page": "html",
}


@dataclass(frozen=True)
class PreviewField:
    key: str
    label: str
    path: Path


@dataclass(frozen=True)
class Preview:
    header: str
    fields: list[PreviewField]
    default_key: str | None
    placeholder: str | None = None


def build_preview(node: FileNode) -> Preview:
    header = f"{node.name} · {node.table} · {node.sys_id}"
    if not node.local or node.record_path is None:
        return Preview(header, [], None, placeholder="Not pulled. Press p to sync this set.")

    folder = Path(node.record_path)
    art = CODE_ARTIFACTS.get(node.table)
    fields_dict: dict[str, PreviewField] = {}
    if art:
        for f in art.script_fields:
            fp = folder / f"{f}{field_extension(f)}"
            if fp.exists():
                fields_dict[f] = PreviewField(f, FIELD_LABELS.get(f, f), fp)

    if not fields_dict:
        # non-code record, or code record with nothing extracted -> record.json
        return Preview(header, [PreviewField("record", "record.json", folder / "record.json")], "record")

    # Reorder fields for display: script, client_script, html/template, css, then others
    display_order = ["script", "client_script", "html", "template", "css", "link", "option_schema", "demo_data", "processing_script", "operation_script"]
    fields = [fields_dict[f] for f in display_order if f in fields_dict]

    preferred = DEFAULT_FIELD.get(node.table)
    default_key = preferred if any(f.key == preferred for f in fields) else fields[0].key
    return Preview(header, fields, default_key)


def read_field(field: PreviewField) -> str:
    text = field.path.read_text(encoding="utf-8")
    if field.key == "record":
        try:
            return json.dumps(json.loads(text), indent=2)
        except json.JSONDecodeError:
            return text
    return text
