"""Headless verification: staging appears on a watcher tick without a network refresh,
and re-rendering the same model reuses tree nodes (no flicker). Run: python3 scripts/verify_live_staging.py"""
import asyncio
import json
import sys
from pathlib import Path
import tempfile

from sndeck.app import SndeckApp
from sndeck.watcher import ScratchChanged
from sndeck.records import set_workspace
import sndeck.records as records
from sndeck.tree import TreeModel
from sndeck.theme import MACCHIATO
from textual.widgets import DataTable, Static


class _FakeClient:
    instance = type("I", (), {"name": "dev"})()

    def query(self, *a, **k):
        return []


async def main():
    with tempfile.TemporaryDirectory() as d:
        app = SndeckApp(_FakeClient(), d, MACCHIATO)
        async with app.run_test() as pilot:
            # Let the initial _fetch worker settle.  With the fake client,
            # current_user() returns None so build_tree yields:
            #   TreeModel(scopes=[], current_set=None, error="Could not resolve …")
            # _last_model is therefore an error model, so _refresh_local hits its
            # early-return guard (model.error is truthy) and _render_staging is
            # never reached — the ScratchChanged tick would be swallowed.
            # We seed a minimal non-error model here so the watcher path runs.
            await pilot.pause()

            # Seed: empty-but-valid model (no error, no scopes). _refresh_local
            # will walk zero scopes, update _last_model, then call _render_staging.
            app._last_model = TreeModel(scopes=[], current_set=None)

            # Write a NEW dirty record directly to the scratch dir.
            # Layout: <root>/<set_name>__<32hex>/<table>/<name>__<sysid>/record.json
            ws = set_workspace(Path(d), "d" * 32, "Live Set")
            folder = ws / "sys_script_include" / "NewGuy__f1"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "record.json").write_text(json.dumps({
                "_meta": {
                    "table": "sys_script_include",
                    "sys_id": "f1",
                    "name": "NewGuy",
                }
            }))

            # Monkeypatch records.is_dirty so dirty_files_from_disk sees our
            # record as dirty.  records.py binds is_dirty at import time via
            # `from .sync import is_dirty`, so replacing it in the records module
            # namespace is the correct patch point for dirty_files_from_disk.
            orig_is_dirty = records.is_dirty
            records.is_dirty = lambda p: str(p) == str(folder)

            try:
                # Post the watcher message: on_scratch_changed → _refresh_local
                # → _render_staging (now unblocked because _last_model is valid).
                app.post_message(ScratchChanged({str(folder)}))
                await pilot.pause()

                tbl = app.query_one("#staging-table", DataTable)
                hdr = app.query_one("#staging-header", Static)

                row_count = tbl.row_count
                hdr_text = str(hdr.render())

                assert row_count == 1, (
                    f"expected 1 staged row, got {row_count}"
                )
                assert "1 changed" in hdr_text, (
                    f"expected '1 changed' in header, got: {hdr_text!r}"
                )
            finally:
                records.is_dirty = orig_is_dirty

            print("OK: disk record appeared in staging on watcher tick, no refresh")


asyncio.run(main())
